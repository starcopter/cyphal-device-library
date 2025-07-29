"""CLI command to display the registry of a given node."""

import asyncio

import rich
import typer
from rich.padding import Padding

from ..client import Client
from ..registry import Registry

app = typer.Typer()


async def async_print_registry(node_id: int):
    with Client("cyphal.print-registry") as client:
        registry = Registry(node_id, client.node.make_client)
        await registry.discover_registers()
        rich.print(Padding(registry, (1, 2)))


@app.command("print-registry")
def print_registry(ctx: typer.Context, node_id: int):
    """Print the registry for a given node ID."""
    asyncio.run(async_print_registry(node_id))
