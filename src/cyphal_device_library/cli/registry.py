"""Simple CLI command to discover and display Cyphal nodes on the network."""

import asyncio

import rich
import typer
from rich.padding import Padding
from rich.pretty import Pretty
from rich.table import Column, Table

from ..client import Client
from ..registry import Registry

app = typer.Typer()


def format_registry(registry: Registry, title: str | None = None) -> Table:
    table = Table(
        "Name",
        "Type",
        "Value",
        Column("Flags", justify="right"),
        title=title or f"Registry for node ID {registry.node_id}",
        caption="Flags: '<' has min, '>' has max, '=' has default, 'M' mutable, 'P' persistent",
    )

    for register in registry:
        flags = " ".join(
            [
                "<" if register.has_min else " ",
                ">" if register.has_max else " ",
                "=" if register.has_default else " ",
                "M" if register.mutable else " ",
                "P" if register.persistent else " ",
            ]
        ).lstrip()
        table.add_row(register.name, register.dtype, Pretty(register.value), flags)

    return table


async def async_print_registry(node_id: int):
    with Client("com.starcopter.foo") as client:
        registry = Registry(node_id, client.node.make_client)
        await registry.discover_registers()
        table = format_registry(registry)

        rich.print(Padding(table, (1, 1)))


@app.command("print-registry")
def print_registry(ctx: typer.Context, node_id: int):
    """Print the registry for a given node ID."""
    asyncio.run(async_print_registry(node_id))
