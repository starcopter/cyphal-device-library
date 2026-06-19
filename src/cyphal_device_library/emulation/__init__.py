"""Cyphal device node emulation for bus testing and development.

This package provides server-side Cyphal node emulation: each emulated device is a
real :mod:`pycyphal` node on a shared CAN interface, not a client pretending to be
a remote node. Device-type behavior (registers, publications, execute commands) is
declared through :class:`~cyphal_device_library.emulation.base.DeviceEmulationProfile`
subclasses.

Typical layout:

1. **Profile** — declare registers, publication ports, and background tasks.
2. **Node** — one :class:`~cyphal_device_library.emulation.node.EmulatedCyphalNode`
   per emulated device (created via :func:`create_emulated_node`).
3. **Host** — :class:`~cyphal_device_library.emulation.host.EmulatedNodeHost` manages
   multiple nodes on the same CAN interface.

Example — single emulated node::

    import asyncio
    from pycyphal.application.register import String
    import uavcan.node

    from cyphal_device_library.emulation import (
        DeviceEmulationProfile,
        EmulationNodeConfig,
        PublicationPortSpec,
        RegisterMap,
        create_emulated_node,
    )


    class ExampleProfile(DeviceEmulationProfile):
        device_type = "example"
        cyphal_name = "com.example.sensor"
        description = "Example emulated sensor"
        default_hardware_version = uavcan.node.Version_1(major=1, minor=0)
        default_software_version = uavcan.node.Version_1(major=1, minor=0)

        def default_registers(self) -> RegisterMap:
            return {"example.label": String("demo")}

        def publication_specs(self) -> list[PublicationPortSpec]:
            return []

        def start_background_tasks(self, emulated_node, config: EmulationNodeConfig) -> list:
            return []


    async def main() -> None:
        profile = ExampleProfile()
        config = profile.merge_add_config({"registers": {"example.label": "bench"}})
        emulated = create_emulated_node(profile, node_id=50, interface="virtual:", config=config)
        emulated.start()
        try:
            await asyncio.sleep(5)
        finally:
            await emulated.stop()


    asyncio.run(main())

Example — multiple nodes on one interface::

    import asyncio

    from cyphal_device_library.emulation import EmulatedNodeHost


    async def main(profile_a, profile_b) -> None:
        host = EmulatedNodeHost("virtual:")
        host.add(profile_a, node_id=10)
        host.add(profile_b, node_id=20, config={"publications": {"status": {"value": 1.0}}})
        try:
            await asyncio.sleep(10)
        finally:
            await host.stop_all()


    asyncio.run(main(profile_a, profile_b))
"""

from .base import DeviceEmulationProfile, EmulationNodeConfig, RegisterMap
from .factory import create_emulated_node
from .host import EmulatedNodeHost
from .local_registry import apply_native_register_overrides, configure_can_registers
from .node import EmulatedCyphalNode
from .publication_spec import PublicationPortSpec

__all__ = [
    "DeviceEmulationProfile",
    "EmulatedCyphalNode",
    "EmulatedNodeHost",
    "EmulationNodeConfig",
    "PublicationPortSpec",
    "RegisterMap",
    "apply_native_register_overrides",
    "configure_can_registers",
    "create_emulated_node",
]
