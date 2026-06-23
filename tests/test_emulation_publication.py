"""Tests for the generic node-owned publication loop."""

import asyncio

import uavcan.node
import uavcan.primitive
from pycyphal.application.register import Natural16, String

from cyphal_device_library.emulation import (
    DeviceEmulationProfile,
    EmulatedCyphalNode,
    PublicationPortSpec,
    RegisterMap,
)


class _PublishingProfile(DeviceEmulationProfile):
    device_type = "example"
    cyphal_name = "com.example.device"
    description = "Example device"
    default_hardware_version = uavcan.node.Version_1(major=1, minor=0)
    default_software_version = uavcan.node.Version_1(major=1, minor=0)

    def __init__(self) -> None:
        self.built_ports: list[str] = []

    def default_registers(self) -> RegisterMap:
        return {
            "uavcan.pub.status.id": Natural16([1234]),
            "uavcan.pub.status.type": String("uavcan.primitive.Empty.1.0"),
            "uavcan.pub.status.dt_ms": Natural16([10]),
        }

    def publication_specs(self) -> list[PublicationPortSpec]:
        return [
            PublicationPortSpec(
                port_name="status",
                type_name="uavcan.primitive.Empty.1.0",
                subject_id=1234,
                dt_ms=10,
            )
        ]

    def build_message(self, port_name, fields, emulated_node):
        self.built_ports.append(port_name)
        return uavcan.primitive.Empty_1_0()


def test_node_runs_generic_publication_loop() -> None:
    async def _run() -> None:
        profile = _PublishingProfile()
        config = profile.merge_add_config(None)
        emulated = EmulatedCyphalNode(profile, 42, "virtual:", config)
        emulated.start()
        try:
            await asyncio.sleep(0.05)
        finally:
            await emulated.stop()
        assert profile.built_ports, "build_message should be called by the publication loop"
        assert set(profile.built_ports) == {"status"}

    asyncio.run(_run())
