"""CLI command to display the registry of a given node."""

import asyncio
import json
import re
from typing import Annotated, Any

import rich
import typer
from rich.padding import Padding

from ..client import Client
from ..registry import Registry
from ._util import get_can_transport

app = typer.Typer()


def _parse_bool_token(token: str) -> bool:
    normalized = token.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {token!r}")


def _parse_cli_value_for_dtype(raw_value: str, dtype: str) -> Any:
    match = re.match(r"^(?P<base>[a-z0-9]+)(?:\[(?P<len>\d+)\])?$", dtype)
    if not match:
        raise ValueError(f"Unsupported register type descriptor: {dtype!r}")

    base_type = match.group("base")
    vector_len = int(match.group("len")) if match.group("len") is not None else None
    text = raw_value.strip()

    if base_type == "string":
        return raw_value

    if base_type == "unstructured":
        compact = text.replace(" ", "")
        if compact.startswith("0x"):
            compact = compact[2:]
        if compact and re.fullmatch(r"[0-9a-fA-F]+", compact) and len(compact) % 2 == 0:
            return bytes.fromhex(compact)
        return raw_value.encode("utf-8")

    if text.startswith("["):
        parsed_json = json.loads(text)
        if not isinstance(parsed_json, list):
            raise ValueError("Expected a JSON list for array input")
        raw_items = parsed_json
    elif "," in text:
        raw_items = [item.strip() for item in text.split(",")]
    else:
        raw_items = [text]

    if base_type == "bit":
        converted = [_parse_bool_token(str(item)) for item in raw_items]
    elif base_type.startswith("natural") or base_type.startswith("integer"):
        converted = [int(str(item), 0) for item in raw_items]
    elif base_type.startswith("real"):
        converted = [float(str(item)) for item in raw_items]
    else:
        raise ValueError(f"Unsupported register base type: {base_type!r}")

    if vector_len is None:
        if len(converted) != 1:
            raise ValueError(f"Type {dtype} expects a scalar value")
        return converted[0]

    if vector_len != len(converted):
        raise ValueError(f"Type {dtype} expects {vector_len} value(s), got {len(converted)}")
    return converted


@app.command("print-registry")
def print_registry(ctx: typer.Context, node_id: int):
    """Print the registry for a given node ID."""

    async def _run() -> None:
        try:
            can_transport = await get_can_transport(ctx)
        except Exception as e:
            rich.print(f"[red]:rotating_light: Failed to initialize CAN transport: {e}[/red]")
            return

        with Client("cyphal.print-registry", transport=can_transport) as client:
            registry = Registry(node_id, client.node.make_client)
            await registry.discover_registers()
            rich.print(Padding(registry, (1, 2)))

    asyncio.run(_run())


@app.command("r")
def read_register(
    ctx: typer.Context,
    node_id: int,
    register_name: str,
    value: Annotated[str | None, typer.Argument(help="Optional value to write before printing")] = None,
):
    """Read one register, or write it if a value is provided."""

    async def _run() -> None:
        try:
            can_transport = await get_can_transport(ctx)
        except Exception as e:
            rich.print(f"[red]:rotating_light: Failed to initialize CAN transport: {e}[/red]")
            return

        with Client("cyphal.read-register", transport=can_transport) as client:
            registry = Registry(node_id, client.node.make_client)
            await registry.refresh_register(register_name, full=True)

            if value is not None:
                register = registry[register_name]
                try:
                    parsed_value = _parse_cli_value_for_dtype(value, register.dtype)
                except (ValueError, json.JSONDecodeError) as ex:
                    raise typer.BadParameter(f"Invalid value for {register_name} ({register.dtype}): {ex}") from ex

                success = await registry.set_value(register_name, parsed_value)
                if not success:
                    raise typer.Exit(code=1)

                await registry.refresh_register(register_name, full=True)

            typer.echo(str(registry[register_name]))

    asyncio.run(_run())
