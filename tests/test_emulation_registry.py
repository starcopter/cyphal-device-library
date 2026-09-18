"""Tests for emulated node register Access that auto-creates unknown registers."""

from __future__ import annotations

import asyncio
from typing import cast

import uavcan.node
import uavcan.register
from pycyphal.application.register import Natural16, String
from pycyphal.transport.can import CANTransport

from cyphal_device_library.client import Client
from cyphal_device_library.emulation import (
    DeviceEmulationProfile,
    EmulatedCyphalNode,
    PublicationPortSpec,
    SharedCANMedia,
    apply_native_register_overrides,
    extract_can_media,
)
from cyphal_device_library.emulation.base import RegisterMap
from cyphal_device_library.registry import Registry
from cyphal_device_library.util import make_can_transport


class _ExampleProfile(DeviceEmulationProfile):
    device_type = "example"
    cyphal_name = "com.example.device"
    description = "Example device"
    default_hardware_version = uavcan.node.Version_1(major=1, minor=0)
    default_software_version = uavcan.node.Version_1(major=1, minor=0)

    def default_registers(self) -> RegisterMap:
        return {
            "example.value": String("default"),
            "uavcan.pub.status.id": Natural16([100]),
            "uavcan.pub.status.type": String("uavcan.primitive.Empty.1.0"),
        }

    def publication_specs(self) -> list[PublicationPortSpec]:
        return []

    def build_message(self, port_name, fields, emulated_node):
        return None


def test_apply_overrides_creates_missing_registers() -> None:
    import pycyphal.application

    registry = pycyphal.application.make_registry()
    apply_native_register_overrides(registry, {"custom.name": "hello", "custom.count": 7})
    assert "custom.name" in registry
    assert str(registry["custom.name"]) == "hello"
    assert int(registry["custom.count"]) == 7


def test_unknown_register_read_write_roundtrip() -> None:
    async def _run() -> None:
        profile = _ExampleProfile()
        config = profile.merge_add_config(None)
        transport = make_can_transport(iface="virtual:emulreg", bitrate=1_000_000, node_id=126)
        with Client("com.example.registry-test", transport=transport, pnp_server=False) as client:
            media = extract_can_media(client.node.presentation.transport)
            assert media is not None
            shared = SharedCANMedia(media, owns_media=False, already_running=True)
            emulated = EmulatedCyphalNode(
                profile,
                61,
                "virtual:emulreg",
                config,
                transport=CANTransport(shared, 61),
            )
            emulated.start()
            try:
                registry = Registry(61, client.node.make_client)
                await registry.refresh_register("unknown.fresh")
                assert registry["unknown.fresh"].value == ""

                written = await registry["unknown.fresh"].set_value("saved")
                assert written is True
                await registry.refresh_register("unknown.fresh")
                assert registry["unknown.fresh"].value == "saved"

                await registry.refresh_register("example.value")
                assert registry["example.value"].value == "default"

                access = client.node.make_client(uavcan.register.Access_1, 61)
                request = uavcan.register.Access_1.Request()
                request.name.name = "unknown.typed"
                request.value.natural16 = Natural16([42])
                result = await access.call(request)
                assert result is not None
                response = cast(uavcan.register.Access_1.Response, result[0])
                assert response.value.empty is None
                assert response.value.natural16 is not None
                assert list(map(int, response.value.natural16.value)) == [42]
            finally:
                await emulated.stop()

    asyncio.run(_run())
