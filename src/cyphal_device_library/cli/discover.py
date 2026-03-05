"""Simple CLI command to discover and display Cyphal nodes on the network."""

import asyncio
import time
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


script_begin_time = time.monotonic()


def format_node_table(nodes: dict[int, pycyphal.application.node_tracker.Entry], pnp: bool) -> rich.table.Table:
    """Format a dictionary of Cyphal nodes into a rich table for display.

    Args:
        nodes: Dictionary mapping node IDs to their tracker entries.
            Each entry contains heartbeat and possibly node information.
        pnp: Whether the discovery is running in PnP Server mode, which may affect display hints.

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

    rows_added = False

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
        rows_added = True

    if not rows_added:
        table = rich.table.Table(title="Cyphal Nodes")
        table.add_column("No nodes discovered yet...")
        table.add_row(
            f"Tip: Use [green]-p[/green]{' ([bold]already active![/bold])' if pnp else ''} for a PnP Server, or [blue]-v[/blue] for higher verbosity!",
        )
        uptime_seconds = time.monotonic() - script_begin_time
        minutes = int(uptime_seconds // 60)
        seconds = int(uptime_seconds % 60)
        formatted_uptime = f"{minutes}:{seconds:02d}"
        table.add_row(f"Script running for: {formatted_uptime} \\[min:sec]")

    return table


@app.command()
def discover(
    ctx: typer.Context,
    frame_rate: Annotated[
        float, typer.Option(help="Refresh rate for updating the display, in frames per second. Defaults to 4.")
    ] = 4,
):
    """Discover and display Cyphal nodes on the network.

    Args:
        frame_rate: Refresh rate for updating the display, in frames per second. Defaults to 4.
    """

    async def _run() -> None:
        can_transport = await get_can_transport(ctx)
        pnp = bool(ctx.parent.params.get("pnp", False)) if ctx.parent else False

        from ..util import can_transport_bitrate, can_transport_cyphal_node_id, can_transport_interface

        assert can_transport_cyphal_node_id is not None
        assert can_transport_interface is not None
        assert can_transport_bitrate is not None

        command = (
            "cyphal discover"
            + (" -p" if pnp else "")
            + f" --can-protocol {'classic' if can_transport.protocol_parameters.mtu <= 8 else 'fd'}"
            + f" --interface {can_transport_interface}"
            + f" --cyphal-node-id {can_transport_cyphal_node_id}"
            + (
                f" --can-arb-bitrate {can_transport_bitrate[0]}"
                if can_transport.protocol_parameters.mtu <= 8
                else f" --can-data-bitrate {can_transport_bitrate[1]}"
            )
        )
        rich.print(f"Current discovery command: \n[bold green]{command}[/bold green]\n")

        with Client("com.starcopter.device-discovery", transport=can_transport, pnp_server=pnp) as client:

            def get_table():
                return rich.padding.Padding(format_node_table(client.node_tracker.registry, pnp), (0, 1))

            with rich.live.Live(get_table(), auto_refresh=True, refresh_per_second=frame_rate) as live:
                try:
                    while True:
                        live.update(get_table())
                        await asyncio.sleep(1 / frame_rate)
                except KeyboardInterrupt:
                    pass

    asyncio.run(_run())
