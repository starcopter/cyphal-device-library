"""Tests for generic Cyphal device emulation."""

import uavcan.node
from pycyphal.application.register import String

from cyphal_device_library.emulation import DeviceEmulationProfile, PublicationPortSpec
from cyphal_device_library.emulation.base import RegisterMap


class _ExampleProfile(DeviceEmulationProfile):
    device_type = "example"
    cyphal_name = "com.example.device"
    description = "Example device"
    default_hardware_version = uavcan.node.Version_1(major=1, minor=0)
    default_software_version = uavcan.node.Version_1(major=1, minor=0)

    def default_registers(self) -> RegisterMap:
        return {"example.value": String("default")}

    def publication_specs(self) -> list[PublicationPortSpec]:
        return [
            PublicationPortSpec(
                port_name="status",
                type_name="uavcan.primitive.Empty.1.0",
                subject_id=1,
                default_fields={},
            )
        ]

    def build_message(self, port_name, fields, emulated_node):
        return None


def test_profile_merge_add_config() -> None:
    profile = _ExampleProfile()
    config = profile.merge_add_config(
        {
            "registers": {"example.value": "override"},
            "publications": {"status": {"enabled": True}},
        }
    )
    assert config.registers["example.value"] == "override"
    assert config.publications["status"]["enabled"] is True
