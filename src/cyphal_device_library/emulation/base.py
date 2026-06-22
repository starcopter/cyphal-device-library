"""Abstract emulation profile for Cyphal device types."""

from __future__ import annotations

import abc
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pycyphal.application
import uavcan.node

from .publication_spec import PublicationPortSpec

if TYPE_CHECKING:
    from .node import EmulatedCyphalNode

# pycyphal local registry values (String, Natural16, Real32, …), not uavcan.register.Value_1.
RegisterMap = dict[str, Any]


@dataclass
class EmulationNodeConfig:
    """Runtime configuration for one emulated node.

    Produced by :meth:`DeviceEmulationProfile.merge_add_config` and passed to
    :class:`~cyphal_device_library.emulation.node.EmulatedCyphalNode`.

    Attributes:
        registers: Register name → native value overrides applied after defaults.
        publications: Port name → field dict merged onto profile publication defaults.
        unique_id: Optional 16-byte node UID reported by GetInfo.
        software_vcs_revision_id: Optional Git revision reported by GetInfo.
        software_image_crc: Optional firmware image CRC list reported by GetInfo.
        hardware_version: Optional hardware version override for GetInfo.
        software_version: Optional software version override for GetInfo.
    """

    registers: RegisterMap = field(default_factory=dict)
    publications: dict[str, dict[str, Any]] = field(default_factory=dict)
    unique_id: bytes | None = None
    software_vcs_revision_id: int | None = None
    software_image_crc: list[int] | None = None
    hardware_version: uavcan.node.Version_1 | None = None
    software_version: uavcan.node.Version_1 | None = None


class DeviceEmulationProfile(abc.ABC):
    """Declare registers, publications, and bus behavior for one device type.

    Subclass this to describe how an emulated node behaves on the CAN bus.
    Profiles are consumed by :class:`~cyphal_device_library.emulation.node.EmulatedCyphalNode`.

    Required class attributes:
        device_type: Short identifier (e.g. ``"bms"``).
        cyphal_name: Cyphal node name (e.g. ``"com.starcopter.highdra.bms"``).
        description: Human-readable description stored in ``uavcan.node.description``.
        default_hardware_version: Reported hardware version.
        default_software_version: Reported software version.

    Required methods:
        :meth:`default_registers` — full register map including ``uavcan.pub.*``.
        :meth:`publication_specs` — ports with default message field values.
        :meth:`start_background_tasks` — periodic publishers/subscribers.

    Optional overrides:
        :meth:`handle_execute_command` — device-specific execute-command handling.

    Example::

        class SensorProfile(DeviceEmulationProfile):
            device_type = "sensor"
            cyphal_name = "com.example.sensor"
            description = "Emulated sensor"
            default_hardware_version = uavcan.node.Version_1(major=1, minor=0)
            default_software_version = uavcan.node.Version_1(major=1, minor=0)

            def default_registers(self) -> RegisterMap:
                return {"sensor.gain": Real32([1.0])}

            def publication_specs(self) -> list[PublicationPortSpec]:
                return [PublicationPortSpec("reading", "example.Reading.0.1", 100)]

            def start_background_tasks(self, emulated_node, config):
                return []  # add asyncio publisher tasks here
    """

    device_type: str
    cyphal_name: str
    description: str
    default_hardware_version: uavcan.node.Version_1
    default_software_version: uavcan.node.Version_1

    @abc.abstractmethod
    def default_registers(self) -> RegisterMap:
        """Return the full register map for a new emulated node."""

    @abc.abstractmethod
    def publication_specs(self) -> list[PublicationPortSpec]:
        """Return publication ports and default message field values."""

    def publication_config_schema(self) -> dict[str, dict[str, str]]:
        """Return per-port JSON field types for UI validation.

        Example return value::

            {"power_data": {"vbat": "float", "ibat": "float"}}
        """
        return {
            spec.port_name: {key: type(value).__name__ for key, value in spec.default_fields.items()}
            for spec in self.publication_specs()
        }

    def merge_add_config(self, config: dict[str, Any] | None) -> EmulationNodeConfig:
        """Overlay add-device config onto profile defaults.

        Merges optional ``registers`` and ``publications`` keys from a config onto the profile's built-in defaults.

        Example::

            config = profile.merge_add_config({
                "registers": {"bms.measurement.vbat.gain": 1.5},
                "publications": {"power_data": {"vbat": 22.0}},
            })
        """
        payload = config or {}
        registers = dict(payload.get("registers") or {})
        publications: dict[str, dict[str, Any]] = {}
        # Build per-port field dicts from profile defaults, then apply caller overrides.
        for spec in self.publication_specs():
            port_config = dict(spec.default_fields)
            incoming = (payload.get("publications") or {}).get(spec.port_name)
            if isinstance(incoming, dict):
                port_config.update(incoming)
            publications[spec.port_name] = port_config

        node_info = payload.get("node_info") or {}
        unique_id = _parse_unique_id(node_info.get("unique_id", payload.get("unique_id")))
        software_vcs_revision_id = node_info.get("software_vcs_revision_id", payload.get("software_vcs_revision_id"))
        software_image_crc = node_info.get("software_image_crc", payload.get("software_image_crc"))
        hardware_version = _parse_version(node_info.get("hardware_version", payload.get("hardware_version")))
        software_version = _parse_version(node_info.get("software_version", payload.get("software_version")))

        return EmulationNodeConfig(
            registers=registers,
            publications=publications,
            unique_id=unique_id,
            software_vcs_revision_id=software_vcs_revision_id,
            software_image_crc=software_image_crc,
            hardware_version=hardware_version,
            software_version=software_version,
        )

    def build_node_info(self, config: EmulationNodeConfig) -> pycyphal.application.NodeInfo:
        """Build the GetInfo payload for one emulated node."""
        unique_id = config.unique_id if config.unique_id is not None else secrets.token_bytes(16)
        info = pycyphal.application.NodeInfo(
            name=self.cyphal_name,
            hardware_version=config.hardware_version or self.default_hardware_version,
            software_version=config.software_version or self.default_software_version,
            unique_id=unique_id,
        )
        if config.software_vcs_revision_id is not None:
            info.software_vcs_revision_id = int(config.software_vcs_revision_id)
        if config.software_image_crc is not None:
            info.software_image_crc = [int(value) for value in config.software_image_crc]
        return info

    @abc.abstractmethod
    def start_background_tasks(
        self,
        emulated_node: EmulatedCyphalNode,
        config: EmulationNodeConfig,
    ) -> list[Any]:
        """Start publishers/subscribers; return asyncio tasks.

        Tasks are cancelled automatically when :meth:`EmulatedCyphalNode.stop` runs.
        Read live publication values from ``emulated_node.config.publications`` so
        callers can mutate fields at runtime (e.g. change ``ibat`` during a test).
        """

    async def handle_execute_command(
        self,
        emulated_node: EmulatedCyphalNode,
        command: int,
        parameter: bytes,
        client_node_id: int,
    ) -> tuple[int, str | bytes | None]:
        """Handle device-specific execute commands.

        Return ``(status, output)`` using
        :data:`uavcan.node.ExecuteCommand_1_3.Response` status constants.
        Store persistent emulation state on ``emulated_node.state``.
        """
        return (
            uavcan.node.ExecuteCommand_1_3.Response.STATUS_BAD_COMMAND,
            "Command not implemented",
        )


def _parse_unique_id(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return bytes.fromhex(value)
    raise TypeError(f"unique_id must be bytes or hex string, got {type(value).__name__}")


def _parse_version(value: Any) -> uavcan.node.Version_1 | None:
    if value is None:
        return None
    if isinstance(value, uavcan.node.Version_1):
        return value
    if isinstance(value, dict):
        return uavcan.node.Version_1(major=int(value["major"]), minor=int(value["minor"]))
    raise TypeError(f"version must be Version_1 or dict, got {type(value).__name__}")
