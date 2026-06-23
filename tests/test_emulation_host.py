"""Tests for creating multiple emulated nodes."""

import asyncio

import uavcan.node
from pycyphal.application.register import String

from cyphal_device_library.emulation import (
    DeviceEmulationProfile,
    EmulatedCyphalNode,
    PublicationPortSpec,
    RegisterMap,
)


class _ExampleProfile(DeviceEmulationProfile):
    device_type = "example"
    cyphal_name = "com.example.device"
    description = "Example device"
    default_hardware_version = uavcan.node.Version_1(major=1, minor=0)
    default_software_version = uavcan.node.Version_1(major=1, minor=0)

    def default_registers(self) -> RegisterMap:
        return {"example.value": String("default")}

    def publication_specs(self) -> list[PublicationPortSpec]:
        return []

    def build_message(self, port_name, fields, emulated_node):
        return None


def test_create_multiple_nodes_on_virtual_interface() -> None:
    async def _run() -> None:
        profile = _ExampleProfile()
        nodes = []
        try:
            for node_id in (40, 41):
                config = profile.merge_add_config(None)
                emulated = EmulatedCyphalNode(profile, node_id, "virtual:", config)
                emulated.start()
                nodes.append(emulated)
            assert len(nodes) == 2  # noqa: PLR2004
        finally:
            for emulated in nodes:
                await emulated.stop()

    asyncio.run(_run())
