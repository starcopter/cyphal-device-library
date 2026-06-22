"""Tests for publication port discovery."""

from dataclasses import dataclass
from typing import Iterator

from cyphal_device_library.publications import PublicationPort, discover_publication_ports
from cyphal_device_library.registry import NativeValue


@dataclass
class _FakeRegister:
    name: str
    value: NativeValue


class _FakeRegistry:
    def __init__(self, registers: list[_FakeRegister]) -> None:
        self._registers = {register.name: register for register in registers}

    def __iter__(self) -> Iterator[_FakeRegister]:
        return iter(self._registers.values())


def test_discover_publication_ports_groups_registers() -> None:
    registry = _FakeRegistry(
        [
            _FakeRegister("uavcan.pub.power_data.id", 6060),
            _FakeRegister("uavcan.pub.power_data.type", "uavcan.primitive.Empty.1.0"),
            _FakeRegister("uavcan.pub.power_data.dt_ms", 2000),
        ]
    )

    ports = discover_publication_ports(registry)
    assert len(ports) == 1
    assert ports[0].port_name == "power_data"
    assert ports[0].subject_id == 6060
    assert ports[0].type_name == "uavcan.primitive.Empty.1.0"
    assert ports[0].dt_ms == 2000
    assert ports[0].parse_status == "ok"


def test_publication_port_to_dict() -> None:
    port = PublicationPort(
        port_name="state",
        subject_id=6062,
        type_name="uavcan.primitive.Empty.1.0",
        dt_ms=2500,
        parse_status="missing_dsdl",
    )
    payload = port.to_dict()
    assert payload["port_name"] == "state"
    assert payload["parse_status"] == "missing_dsdl"
    assert payload["subscribed"] is False
