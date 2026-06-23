"""Helpers to align emulation publication specs with register maps."""

from __future__ import annotations

import re
from typing import Any

from .base import RegisterMap
from .publication_spec import PublicationPortSpec

_PUB_ID_RE = re.compile(r"^uavcan\.pub\.(?P<port_name>[^.]+)\.id$")


def publication_port_names(registers: RegisterMap) -> set[str]:
    """Return port basenames declared via ``uavcan.pub.<port>.id`` registers."""
    return {match.group("port_name") for name in registers if (match := _PUB_ID_RE.match(name))}


def string_register_value(register_value: Any) -> str:
    """Decode a pycyphal String register value to a plain type name string."""
    text = str(register_value)
    marker = "value='"
    if marker in text:
        return text.split(marker, 1)[1].split("'", 1)[0]
    if hasattr(register_value, "value"):
        raw = register_value.value
        if isinstance(raw, str):
            return raw
        return bytes(raw).decode(errors="replace")
    return text


def scalar_register_value(register_value: Any) -> int:
    """Read the first element from a numeric register value."""
    return int(register_value.value[0])


def port_spec_from_registers(
    registers: RegisterMap,
    port_name: str,
    default_fields: dict[str, Any],
) -> PublicationPortSpec:
    """Build one :class:`PublicationPortSpec` from matching ``uavcan.pub.*`` registers."""
    subject_id = scalar_register_value(registers[f"uavcan.pub.{port_name}.id"])
    type_name = string_register_value(registers[f"uavcan.pub.{port_name}.type"])
    dt_ms_register = registers.get(f"uavcan.pub.{port_name}.dt_ms")
    dt_ms = scalar_register_value(dt_ms_register) if dt_ms_register is not None else None
    return PublicationPortSpec(
        port_name=port_name,
        type_name=type_name,
        subject_id=subject_id,
        dt_ms=dt_ms,
        default_fields=default_fields,
    )
