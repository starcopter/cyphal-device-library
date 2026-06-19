"""Host for multiple emulated Cyphal nodes on one CAN interface."""

from __future__ import annotations

from typing import Any

from .base import DeviceEmulationProfile, EmulationNodeConfig
from .factory import create_emulated_node
from .node import EmulatedCyphalNode


class EmulatedNodeHost:
    """Manage emulated device nodes on a single CAN interface.

    One host owns the shared ``interface`` (and optional bitrate/MTU) while each
    added device is an independent :class:`~cyphal_device_library.emulation.node.EmulatedCyphalNode`
    with its own pycyphal transport inferior on that bus.

    Typical usage in a long-running service::

        host = EmulatedNodeHost("socketcan:can0", bitrate=[1_000_000, 5_000_000])
        host.add(bms_profile, node_id=50)
        host.add(mb_profile, node_id=10, config={"publications": {"mb_data": {"v48_mv": 47000}}})
        ...
        await host.stop_all()

    Use :meth:`build_devices_status` to produce JSON snapshots.
    """

    def __init__(
        self,
        interface: str,
        *,
        bitrate: int | list[int] | None = None,
        mtu: int | None = None,
    ) -> None:
        """Bind emulated nodes to one CAN interface and transport parameters."""
        self.interface = interface
        self.bitrate = bitrate
        self.mtu = mtu
        self._nodes: dict[int, EmulatedCyphalNode] = {}

    def list_devices(self) -> list[EmulatedCyphalNode]:
        """Return all running emulated nodes (order not guaranteed)."""
        return list(self._nodes.values())

    def get_device(self, node_id: int) -> EmulatedCyphalNode | None:
        """Return one emulated node by ID, or ``None`` if not present."""
        return self._nodes.get(node_id)

    def add(
        self,
        profile: DeviceEmulationProfile,
        node_id: int,
        config: dict[str, Any] | None = None,
    ) -> EmulatedCyphalNode:
        """Add and start one emulated device.

        Args:
            profile: Device-type profile to instantiate.
            node_id: Cyphal node ID; must be unique within this host.
            config: Optional ``{"registers": {...}, "publications": {...}}`` payload.

        Raises:
            ValueError: If ``node_id`` is already emulated on this host.

        Example::

            emulated = host.add(profile, node_id=50, config={
                "registers": {"bms.measurement.vbat.gain": 1.0},
            })
        """
        if node_id in self._nodes:
            raise ValueError(f"Emulated node {node_id} already exists")
        node_config: EmulationNodeConfig = profile.merge_add_config(config)
        emulated = create_emulated_node(
            profile,
            node_id,
            self.interface,
            node_config,
            bitrate=self.bitrate,
            mtu=self.mtu,
        )
        emulated.start()
        self._nodes[node_id] = emulated
        return emulated

    async def remove(self, node_id: int) -> bool:
        """Remove one emulated device by node ID.

        Returns:
            ``True`` if a device was removed, ``False`` if ``node_id`` was unknown.
        """
        emulated = self._nodes.pop(node_id, None)
        if emulated is None:
            return False
        await emulated.stop()
        return True

    async def stop_all(self) -> None:
        """Stop and remove all emulated devices."""
        node_ids = list(self._nodes.keys())
        for node_id in node_ids:
            await self.remove(node_id)

    def build_devices_status(
        self,
        *,
        device_type_key: str = "device_type",
    ) -> list[dict[str, Any]]:
        """Build a JSON-safe device list for status snapshots.

        Args:
            device_type_key: JSON key for :attr:`DeviceEmulationProfile.device_type`.

        Example return value::

            [{
                "node_id": 50,
                "device_type": "bms",
                "cyphal_name": "com.starcopter.highdra.bms",
                "publications": [...],
                "registers_summary": {"bms.measurement.vbat.gain": 1.0},
            }]
        """
        devices: list[dict[str, Any]] = []
        for node_id in sorted(self._nodes):
            emulated = self._nodes[node_id]
            devices.append(
                {
                    "node_id": node_id,
                    device_type_key: emulated.profile.device_type,
                    "cyphal_name": emulated.profile.cyphal_name,
                    "publications": emulated.publication_summary(),
                    "registers_summary": emulated.registers_summary(),
                }
            )
        return devices
