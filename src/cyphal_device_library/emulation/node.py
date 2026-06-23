"""Production emulated Cyphal node."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pycyphal.application
import pycyphal.transport
import uavcan.node
from pycyphal.application.register import Natural16, String
from uavcan.node import ExecuteCommand_1_3

from ..util.message_types import load_message_type
from .base import DeviceEmulationProfile, EmulationNodeConfig
from .local_registry import (
    apply_native_register_overrides,
    configure_can_registers,
    configure_standard_service_registers,
)
from .publication_spec import PublicationPortSpec

# Fallback publication period when neither the register nor the spec declares one.
_DEFAULT_PUBLICATION_DT_MS = 1000


class EmulatedCyphalNode:
    """One emulated Cyphal device node with its own registry and publishers.

    Each node exposes the standard Cyphal services ``uavcan.node.GetInfo`` and
    ``uavcan.register`` (list/access) through :mod:`pycyphal`, using the local
    register map declared by the device profile.

    Pass a pre-built :class:`~pycyphal.transport.can.CANTransport` when several
    emulated devices share one CAN interface.
    """

    def __init__(
        self,
        profile: DeviceEmulationProfile,
        node_id: int,
        interface: str,
        config: EmulationNodeConfig,
        *,
        initial_uptime: float = 10.0,
        bitrate: int | list[int] | None = None,
        mtu: int | None = None,
        transport: pycyphal.transport.Transport | None = None,
    ) -> None:
        if node_id < 0 or node_id > 127:  # noqa: PLR2004
            raise ValueError("node_id must be between 0 and 127")

        self.profile = profile
        self.node_id = node_id
        self.interface = interface
        self.config = config
        self.state: dict[str, Any] = {}
        self._background_tasks: list[asyncio.Task[Any]] = []

        info = profile.build_node_info(config)

        registry = pycyphal.application.make_registry()
        registry["uavcan.node.id"] = self.get_node_id
        registry["uavcan.node.description"] = String(profile.description)
        configure_can_registers(registry, interface, bitrate=bitrate, mtu=mtu)
        configure_standard_service_registers(registry)

        for name, value in profile.default_registers().items():
            registry[name] = value

        self.node = pycyphal.application.make_node(
            info,
            registry,
            transport=transport,
            reconfigurable_transport=False,
        )
        apply_native_register_overrides(self.node.registry, config.registers)

        self.srv_execute_command = self.node.get_server(uavcan.node.ExecuteCommand_1_3)
        self.srv_execute_command.serve_in_background(self._serve_execute_command)

        self.node.heartbeat_publisher.health = uavcan.node.Health_1.NOMINAL
        self.node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL
        self.node.heartbeat_publisher.vendor_specific_status_code = 0
        self.node.heartbeat_publisher._started_at = time.monotonic() - initial_uptime

    def get_node_id(self) -> Natural16:
        """Return the fixed node ID as a read-only pycyphal register getter."""
        return Natural16([self.node_id])

    def start(self) -> None:
        """Start the node transport and generic publication loops."""
        self.node.start()
        self._background_tasks = self._start_publication_loops()

    async def stop(self) -> None:
        """Cancel background tasks and close the node."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self.node.close()

    def _start_publication_loops(self) -> list[asyncio.Task[Any]]:
        """Create one publisher and periodic publish task per publication port."""
        tasks: list[asyncio.Task[Any]] = []
        for spec in self.profile.publication_specs():
            message_type = load_message_type(spec.type_name)
            publisher = self.node.make_publisher(message_type, spec.port_name)
            tasks.append(
                asyncio.create_task(
                    self._publication_loop(publisher, spec),
                    name=f"emulate-{self.profile.device_type}-{self.node_id}-{spec.port_name}",
                )
            )
        return tasks

    async def _publication_loop(self, publisher: Any, spec: PublicationPortSpec) -> None:
        """Publish ``spec.port_name`` on the configured interval until cancelled."""
        try:
            while True:
                fields = self.config.publications.get(spec.port_name, {})
                message = self.profile.build_message(spec.port_name, fields, self)
                await publisher.publish(message)
                await asyncio.sleep(self._publication_dt_ms(spec) / 1000)
        except asyncio.CancelledError:
            pass

    def _publication_dt_ms(self, spec: PublicationPortSpec) -> int:
        """Resolve the live publication period from the register, then the spec."""
        register_name = f"uavcan.pub.{spec.port_name}.dt_ms"
        if register_name in self.node.registry:
            return int(self.node.registry[register_name])
        return spec.dt_ms if spec.dt_ms is not None else _DEFAULT_PUBLICATION_DT_MS

    async def _serve_execute_command(
        self,
        request: ExecuteCommand_1_3.Request,
        metadata: pycyphal.presentation.ServiceRequestMetadata,
    ) -> ExecuteCommand_1_3.Response:
        """Dispatch execute commands to built-in handlers, then the profile."""
        match request.command:
            case ExecuteCommand_1_3.Request.COMMAND_RESTART:
                self.node.heartbeat_publisher._started_at = time.monotonic()
                return ExecuteCommand_1_3.Response(ExecuteCommand_1_3.Response.STATUS_SUCCESS)

        status, output = await self.profile.handle_execute_command(
            self,
            request.command,
            request.parameter.tobytes(),
            metadata.client_node_id,
        )
        return ExecuteCommand_1_3.Response(status, output)
