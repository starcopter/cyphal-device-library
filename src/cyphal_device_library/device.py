import asyncio
import contextlib
import importlib
import logging
import re
from pathlib import Path
from typing import AsyncGenerator, Type, TypeVar

import pycyphal
import pycyphal.application
import uavcan.node

from .client import Client
from .registry import NativeValue, Registry

logger = logging.getLogger(__name__)
MessageClass = TypeVar("MessageClass")


class Device:
    def __init__(self, client: Client, node_id: int, discover_registers: bool | list[str] = True) -> None:
        self.client = client
        self.registry = Registry(None, self.client.node.make_client)

        self.node_id = node_id
        self._initialized = asyncio.Event()
        self._info: uavcan.node.GetInfo_1.Response | None = None

        async def initialize():
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.get_info())
                if isinstance(discover_registers, list):
                    for name in discover_registers:
                        tg.create_task(self.registry.refresh_register(name, full=True))
                elif discover_registers:
                    tg.create_task(self.registry.discover_registers())

            self._initialized.set()

        asyncio.get_event_loop().create_task(initialize())

    @classmethod
    async def discover(
        cls,
        client: Client,
        name: str | None = None,
        uid: str | bytes | None = None,
        *,
        timeout: float = 3.0,
        **kwargs,
    ) -> "Device":
        node_id = await discover_device_node_id(client, name, uid, timeout=timeout)
        return cls(client, node_id, **kwargs)

    async def __aenter__(self) -> "Device":
        await self.client.__aenter__()
        await self._initialized.wait()
        return self

    async def __aexit__(self, exc_t, exc_v, exc_tb) -> None:
        await self.client.__aexit__(None, None, None)

    @property
    def node_id(self) -> int:
        """Device Under Test Node ID."""
        return self._node_id

    @node_id.setter
    def node_id(self, value: int) -> None:
        if value == self.client.node.id:
            raise ValueError("Device under test cannot be the same as own node ID")
        self._node_id = value
        self.registry.node_id = value

    @property
    def info(self) -> uavcan.node.GetInfo_1.Response | None:
        """Node info, gathered from the node tracker."""
        _, node_tracker_info = self.client.node_tracker.registry.get(self.node_id, (None, None))
        return node_tracker_info or self._info

    @property
    def heartbeat(self) -> uavcan.node.Heartbeat_1 | None:
        """Last received heartbeat."""
        heartbeat, _ = self.client.node_tracker.registry.get(self.node_id, (None, None))
        return heartbeat

    @property
    def uptime(self) -> int:
        """Uptime of the device under test, in seconds."""
        try:
            return self.heartbeat.uptime
        except AttributeError:
            # no heartbeat yet
            return 0

    async def get_info(self, refresh: bool = False) -> pycyphal.application.NodeInfo:
        """Wait for the node info of the device under test to be available."""
        if refresh or self._info is None:
            self._info = await self.client.get_info(self.node_id)
        return self._info

    async def get_app_name(self) -> str:
        """Get the application name of the device under test."""
        info = await self.get_info()
        return info.name.tobytes().decode()

    async def get_device_uid(self) -> str:
        """Get the unique ID of the device under test."""
        info = await self.get_info()
        return info.unique_id.tobytes().hex()

    async def execute(self, command: uavcan.node.ExecuteCommand_1.Request) -> uavcan.node.ExecuteCommand_1.Response:
        """Execute a command on the device under test.

        Args:
            command: The command to execute.

        Returns:
            The response from the device under test.
        """
        return await self.client.execute_command(command, server_node_id=self.node_id)

    async def restart(self, wait: bool = True, timeout: float = 1.0) -> float:
        """Restart the device under test.

        Args:
            wait: Whether to wait for the device under test to restart.
            timeout: The timeout in seconds to wait for the device under test to restart.

        Returns:
            If `wait` is True, the time the DUT took to come back online.
            If `wait` is False, the time until the DUT took to respond to the request.
        """
        return await self.client.restart_node(self.node_id, wait, timeout)

    async def update(self, image: Path, wait: bool = True, timeout: float = 5.0) -> float:
        """Update the firmware of the device under test.

        Args:
            image: Path to the firmware image file.
            wait: Whether to wait for the device to restart after the update.
            timeout: Maximum time in seconds to wait for the device to restart.

        Returns:
            float: Time taken for the update process to complete.
        """
        return await self.client.update(self.node_id, image, wait, timeout)

    async def write_register(self, register_name: str, value: NativeValue) -> NativeValue:
        """Write a value to a register on the device under test.

        Args:
            register_name: Name of the register to write to.
            value: Value to write to the register. Must be compatible with the register's type.

        Returns:
            NativeValue: The value that was written to the register.

        Raises:
            KeyError: If the register does not exist.
            TypeError: If the registter is immutable or if the value is not compatible with the register's type.
            AssertionError: If the write operation fails.
        """
        register = self.registry[register_name]  # this may raise a KeyError
        logger.debug(f"setting {register_name} to {value}...")
        success = await register.set_value(value)
        assert success
        return register.value

    async def reset_register(self, register_name: str) -> NativeValue:
        """Reset a register to its default value.

        Args:
            register_name: Name of the register to reset.

        Returns:
            NativeValue: The default value that was written to the register.

        Raises:
            KeyError: If the register does not exist.
            AttributeError: If the register has no configured default value.
        """
        register = self.registry[register_name]  # this may raise a KeyError
        if not register.has_default:
            raise AttributeError(f"{register_name} has no configured default")
        return await self.write_register(register_name, register.default)

    async def read_register(self, register_name: str, refresh: bool = False) -> NativeValue:
        """Read the current value of a register.

        Args:
            register_name: Name of the register to read.
            refresh: Whether to force a refresh of the register value from the device.

        Returns:
            NativeValue: The current value of the register.

        Raises:
            KeyError: If the register does not exist.
        """
        register = self.registry[register_name]  # this may raise a KeyError
        if refresh:
            await register.refresh()
        return register.value

    async def set_node_id(self, node_id: int) -> None:
        """Set the node ID of the device under test.

        This first configures the node ID on the device under test, and then updates the
        `dut` property to reflect the new node ID.

        Args:
            node_id: The new node ID to set.

        Raises:
            RuntimeError: If setting the node ID fails.
        """
        register = self.registry["uavcan.node.id"]
        if await register.set_value(node_id):
            self.node_id = node_id
        else:
            raise RuntimeError(f"Failed to set node ID to {node_id}")

    @contextlib.asynccontextmanager
    async def temporary_node_id(self, node_id: int) -> AsyncGenerator[None, None]:
        """Temporarily change the node ID of the device under test.

        This is a context manager that will restore the original node ID when exiting.

        Args:
            node_id: The temporary node ID to use.

        Example:
            >>> previous_node_id = client.dut
            >>> async with client.temporary_node_id(42):
            ...     assert client.dut == 42
            ...     # do something with the device
            ...     print(1 / 0)
            ... except ZeroDivisionError:
            ...     pass
            >>> assert client.dut == previous_node_id
        """
        previous_value = await self.read_register("uavcan.node.id")
        if previous_value != node_id:
            await self.set_node_id(node_id)
        try:
            yield
        finally:
            await self.set_node_id(previous_value)

    def _get_port_id(self, port_name: str) -> int:
        port_id = self.registry[f"uavcan.pub.{port_name}.id"].value
        assert isinstance(port_id, int)
        return port_id

    def _get_port_type(self, port_name: str) -> Type[MessageClass]:
        port_type = self.registry[f"uavcan.pub.{port_name}.type"].value
        assert isinstance(port_type, str)

        match = re.match(
            r"(?P<namespace>[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*)\."
            r"(?P<shortname>[a-zA-Z_][a-zA-Z0-9_]*)\.(?P<major>\d+)\.(?P<minor>\d+)",
            port_type,
        )
        assert match

        try:
            namespace = importlib.import_module(match.group("namespace"))
            # Cyphal Specification v1.0, page 42, section 3.8.3.2 "Versioning principles":
            #
            #   In order to ensure a deterministic application behavior and ensure a robust migration path as
            #   data type definitions evolve, all data type definitions that share the same full name and the
            #   same major version number shall be semantically compatible with each other.
            #
            #   An exception to the above rules applies when the major version number is zero. Data type
            #   definitions bearing the major version number of zero are not subjected to any compatibility
            #   requirements.
            #
            # Therefore, to reach maximum compatibility, we will attempt to load the exact type (major.minor)
            # for data types with a major version number of zero, and the newest major type for data types
            # with a major version number greater than zero.

            py_type_name = "_".join(
                match.group("shortname", "major")
                if int(match.group("major")) > 0
                else match.group("shortname", "major", "minor")
            )
            Message: Type[MessageClass] = getattr(namespace, py_type_name)
        except (ImportError, AttributeError) as ex:
            raise RuntimeError(f"Port {port_name}: No matching type found for {port_type}") from ex

        return Message

    def get_subscription(self, port_name: str) -> pycyphal.presentation.Subscriber:
        """Get a subscriber for a specific port on the device under test.

        Args:
            port_name: Name of the port to subscribe to.

        Returns:
            pycyphal.presentation.Subscriber: A subscriber instance for the specified port.

        Raises:
            RuntimeError: If the port type cannot be found or loaded.

        Example:
            >>> from starcopter.aeric import Navlight_0_1
            >>> subscriber = client.get_subscription("navlights_feedback")
            >>> assert subscriber.dtype == Navlight_0_1
            >>> async for message, _ in subscriber:
            ...     print(message)
            ...     break
            starcopter.aeric.Navlight.0.1(brightness=300, color=2)
        """
        port_id = self._get_port_id(port_name)
        port_type = self._get_port_type(port_name)

        return self.node.make_subscriber(port_type, port_id)


async def discover_device_node_id(
    client: Client,
    name: str | None = None,
    uid: str | bytes | None = None,
    *,
    timeout: float = 3.0,
) -> int:
    """Discover a device on the network when its node ID is not known.

    Args:
        client: The client to use to discover the device.
        name: The name of the device to discover.
        uid: The unique ID of the device to discover.
    """
    from pycyphal.application.node_tracker import Entry

    if not name and not uid:
        raise ValueError("Either name or UID must be provided")
    if isinstance(uid, str):
        uid: bytes = bytes.fromhex(uid)

    loop = asyncio.get_event_loop()
    fut_node_id: asyncio.Future[int] = loop.create_future()

    def _matches(entry: Entry) -> bool:
        if name is not None and entry.info.name.tobytes().decode() != name:
            return False
        if uid is not None and entry.info.unique_id.tobytes() != uid:
            return False

        return True

    def _discover(node_id: int, _old: Entry | None, entry: Entry | None) -> None:
        if fut_node_id.done() or entry is None or entry.info is None:
            return

        if _matches(entry):
            fut_node_id.set_result(node_id)

    for node_id, entry in client.node_tracker.registry.items():
        if _matches(entry):
            # matching device already in the registry, no need to wait
            dut_node_id = node_id
            break
    else:
        # no matching device known yet, wait for it to appear
        client.node_tracker.add_update_handler(_discover)
        try:
            dut_node_id = await asyncio.wait_for(fut_node_id, timeout)

        except asyncio.TimeoutError as ex:
            attrs = []
            if name is not None:
                attrs.append(f"name={name}")
            if uid is not None:
                attrs.append(f"UID={uid.hex()}")
            raise TimeoutError(f"Failed to discover device with {', '.join(attrs)} within {timeout} seconds") from ex

        finally:
            client.node_tracker.remove_update_handler(_discover)

    entry = client.node_tracker.registry[dut_node_id]
    logger.info(
        "Found device under test: node ID %d, name %s, UID %s",
        dut_node_id,
        entry.info.name.tobytes().decode(),
        entry.info.unique_id.tobytes().hex(),
    )

    return dut_node_id
