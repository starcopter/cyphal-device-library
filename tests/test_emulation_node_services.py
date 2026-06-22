"""Tests for emulated node GetInfo and register services."""

import uavcan.node

from cyphal_device_library.emulation import DeviceEmulationProfile, EmulationNodeConfig, PublicationPortSpec
from cyphal_device_library.emulation.base import RegisterMap


class _ExampleProfile(DeviceEmulationProfile):
    device_type = "example"
    cyphal_name = "com.example.device"
    description = "Example device"
    default_hardware_version = uavcan.node.Version_1(major=2, minor=1)
    default_software_version = uavcan.node.Version_1(major=3, minor=4)

    def default_registers(self) -> RegisterMap:
        from pycyphal.application.register import Natural16, String

        return {
            "uavcan.pub.status.id": Natural16([100]),
            "uavcan.pub.status.type": String("uavcan.primitive.Empty.1.0"),
        }

    def publication_specs(self) -> list[PublicationPortSpec]:
        return []

    def start_background_tasks(self, emulated_node, config: EmulationNodeConfig) -> list[object]:
        return []


def test_build_node_info_includes_identity_fields() -> None:
    profile = _ExampleProfile()
    config = profile.merge_add_config(
        {
            "node_info": {
                "unique_id": "01" * 16,
                "software_vcs_revision_id": 0xABCDEF01,
                "software_image_crc": [0x12345678],
                "hardware_version": {"major": 5, "minor": 6},
                "software_version": {"major": 7, "minor": 8},
            }
        }
    )
    info = profile.build_node_info(config)
    assert info.name.tobytes() == b"com.example.device"
    assert info.unique_id.tobytes().hex() == "01" * 16
    assert info.hardware_version.major == 5  # noqa: PLR2004
    assert info.software_version.minor == 8  # noqa: PLR2004
    assert info.software_vcs_revision_id == 0xABCDEF01
    assert list(info.software_image_crc) == [0x12345678]
