import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pycyphal
import rich.console
import rich.padding
import rich.table
import typer
import uavcan.node
from pycyphal.application import node_tracker
from rich.prompt import Confirm

from ..client import Client
from ..util import async_prompt

logger = logging.getLogger(__name__ if __name__ != "__main__" else Path(__file__).name)
app = typer.Typer()


def _padded(table: rich.table.Table) -> rich.padding.Padding:
    return rich.padding.Padding(table, (1, 0))


@dataclass
class SoftwareFile:
    """Entry representing an available Software for Update."""

    _filename_pattern = re.compile(
        r"""^
            (?P<name>[a-zA-Z0-9.]+)       # Node name (e.g., com.zubax.telega)
            -
            (?P<hw_version>\d+\.\d+)      # Hardware version (required)
            -
            (?P<sw_version>\d+\.\d+)      # Software version (required)
            (?:\.(?P<vcs>[a-fA-F0-9]+))?  # Optional VCS hash
            (?:\.(?P<crc>[a-fA-F0-9]+))?  # Optional CRC
            \.app                         # Literal ".app"
            (?:\..+)?                     # Optional extension
        $""",
        re.VERBOSE,
    )

    file: Path

    name: str
    hw_version: str
    sw_version: str
    vcs: str | None = None
    crc: str | None = None

    @classmethod
    def from_file(cls, file: Path) -> "SoftwareFile":
        match = cls._filename_pattern.match(file.name)
        if not match:
            raise ValueError(f"Invalid filename: {file.name}")

        return cls(file, **match.groupdict())

    def is_compatible_to(self, node: node_tracker.Entry) -> bool:
        if node.info is None:
            logger.warning("Node %s: no info available, cannot determine compatibility", node.id)
            return False

        node_name = node.info.name.tobytes().decode()
        node_hw_version = f"{node.info.hardware_version.major}.{node.info.hardware_version.minor}"

        return self.name == node_name and self.hw_version == node_hw_version

    @property
    def _sort_key(self) -> tuple[str, str, str, float]:
        return self.name, self.hw_version, self.sw_version, self.file.stat().st_mtime


class SoftwareDirectory(list[SoftwareFile]):
    path: Path | None = None

    @classmethod
    def from_path(cls, path: str | Path) -> "SoftwareDirectory":
        self = cls()
        self.path = Path(path)

        logger.debug("Loading software files from %s", self.path.resolve())

        for file in self.path.glob("*.app*"):
            try:
                file = SoftwareFile.from_file(file)
                self.append(file)
                logger.debug("Found %s", file)
            except ValueError:
                logger.warning("Failed to parse software file %s", file)

        return self

    def get_updates_for(self, node: node_tracker.Entry) -> list[SoftwareFile]:
        if node.info is None:
            logger.warning("Node %s: no info available, cannot determine updates", node.id)
            return []

        compatible = [file for file in self if file.is_compatible_to(node)]
        name = node.info.name.tobytes().decode()
        hw_version = f"{node.info.hardware_version.major}.{node.info.hardware_version.minor}"
        logger.debug("%s %s: found %d compatible software files", name, hw_version, len(compatible))
        return compatible

    def get_update_for(self, node: node_tracker.Entry, force: bool = False) -> SoftwareFile | None:
        updates = self.get_updates_for(node)
        if not updates:
            return None

        assert node.info is not None, "Node info is required to determine updates"

        file = max(updates, key=lambda x: x._sort_key)
        file_version_tuple: tuple[int, int] = tuple(map(int, file.sw_version.split(".")))
        file_vcs_int = int(file.vcs, 16) if file.vcs else 0
        file_crc_int = int(file.crc, 16) if file.crc else 0

        node_version_tuple = (node.info.software_version.major, node.info.software_version.minor)
        node_vcs_int = node.info.software_vcs_revision_id
        node_crc_int = int(node.info.software_image_crc[0]) if node.info.software_image_crc.size > 0 else 0

        if (
            force
            or file_version_tuple > node_version_tuple
            or file_vcs_int != node_vcs_int
            or file_crc_int != node_crc_int
        ):
            return file

        return None

    def print_rich_table(self, console: rich.console.Console | None = None, **table_kwargs: Any) -> None:
        table = rich.table.Table("Name", "HW", "SW", "Git Hash", "CRC", title="Software Directory", **table_kwargs)
        if self.path:
            table.caption = f"Software files discovered in {self.path.resolve()}"

        for file in sorted(self, key=lambda x: x._sort_key):
            table.add_row(file.name, file.hw_version, file.sw_version, file.vcs or "", file.crc or "")

        if console:
            console.print(_padded(table))
        else:
            rich.print(_padded(table))


MODE_NAMES = {
    uavcan.node.Mode_1.OPERATIONAL: "Operational",
    uavcan.node.Mode_1.INITIALIZATION: "Initialization",
    uavcan.node.Mode_1.MAINTENANCE: "Maintenance",
    uavcan.node.Mode_1.SOFTWARE_UPDATE: "Software Update",
}

HEALTH_NAMES = {
    uavcan.node.Health_1.NOMINAL: "Nominal",
    uavcan.node.Health_1.ADVISORY: "Advisory",
    uavcan.node.Health_1.CAUTION: "Caution",
    uavcan.node.Health_1.WARNING: "Warning",
}


async def update_all_nodes(client: Client, software_files: SoftwareDirectory, console: rich.console.Console) -> None:
    nodes = {node_id: node for node_id, node in client.node_tracker.registry.items() if node.info is not None}
    updates = {
        node_id: file
        for node_id, node in nodes.items()
        if (file := software_files.get_update_for(node, force=True)) is not None
    }

    columns = "ID", "Name", "HW", "SW", "Git Hash", "CRC", "Mode", "Health"
    interface_name = os.environ.get("UAVCAN__CAN__IFACE")

    table = rich.table.Table(
        *columns,
        title="Pending Software Updates",
        caption=f"Pending software updates for nodes on CAN interface {interface_name!r}"
        if interface_name
        else "Pending software updates",
    )

    for node_id, file in sorted(updates.items()):
        info = nodes[node_id].info
        heartbeat = nodes[node_id].heartbeat
        node_hw_version = f"{info.hardware_version.major}.{info.hardware_version.minor}"
        node_sw_version = f"{info.software_version.major}.{info.software_version.minor}"
        node_vcs = f"{info.software_vcs_revision_id:08x}"
        node_crc = f"{info.software_image_crc[0]:08x}" if info.software_image_crc.size > 0 else ""
        node_mode = heartbeat.mode.value
        node_health = heartbeat.health.value

        table.add_row(
            str(node_id),
            info.name.tobytes().decode(),
            node_hw_version,
            f"{node_sw_version} → {file.sw_version}",
            f"{node_vcs[:8] or '[italic]None[/italic]'} → {file.vcs[:8] or '[italic]None[/italic]'}",
            f"{node_crc[:8] or '[italic]None[/italic]'} → {file.crc[:8] or '[italic]None[/italic]'}",
            MODE_NAMES[node_mode],
            HEALTH_NAMES[node_health],
        )

    console.print(_padded(table))

    if not await async_prompt(Confirm("Continue with update?", console=console)):
        logger.info("Update cancelled by user, exiting.")
        return

    console.print("")

    update_tasks = [client.update(node_id, file.file, timeout=300) for node_id, file in updates.items()]
    results = await asyncio.gather(*update_tasks, return_exceptions=True)

    table = rich.table.Table("ID", "Status", title="Update Status")

    for node_id, file, result in zip(updates.keys(), updates.values(), results):
        if isinstance(result, Exception):
            status = f"✘ failed to update: {result}"
        else:
            assert isinstance(result, float), f"Expected float, got {type(result)}"
            status = f"✔ successfully updated to v{file.sw_version} in {result:.2f}s"

        table.add_row(str(node_id), status)

    console.print(_padded(table))


async def main(parallel_updates: int = 1, software_path: str | Path = "bin"):
    console = rich.console.Console()

    software_files = SoftwareDirectory.from_path(software_path)
    software_files.print_rich_table(console)

    with Client("com.starcopter.update-server", parallel_updates=parallel_updates) as client:
        await asyncio.sleep(3)

        await update_all_nodes(client, software_files, console)

        await asyncio.sleep(0.1)


@app.command()
def update(
    parallel_updates: int | None = typer.Option(None, "--parallel", "-n", help="Number of parallel updates"),
    software_path: Path = typer.Option(Path("bin"), "--path", "-p", help="Path to software files"),
) -> None:
    """Update all nodes with the latest software."""

    os.environ.setdefault("UAVCAN__NODE__ID", "126")
    os.environ.setdefault("UAVCAN__CAN__IFACE", "usbtingo:")
    os.environ.setdefault("UAVCAN__CAN__MTU", "64")
    os.environ.setdefault("UAVCAN__CAN__BITRATE", "1000000 5000000")

    if parallel_updates is None:
        parallel_updates = 12 if "socketcan" in os.environ.get("UAVCAN__CAN__IFACE", "") else 1

    try:
        asyncio.run(main(parallel_updates, software_path))
    except KeyboardInterrupt:
        typer.echo("Cancelled by user, exiting.")
    except (pycyphal.presentation.PortClosedError, asyncio.InvalidStateError):
        pass
