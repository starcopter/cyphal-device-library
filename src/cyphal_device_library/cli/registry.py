"""CLI command to display the registry of a given node."""

import asyncio

import rich
import typer
from rich.padding import Padding

from ..client import Client
from ..registry import Registry
from ._util import get_can_transport

app = typer.Typer()


@app.command("print-registry")
def print_registry(ctx: typer.Context, node_id: int):
    """Print the registry for a given node ID."""

    async def _run() -> None:
        can_transport = await get_can_transport(ctx)

        with Client("cyphal.print-registry", transport=can_transport) as client:
            registry = Registry(node_id, client.node.make_client)
            await registry.discover_registers()
            rich.print(Padding(registry, (1, 2)))

    asyncio.run(_run())
