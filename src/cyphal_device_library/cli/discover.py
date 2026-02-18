"""Simple CLI command to discover and display Cyphal nodes on the network."""

import asyncio
from datetime import timedelta
from typing import Annotated

import pycyphal.application.node_tracker
import questionary
import rich
import rich.live
import rich.padding
import rich.table
import typer

from ..client import Client
from ..util import make_can_transport, select_can_channel
from ._util import Health, Mode

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
    interface: str | None = None,
    can_protocol: str | None = "classic",
    can_bitrate: int = 1_000_000,
    can_fd_bitrate: list[int] = [1_000_000, 5_000_000],
    node_id: int = 127,
    frame_rate: float = 4,
    pnp: bool = False,
):
    """Discover and display Cyphal nodes on the network.

    Args:
        interface: Optional CAN interface to use (e.g., 'socketcan:can0'). If None, the user will be prompted to select from available interfaces.
        can_protocol: CAN protocol to use ('classic' or 'fd'). Defaults to 'classic'.
        can_fd_bitrate: List of CAN FD bitrates to use. Defaults to [1_000_000, 5_000_000].
        node_id: Node ID to use for this client. Defaults to 127.
        frame_rate: Refresh rate for updating the display, in frames per second. Defaults to 4.
        pnp: Whether to use plug-and-play server functionality. Defaults to False.
    """
    if interface is None:
        interface = await select_can_channel()

    if can_protocol is None:
        question = questionary.select(
            "Use Classic-CAN or CAN-FD?", instruction="Select the CAN protocol", choices=["Classic CAN", "CAN FD"]
        )
        answer = await question.ask_async()
        if not answer:
            raise ValueError("No answer provided")
        if answer == "Classic CAN":
            can_protocol = "classic"
        else:
            can_protocol = "fd"

    if can_protocol == "classic":
        can_transport = make_can_transport(interface, can_bitrate, node_id)
    elif can_protocol == "fd":
        can_transport = make_can_transport(interface, can_fd_bitrate, node_id)
    else:
        raise ValueError(f"Unsupported CAN protocol: {can_protocol}. Use 'classic' or 'fd'.")

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
    transport: Annotated[
        str | None,
        typer.Option(
            help="CAN interface to be used. If not provided, the user will be asked to select one.",
        ),
    ] = None,
    can_protocol: Annotated[
        str | None,
        typer.Option(
            help="CAN protocol to use ('classic' or 'fd'). If not provided, the user will be asked to select one.",
        ),
    ] = None,
    node_id: Annotated[int, typer.Option(help="Node ID to use for this client. Defaults to 127.")] = 127,
    can_bitrate: Annotated[
        int, typer.Option(help="CAN bitrate in bits per second. Defaults to 1,000,000.")
    ] = 1_000_000,
    can_fd_bitrate: Annotated[
        list[int], typer.Option(help="CAN FD bitrates in bits per second. Defaults to [1,000,000, 5,000,000].")
    ] = [1_000_000, 5_000_000],
    frame_rate: Annotated[
        float, typer.Option(help="Refresh rate for updating the display, in frames per second. Defaults to 4.")
    ] = 4,
):
    """Discover and display Cyphal nodes on the network."""
    pnp = ctx.parent.params.get("pnp", False) if ctx.parent else False
    asyncio.run(
        async_discover(
            interface=transport,
            can_protocol=can_protocol,
            node_id=node_id,
            can_bitrate=can_bitrate,
            can_fd_bitrate=can_fd_bitrate,
            frame_rate=frame_rate,
            pnp=pnp,
        )
    )
