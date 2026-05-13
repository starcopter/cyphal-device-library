"""CLI command to execute a Cyphal ExecuteCommand request on a target node."""

import asyncio
from typing import Annotated

import rich
import typer
import uavcan.node

from ..client import Client
from ._util import get_can_transport

app = typer.Typer()


def _parse_command_number(value: str) -> int:
    normalized = value.strip().lower()
    shortcuts = {
        "r": uavcan.node.ExecuteCommand_1.Request.COMMAND_RESTART,
        "restart": uavcan.node.ExecuteCommand_1.Request.COMMAND_RESTART,
        "i": uavcan.node.ExecuteCommand_1.Request.COMMAND_IDENTIFY,
        "identify": uavcan.node.ExecuteCommand_1.Request.COMMAND_IDENTIFY,
    }
    if normalized in shortcuts:
        return int(shortcuts[normalized])

    try:
        command_number = int(value, 0)
    except ValueError as ex:
        raise typer.BadParameter("Command must be 'r' (restart), 'i' (identify), or an integer command number.") from ex

    if not 0 <= command_number <= 0xFFFF:
        raise typer.BadParameter("Command number must be in range 0..65535.")

    return command_number


@app.command("cmd")
def cmd(
    ctx: typer.Context,
    node_id: Annotated[int, typer.Argument(help="Target Cyphal node ID.")],
    cmd_nr: Annotated[
        str,
        typer.Argument(help="Command number, or shortcut: 'r' for restart, 'i' for identify."),
    ],
) -> None:
    """Execute uavcan.node.ExecuteCommand on one node and print the return status code."""

    async def _run() -> int:
        can_transport = await get_can_transport(ctx)
        command_number = _parse_command_number(cmd_nr)
        request = uavcan.node.ExecuteCommand_1.Request(command_number)

        with Client("com.starcopter.cyphal.cmd", transport=can_transport, pnp_server=False) as client:
            response = await client.execute_command(request, server_node_id=node_id)

        return int(response.status)

    try:
        status = asyncio.run(_run())
    except TimeoutError as ex:
        rich.print(f"[red]Timeout while executing command on node {node_id}: {ex}[/red]")
        raise typer.Exit(code=1) from ex

    typer.echo(status)
