"""Simple CLI command to discover and display Cyphal nodes on the network."""

import asyncio

import rich
import typer
from rich.padding import Padding
from rich.pretty import Pretty
from rich.table import Column, Table

from ..client import Client
from ..registry import Registry
from ._util import spaces_to_padding

app = typer.Typer()


def format_registry(registry: Registry, title: str | None = None) -> Table:
    table = Table(
        "Name",
        "Type",
        "Value",
        Column("Flags", justify="right"),
        title=title or f"Registry for node ID {registry.node_id}",
    )

    flags: dict[str, str] = {
        "<": "has_min",
        ">": "has_max",
        "=": "has_default",
        "M": "mutable",
        "P": "persistent",
    }
    used_flags: set[str] = set()

    for register in registry:
        reg_flags = " ".join([(flag if getattr(register, attr) else " ") for flag, attr in flags.items()]).lstrip()
        used_flags.update(*reg_flags.split())
        table.add_row(register.name, register.dtype, Pretty(register.value), spaces_to_padding(reg_flags))

    if used_flags:
        table.caption = f"Flags: {', '.join(f"'{flag}' {flags[flag].replace('_', ' ')}" for flag in used_flags)}"

    return table


async def async_print_registry(node_id: int):
    with Client("com.starcopter.foo") as client:
        registry = Registry(node_id, client.node.make_client)
        await registry.discover_registers()
        table = format_registry(registry)

        rich.print(Padding(table, (1, 2)))


@app.command("print-registry")
def print_registry(ctx: typer.Context, node_id: int):
    """Print the registry for a given node ID."""
    asyncio.run(async_print_registry(node_id))
