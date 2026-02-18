"""Simple CLI command to discover and display Cyphal nodes on the network."""

import asyncio
from datetime import timedelta
from typing import Annotated

import pycyphal.application.node_tracker
import pycyphal.transport
import rich
import rich.live
import rich.padding
import rich.table
import typer

from ..client import Client
from ._util import Health, Mode, get_can_transport

app = typer.Typer()


def format_node_table(nodes: dict[int, pycyphal.application.node_tracker.Entry]) -> rich.table.Table:
    """Format a dictionary of Cyphal nodes into a rich table for display.

    Args:
        nodes: Dictionary mapping node IDs to their tracker entries.
            Each entry contains heartbeat and possibly node information.

    Returns:
        rich.table.Table: A formatted table containing node information.
    """
    table = rich.table.Table(title="Cyphal Nodes")
    table.add_column("Node ID", justify="right")
    table.add_column("Uptime", justify="right")
    table.add_column("Health")
    table.add_column("Mode")
    table.add_column("VSSC", justify="right")
    table.add_column("Name")
    table.add_column("HW")
    table.add_column("SW")
    table.add_column("Git Hash")
    table.add_column("CRC")
    table.add_column("Unique ID")

    for node_id, entry in sorted(nodes.items()):
        vssc = entry.heartbeat.vendor_specific_status_code
        row = [
            str(node_id),
            str(timedelta(seconds=entry.heartbeat.uptime)),
            Health(entry.heartbeat.health.value).name,
            Mode(entry.heartbeat.mode.value).name,
            f"{vssc:d} / 0x{vssc:02x}",
        ]

        if entry.info is not None:
            git_hash = f"{entry.info.software_vcs_revision_id:016x}" if entry.info.software_vcs_revision_id else ""
            crc: int | None = int(entry.info.software_image_crc[0]) if entry.info.software_image_crc.size > 0 else None

            row.extend(
                [
                    entry.info.name.tobytes().decode(),
                    f"{entry.info.hardware_version.major}.{entry.info.hardware_version.minor}",
                    f"{entry.info.software_version.major}.{entry.info.software_version.minor}",
                    git_hash,
                    f"{crc:016x}" if crc is not None else "",
                    entry.info.unique_id.tobytes().hex(),
                ]
            )

        table.add_row(*row)

    return table


async def async_discover(
    can_transport: pycyphal.transport.Transport | None = None,
    frame_rate: float = 4,
    pnp: bool = False,
):
    """Discover and display Cyphal nodes on the network.

    Args:
        can_transport: Optional CAN transport to use. If None, the user will be prompted to select from available interfaces.
        frame_rate: Refresh rate for updating the display, in frames per second. Defaults to 4.
        pnp: Whether to use plug-and-play server functionality. Defaults to False.
    """

    with Client("com.starcopter.device-discovery", transport=can_transport, pnp_server=pnp) as client:

        def get_table():
            return rich.padding.Padding(format_node_table(client.node_tracker.registry), (0, 1))

        with rich.live.Live(get_table(), auto_refresh=True, refresh_per_second=frame_rate) as live:
            try:
                while True:
                    live.update(get_table())
                    await asyncio.sleep(1 / frame_rate)
            except KeyboardInterrupt:
                pass


@app.command()
def discover(
    ctx: typer.Context,
    frame_rate: Annotated[
        float, typer.Option(help="Refresh rate for updating the display, in frames per second. Defaults to 4.")
    ] = 4,
):
    """Discover and display Cyphal nodes on the network."""
    can_transport = asyncio.run(get_can_transport(ctx))
    pnp = ctx.parent.params.get("pnp", None) if ctx.parent else None

    asyncio.run(
        async_discover(
            can_transport=can_transport,
            frame_rate=frame_rate,
            pnp=pnp,
        )
    )
