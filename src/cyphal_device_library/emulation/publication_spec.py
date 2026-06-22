"""Publication port metadata for emulated Cyphal devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PublicationPortSpec:
    """One publication port exposed by an emulated device.

    Describes a port that a :class:`~cyphal_device_library.emulation.base.DeviceEmulationProfile`
    will publish on the bus. The ``port_name`` must match a ``uavcan.pub.<port_name>.*``
    register group declared in :meth:`DeviceEmulationProfile.default_registers`.

    Attributes:
        port_name: Register port basename (e.g. ``"power_data"``).
        type_name: DSDL type name (e.g. ``"starcopter.highdra.bms.PowerData.0.2"``).
        subject_id: CAN subject ID from ``uavcan.pub.<port>.id``.
        dt_ms: Optional publication period in milliseconds.
        default_fields: JSON-serializable default message field values used by
            :meth:`DeviceEmulationProfile.merge_add_config` and background publishers.

    Example::

        PublicationPortSpec(
            port_name="power_data",
            type_name="starcopter.highdra.bms.PowerData.0.2",
            subject_id=6060,
            dt_ms=2000,
            default_fields={"vbat": 24.0, "vpack": 24.0, "ibat": 0.0},
        )
    """

    port_name: str
    type_name: str
    subject_id: int
    dt_ms: int | None = None
    default_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the port spec for JSON status payloads."""
        return {
            "port_name": self.port_name,
            "type_name": self.type_name,
            "subject_id": self.subject_id,
            "dt_ms": self.dt_ms,
            "default_fields": dict(self.default_fields),
        }
