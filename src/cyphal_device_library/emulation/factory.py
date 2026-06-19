"""Factory for emulated Cyphal nodes."""

from __future__ import annotations

from .base import DeviceEmulationProfile, EmulationNodeConfig
from .node import EmulatedCyphalNode


def create_emulated_node(
    profile: DeviceEmulationProfile,
    node_id: int,
    interface: str,
    config: EmulationNodeConfig,
    *,
    initial_uptime: float = 10.0,
    bitrate: int | list[int] | None = None,
    mtu: int | None = None,
) -> EmulatedCyphalNode:
    """Create an emulated node from a profile and runtime config.

    Validates ``node_id`` and forwards transport options to
    :class:`~cyphal_device_library.emulation.node.EmulatedCyphalNode`.
    The returned node is **not** started; call :meth:`EmulatedCyphalNode.start`.

    Args:
        profile: Device-type emulation profile.
        node_id: Cyphal node ID (0–127).
        interface: CAN interface string shared with other emulated nodes.
        config: Merged register/publication overrides from
            :meth:`DeviceEmulationProfile.merge_add_config`.
        initial_uptime: Reported heartbeat uptime offset in seconds.
        bitrate: Optional classic or FD bitrate passed to transport registers.
        mtu: Optional CAN frame MTU override.

    Example::

        profile = BMSEmulationProfile()
        config = profile.merge_add_config({
            "publications": {"power_data": {"vbat": 24.0, "ibat": 0.0}},
        })
        emulated = create_emulated_node(profile, 50, "virtual:", config)
        emulated.start()
    """
    if node_id < 0 or node_id > 127:  # noqa: PLR2004
        raise ValueError("node_id must be between 0 and 127")
    return EmulatedCyphalNode(
        profile,
        node_id,
        interface,
        config,
        initial_uptime=initial_uptime,
        bitrate=bitrate,
        mtu=mtu,
    )
