"""Production emulated Cyphal node."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

import pycyphal.application
import pycyphal.transport.redundant
import uavcan.node
from pycyphal.application.register import Natural16, String, Value
from uavcan.node import ExecuteCommand_1_3

from .base import DeviceEmulationProfile, EmulationNodeConfig
from .local_registry import configure_can_registers


class EmulatedCyphalNode:
    """One emulated Cyphal device node on a shared CAN interface.

    Each instance is a full :mod:`pycyphal` application node with its own transport
    attachment on ``interface``. Multiple emulated nodes can coexist on the same
    physical interface (same pattern as integration-test mocks).

    Lifecycle:

    1. Construct with a :class:`~cyphal_device_library.emulation.base.DeviceEmulationProfile`.
    2. Call :meth:`start` to open the transport and start profile background tasks.
    3. Call :meth:`stop` to cancel tasks and close the node.

    Use :attr:`state` for profile-specific emulation data (e.g. RTC reference time).
    Prefer :func:`~cyphal_device_library.emulation.factory.create_emulated_node` over
    constructing this class directly.

    Example::

        profile = MyProfile()
        config = profile.merge_add_config(None)
        emulated = create_emulated_node(profile, node_id=50, interface="virtual:", config=config)
        emulated.start()
        try:
            ...  # discover with a separate Client on the same interface
        finally:
            await emulated.stop()
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
    ) -> None:
        self.profile = profile
        self.node_id = node_id
        self.interface = interface
        self.config = config
        self.state: dict[str, Any] = {}
        self._background_tasks: list[asyncio.Task[Any]] = []

        # Node identity reported in GetInfo and heartbeats.
        info = pycyphal.application.NodeInfo(
            name=profile.cyphal_name,
            hardware_version=profile.default_hardware_version,
            software_version=profile.default_software_version,
            unique_id=secrets.token_bytes(16),
        )

        # Local registry: transport settings + profile defaults + caller overrides.
        registry = pycyphal.application.make_registry()
        registry["uavcan.node.id"] = self.get_node_id, self.set_node_id
        registry["uavcan.node.description"] = String(profile.description)
        configure_can_registers(registry, interface, bitrate=bitrate, mtu=mtu)

        for name, value in profile.default_registers().items():
            registry[name] = value

        self.node = pycyphal.application.make_node(info, registry, reconfigurable_transport=True)
        profile.apply_register_overrides(self.node, config)

        # Standard Cyphal services every emulated node exposes.
        self.srv_execute_command = self.node.get_server(uavcan.node.ExecuteCommand_1_3)
        self.srv_execute_command.serve_in_background(self._serve_execute_command)

        self.node.heartbeat_publisher.health = uavcan.node.Health_1.NOMINAL
        self.node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL
        self.node.heartbeat_publisher.vendor_specific_status_code = 0
        self.node.heartbeat_publisher._started_at = time.monotonic() - initial_uptime

        self._reconfigure_transport_task: asyncio.Task[None] | None = None

    def get_node_id(self) -> Natural16:
        """Return the current node ID as a pycyphal register getter."""
        return Natural16([self.node_id])

    def set_node_id(self, value: Value) -> None:
        """Handle uavcan.node.id register writes with delayed transport reconfiguration."""
        if value.natural16 is None:
            raise TypeError(f"Invalid node ID: expected Natural16, got {value}")
        self.node_id = value.natural16.value[0]
        self._reconfigure_transport_delayed()

    def _reconfigure_transport_delayed(self, delay: float = 0.01) -> None:
        """Reattach transport after a node-ID change.

        A short delay lets the register-access response go out on the old node ID
        before the underlying transport is recreated. See OpenCyphal forum guidance on
        reconfigurable transports after ``uavcan.node.id`` changes.
        """

        async def _reconfigure() -> None:
            await asyncio.sleep(delay)
            transport = self.node.presentation.transport
            assert isinstance(transport, pycyphal.transport.redundant.RedundantTransport), "Not reconfigurable"
            # Tear down existing inferiors before attaching a fresh transport.
            while transport.inferiors:
                transport.detach_inferior(transport.inferiors[0])
            new_transport = pycyphal.application.make_transport(self.node.registry)
            if new_transport is None:
                raise RuntimeError("Failed to create new transport")
            transport.attach_inferior(new_transport)

        if self._reconfigure_transport_task is None or self._reconfigure_transport_task.done():
            self._reconfigure_transport_task = asyncio.create_task(_reconfigure())
        else:
            raise RuntimeError("Transport reconfiguration task is already running")

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

    def publication_summary(self) -> list[dict[str, Any]]:
        """Return publication port metadata for status snapshots."""
        return [spec.to_dict() for spec in self.profile.publication_specs()]

    def registers_summary(self) -> dict[str, Any]:
        """Return a JSON-safe summary of register overrides applied at add time."""
        return dict(self.config.registers)
