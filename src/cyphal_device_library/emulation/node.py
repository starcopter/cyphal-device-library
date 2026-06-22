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

from .base import DeviceEmulationProfile, EmulationNodeConfig
from .local_registry import (
    apply_native_register_overrides,
    configure_can_registers,
    configure_standard_service_registers,
)


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
        """Start the node transport and profile background tasks."""
        self.node.start()
        self._background_tasks = self.profile.start_background_tasks(self, self.config)

    async def stop(self) -> None:
        """Cancel background tasks and close the node."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self.node.close()

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
