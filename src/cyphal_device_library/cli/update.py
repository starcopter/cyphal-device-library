"""
Software update CLI for Cyphal devices.

THIS FILE IS A QUICK AND DIRTY IMPLEMENTATION.
While core functionality is there, there is also a lot of duplicate code.

Use at your own risk.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import pycyphal
import pycyphal.transport
import rich.console
import rich.padding
import rich.progress
import rich.table
import typer
import uavcan.node
from pycyphal.application import node_tracker
from pycyphal.application.node_tracker import Entry
from rich.prompt import Confirm

from ..client import Client
from ..util import async_prompt
from ._util import get_can_transport, parse_int_set
from .discover import format_node_table

logger = logging.getLogger(__name__ if __name__ != "__main__" else Path(__file__).name)
app = typer.Typer()


def _padded(table: rich.table.Table) -> rich.padding.Padding:
    return rich.padding.Padding(table, (1, 0))


def get_default_parallel_updates(iface: str = os.environ.get("UAVCAN__CAN__IFACE", "")) -> int:
    return 12 if "socketcan" in iface else 1


@dataclass
class SoftwareFile:
    """Entry representing an available Software for Update."""

    _filename_pattern = re.compile(
        r"""^
            (?P<name>[a-zA-Z0-9.\-_]+)    # Node name (e.g., com.zubax.telega)
            -
            (?P<hw_version>\d+\.\d+)      # Hardware version (required)
            -
            v?(?P<sw_version>\d+\.\d+)    # Software version (required)
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

    def is_hw_compatible_to(self, node: node_tracker.Entry) -> bool:
        if node.info is None:
            logger.warning("Node %s: no info available, cannot determine compatibility", node.id)
            return False

        node_hw_version = f"{node.info.hardware_version.major}.{node.info.hardware_version.minor}"

        return self.hw_version == node_hw_version

    def is_selftest(self) -> bool:
        return "selftest" in self.name

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

    def get_updates_for(self, node: node_tracker.Entry, selftest_update: bool = False) -> list[SoftwareFile]:
        if node.info is None:
            logger.warning("Node %s: no info available, cannot determine updates", node.id)
            return []
        if not selftest_update:
            compatible = [file for file in self if file.is_compatible_to(node)]
        else:
            if "selftest" not in node.info.name.tobytes().decode():
                logger.warning(
                    "Node %s: selftest update requested, but node is not a selftest node",
                    node.info.name.tobytes().decode(),
                )
                return []
            compatible = [file for file in self if file.is_hw_compatible_to(node) and not file.is_selftest()]
        name = node.info.name.tobytes().decode()
        hw_version = f"{node.info.hardware_version.major}.{node.info.hardware_version.minor}"
        logger.debug("%s %s: found %d compatible software files", name, hw_version, len(compatible))
        return compatible

    def get_update_for(
        self, node: node_tracker.Entry, force: bool = False, selftest_update: bool = False
    ) -> SoftwareFile | None:
        updates = self.get_updates_for(node, selftest_update=selftest_update)
        if not updates:
            return None

        assert node.info is not None, "Node info is required to determine updates"

        if len(updates) > 1:
            logger.warning(
                "Node %s: found multiple compatible software files (%d), using the latest one",
                node.id,
                len(updates),
            )
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


async def update_all_selftest_nodes(
    client: Client,
    software_files: SoftwareDirectory,
    console: rich.console.Console,
    timeout: float = 10,
) -> None:
    nodes = {node_id: node for node_id, node in client.node_tracker.registry.items() if node.info is not None}
    updates = {
        node_id: file
        for node_id, node in nodes.items()
        if node.info is not None and "selftest" in node.info.name.tobytes().decode()
        # == "com.starcopter.selftest"
        if (file := software_files.get_update_for(node, selftest_update=True)) is not None
        # and "selftest" not in file.name
        # and "selftest" in node.info.name.tobytes().decode() # or == "com.starcopter.selftest"
    }

    for node_id, file in updates.items():
        logger.info("Node %d: updating to %s", node_id, file.file.name)

    if not updates:
        logger.info("No nodes to update")
        return
    await execute_updates(client, console, timeout=timeout, nodes=nodes, updates=updates)


async def update_all_nodes(
    client: Client,
    software_files: SoftwareDirectory,
    console: rich.console.Console,
    timeout: float = 10,
    force: bool = False,
) -> None:
    nodes = {node_id: node for node_id, node in client.node_tracker.registry.items() if node.info is not None}
    updates = {
        node_id: file
        for node_id, node in nodes.items()
        if (file := software_files.get_update_for(node, force=force)) is not None
    }
    await execute_updates(client, console, timeout=timeout, nodes=nodes, updates=updates)


async def execute_updates(
    client: Client,
    console: rich.console.Console,
    timeout: float,
    nodes: dict[int, Entry],
    updates: dict[int, SoftwareFile],
) -> None:
    if not updates:
        logger.info("No nodes to update")
        return

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
        node_vcs = f"{info.software_vcs_revision_id:016x}"
        node_crc = f"{info.software_image_crc[0]:016x}" if info.software_image_crc.size > 0 else ""
        node_mode = heartbeat.mode.value
        node_health = heartbeat.health.value
        file_vcs = file.vcs or ""
        file_crc = file.crc or ""

        table.add_row(
            str(node_id),
            info.name.tobytes().decode(),
            node_hw_version,
            f"{node_sw_version} → {file.sw_version}",
            f"{node_vcs[:8] or '[italic]None[/italic]'} → {file_vcs[:8] or '[italic]None[/italic]'}",
            f"{node_crc[:8] or '[italic]None[/italic]'} → {file_crc[:8] or '[italic]None[/italic]'}",
            MODE_NAMES[node_mode],
            HEALTH_NAMES[node_health],
        )

    console.print(_padded(table))

    if not await async_prompt(Confirm("Continue with update?", console=console)):
        logger.info("Update cancelled by user, exiting.")
        return

    console.print("")

    with rich.progress.Progress(console=console, auto_refresh=True) as progress:
        update_tasks: list[asyncio.Task[float]] = []
        for node_id, file in updates.items():
            task_id = progress.add_task(f"Updating node {node_id}...", total=file.file.stat().st_size)

            def callback(task_id: int, _bytes: int) -> None:
                progress.update(task_id, completed=_bytes)

            coroutine = client.update(node_id, file.file, timeout=timeout, callback=partial(callback, task_id))
            update_tasks.append(asyncio.create_task(coroutine))

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


async def async_selftest_update_all(
    can_transport: pycyphal.transport.Transport,
    parallel_updates: int = 1,
    software_path: str | Path = "bin",
    timeout: float = 10,
    pnp: bool = False,
):
    console = rich.console.Console()

    software_files = SoftwareDirectory.from_path(software_path)
    software_files.print_rich_table(console)

    with Client(
        "com.starcopter.update-server", parallel_updates=parallel_updates, pnp_server=pnp, transport=can_transport
    ) as client:
        await asyncio.sleep(3)

        await update_all_selftest_nodes(client, software_files, console, timeout=timeout)

        await asyncio.sleep(0.1)


@app.command()
def selftest_update_all(
    ctx: typer.Context,
    parallel_updates: int | None = typer.Option(None, "--parallel", "-n", help="Number of parallel updates"),
    software_path: Path = typer.Option(Path("bin"), "--path", "-p", help="Path to software files"),
    timeout: float = typer.Option(10, "--timeout", "-t", help="Timeout for each update in seconds."),
) -> None:
    """Update all selftest nodes with the latest software."""

    pnp = ctx.parent.params.get("pnp", False) if ctx.parent else False
    can_transport = asyncio.run(get_can_transport(ctx))

    try:
        asyncio.run(
            async_selftest_update_all(
                parallel_updates=parallel_updates or get_default_parallel_updates(),
                software_path=software_path,
                can_transport=can_transport,
                timeout=timeout,
                pnp=pnp,
            )
        )
    except KeyboardInterrupt:
        typer.echo("Cancelled by user, exiting.")
    except (pycyphal.presentation.PortClosedError, asyncio.InvalidStateError):
        pass


@app.command()
def update(
    ctx: typer.Context,
    nodes: Annotated[
        str,
        typer.Argument(help="Set of Node IDs (e.g. '1,3,10-20,!13'), or 'all' to update all available nodes"),
    ],
    file: Annotated[Path, typer.Argument(exists=True, help="Path to software file (*.bin)", dir_okay=False)],
    parallel_updates: Annotated[
        int | None, typer.Option(..., "--parallel", "-n", help="Number of parallel updates.")
    ] = None,
    timeout: Annotated[float, typer.Option(..., "--timeout", "-t", help="Timeout for each update in seconds.")] = 10,
) -> None:
    """Update a specified set of nodes with a specific software file."""

    node_set = set(range(126)) if nodes == "all" else parse_int_set(nodes)
    pnp = ctx.parent.params.get("pnp", False) if ctx.parent else False
    can_transport = asyncio.run(get_can_transport(ctx))

    try:
        asyncio.run(
            async_update_single(
                can_transport=can_transport,
                node_ids=node_set,
                file=file,
                parallel_updates=parallel_updates or get_default_parallel_updates(),
                timeout=timeout,
                pnp=pnp,
            )
        )
    except KeyboardInterrupt:
        typer.echo("Cancelled by user, exiting.")
    except (pycyphal.presentation.PortClosedError, asyncio.InvalidStateError):
        pass


async def async_update_single(
    node_ids: set[int],
    file: Path,
    parallel_updates: int,
    can_transport: pycyphal.transport.Transport,
    timeout: float = 300,
    pnp: bool = False,
) -> None:
    console = rich.console.Console()

    with Client(
        "com.starcopter.update-server", parallel_updates=parallel_updates, pnp_server=pnp, transport=can_transport
    ) as client:
        await asyncio.sleep(2)

        node_registry = client.node_tracker.registry

        available_nodes = set(node_registry.keys())
        nodes_to_update = node_ids & available_nodes
        nodes_to_skip = available_nodes - nodes_to_update

        if nodes_to_skip:
            logger.info("Skipping %d nodes: %s", len(nodes_to_skip), nodes_to_skip)

        if not nodes_to_update:
            logger.info("No nodes to update")
            await asyncio.sleep(0.1)
            return

        logger.info("Updating %d nodes: %s", len(nodes_to_update), nodes_to_update)

        table = format_node_table({node_id: node_registry[node_id] for node_id in nodes_to_update})

        columns = "ID", "Name", "HW", "SW", "Git Hash", "CRC", "Mode", "Health"
        table = rich.table.Table(
            *columns,
            title="Pending Software Updates",
            caption=f"Nodes to update to {file.name}",
        )
        for node_id in nodes_to_update:
            heartbeat, info = node_registry[node_id]
            node_hw_version = f"{info.hardware_version.major}.{info.hardware_version.minor}"
            node_sw_version = f"{info.software_version.major}.{info.software_version.minor}"
            node_vcs = f"{info.software_vcs_revision_id:016x}" if info.software_vcs_revision_id else ""
            node_crc = f"{info.software_image_crc[0]:016x}" if info.software_image_crc.size > 0 else ""
            node_mode = heartbeat.mode.value
            node_health = heartbeat.health.value

            table.add_row(
                str(node_id),
                info.name.tobytes().decode(),
                node_hw_version,
                node_sw_version,
                node_vcs,
                node_crc,
                MODE_NAMES[node_mode],
                HEALTH_NAMES[node_health],
            )

        console.print(_padded(table))

        if not await async_prompt(Confirm("Continue with update?", console=console)):
            logger.info("Update cancelled by user, exiting.")
            return

        console.print("")

        with rich.progress.Progress(console=console, auto_refresh=True) as progress:
            update_tasks: list[asyncio.Task[float]] = []
            for node_id in nodes_to_update:
                task_id = progress.add_task(f"Updating node {node_id}...", total=file.stat().st_size)

                def callback(task_id: int, _bytes: int) -> None:
                    progress.update(task_id, completed=_bytes)

                coroutine = client.update(node_id, file, timeout=timeout, callback=partial(callback, task_id))
                update_tasks.append(asyncio.create_task(coroutine))

            results = await asyncio.gather(*update_tasks, return_exceptions=True)

        table = rich.table.Table("ID", "Status", title="Update Status")

        for node_id, result in zip(nodes_to_update, results):
            if isinstance(result, Exception):
                status = f"✘ failed to update: {result}"
            else:
                assert isinstance(result, float), f"Expected float, got {type(result)}"
                status = f"✔ successfully updated to {file.name} in {result:.2f}s"

            table.add_row(str(node_id), status)

        console.print(_padded(table))


@app.command()
def update_all(
    ctx: typer.Context,
    parallel_updates: int | None = typer.Option(None, "--parallel", "-n", help="Number of parallel updates"),
    software_path: Path = typer.Option(Path("bin"), "--path", "-p", help="Path to software files"),
    timeout: float = typer.Option(10, "--timeout", "-t", help="Timeout for each update in seconds."),
    force: bool = typer.Option(False, "--force", "-f", help="Force update even if the software is up to date."),
) -> None:
    """Update all nodes with the latest software."""

    pnp = ctx.parent.params.get("pnp", False) if ctx.parent else False
    can_transport = asyncio.run(get_can_transport(ctx))

    try:
        asyncio.run(
            async_update_all(
                can_transport=can_transport,
                parallel_updates=parallel_updates or get_default_parallel_updates(),
                software_path=software_path,
                timeout=timeout,
                force=force,
                pnp=pnp,
            )
        )
    except KeyboardInterrupt:
        typer.echo("Cancelled by user, exiting.")
    except (pycyphal.presentation.PortClosedError, asyncio.InvalidStateError):
        pass


async def async_update_all(
    can_transport: pycyphal.transport.Transport,
    parallel_updates: int = 1,
    software_path: str | Path = "bin",
    timeout: float = 10,
    force: bool = False,
    pnp: bool = False,
):
    console = rich.console.Console()

    software_files = SoftwareDirectory.from_path(software_path)
    software_files.print_rich_table(console)

    with Client(
        "com.starcopter.update-server", parallel_updates=parallel_updates, pnp_server=pnp, transport=can_transport
    ) as client:
        await asyncio.sleep(3)

        await update_all_nodes(client, software_files, console, timeout=timeout, force=force)

        await asyncio.sleep(0.1)
