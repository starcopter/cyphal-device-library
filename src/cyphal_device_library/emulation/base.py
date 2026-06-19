"""Abstract emulation profile for Cyphal device types."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pycyphal.application
import uavcan.node

from .local_registry import apply_native_register_overrides
from .publication_spec import PublicationPortSpec

if TYPE_CHECKING:
    from .node import EmulatedCyphalNode

# pycyphal local registry values (String, Natural16, Real32, …), not uavcan.register.Value_1.
RegisterMap = dict[str, Any]


@dataclass
class EmulationNodeConfig:
    """Runtime configuration for one emulated node.

    Produced by :meth:`DeviceEmulationProfile.merge_add_config` and passed to
    :func:`~cyphal_device_library.emulation.factory.create_emulated_node`.

    Attributes:
        registers: Register name → native value overrides applied after defaults.
        publications: Port name → field dict merged onto profile publication defaults.
    """

    registers: RegisterMap = field(default_factory=dict)
    publications: dict[str, dict[str, Any]] = field(default_factory=dict)


class DeviceEmulationProfile(abc.ABC):
    """Declare registers, publications, and bus behavior for one device type.

    Subclass this to describe how an emulated node behaves on the CAN bus.
    Profiles are consumed by :class:`~cyphal_device_library.emulation.node.EmulatedCyphalNode`
    and :class:`~cyphal_device_library.emulation.host.EmulatedNodeHost`.

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
        return EmulationNodeConfig(registers=registers, publications=publications)

    def apply_register_overrides(
        self,
        node: pycyphal.application.Node,
        config: EmulationNodeConfig,
    ) -> None:
        """Apply JSON register overrides onto a running node registry."""
        apply_native_register_overrides(node.registry, config.registers)

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
