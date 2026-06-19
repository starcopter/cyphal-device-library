"""Discover device publication ports from Cyphal registers."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from .registry import NativeValue, Registry
from .util.message_types import load_message_type

PUB_REGISTER_RE = re.compile(r"^uavcan\.pub\.(?P<port_name>[^.]+)\.(?P<field>id|type|dt_ms)$")


class _PublicationRegisterSource(Protocol):
    name: str
    value: NativeValue


class _PublicationRegistrySource(Protocol):
    def __iter__(self) -> Iterator[_PublicationRegisterSource]: ...


@dataclass(frozen=True)
class PublicationPort:
    """One publication port described by uavcan.pub.* registers."""

    port_name: str
    subject_id: int
    type_name: str
    dt_ms: int | None = None
    message_type: type | None = None
    parse_status: str = "ok"

    def to_dict(self) -> dict:
        """Serialize for JSON payloads."""
        return {
            "port_name": self.port_name,
            "subject_id": self.subject_id,
            "type_name": self.type_name,
            "dt_ms": self.dt_ms,
            "parse_status": self.parse_status,
            "subscribed": self.message_type is not None and self.parse_status == "ok",
        }


def _scalar_int(value: NativeValue) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, bool):
            return int(first)
        if isinstance(first, int):
            return first
    return None


def publication_register_names(port_name: str) -> tuple[str, str]:
    """Return required register names for one publication port."""
    return (f"uavcan.pub.{port_name}.id", f"uavcan.pub.{port_name}.type")


def discover_publication_ports(registry: _PublicationRegistrySource) -> list[PublicationPort]:
    """Build publication catalog entries from a populated registry."""
    grouped: dict[str, dict[str, NativeValue]] = defaultdict(dict)

    for register in registry:
        match = PUB_REGISTER_RE.match(register.name)
        if not match:
            continue
        grouped[match.group("port_name")][match.group("field")] = register.value

    ports: list[PublicationPort] = []
    for port_name in sorted(grouped):
        fields = grouped[port_name]
        subject_id = _scalar_int(fields.get("id"))
        type_name = fields.get("type")
        if subject_id is None or not isinstance(type_name, str) or not type_name.strip():
            continue

        dt_ms = _scalar_int(fields.get("dt_ms"))
        parse_status = "ok"
        message_type: type | None = None
        try:
            message_type = load_message_type(type_name)
        except ValueError, RuntimeError:
            parse_status = "missing_dsdl"

        ports.append(
            PublicationPort(
                port_name=port_name,
                subject_id=subject_id,
                type_name=type_name.strip(),
                dt_ms=dt_ms,
                message_type=message_type,
                parse_status=parse_status,
            )
        )

    return ports


async def discover_publication_ports_remote(registry: Registry) -> list[PublicationPort]:
    """Discover registers on the remote node and return publication ports."""
    await registry.discover_registers()
    return discover_publication_ports(registry)
