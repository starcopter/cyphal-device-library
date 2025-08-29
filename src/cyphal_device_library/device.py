import asyncio
import contextlib
import importlib
import logging
import re
from collections.abc import AsyncGenerator, Container, Iterable
from pathlib import Path
from typing import Self, Type, TypeVar

import pycyphal
import uavcan.node

from .client import Client
from .registry import NativeValue, Registry

logger = logging.getLogger(__name__)
MessageClass = TypeVar("MessageClass")


class Device:
    """A wrapper around a single device on a Cyphal bus.

    This class provides a convenient interface for interacting with a single device on a Cyphal bus.
    It provides easy access to many common device operations:

    - Reading and writing registers
    - Executing commands, including restarting the device
    - Performing firmware updates
    - Subscribing to device ports, including discovering and decoding the data type

    The device is identified by its node ID, which must be different from the client's own node ID.

    Example:
        >>> async with Client("my_client") as client, Device(client, node_id=42) as device:
        ...     # Do something with the device
        ...     info = await device.get_info()
        ...     print(info)
        ...     await device.restart()

    If the node ID is not known, the device can be discovered using the Device.discover() class method instead:

        >>> async with (
        ...     Client("my_client") as client,
        ...     await Device.discover(client, name="com.starcopter.highdra.bms") as device,
        ... ):
        ...     print(await device.get_info())
    """

    DEFAULT_NAME: str | None = None
    """Device name, used for discovery.

    This is a class variable that can be overridden by subclasses to provide a default name for device discovery.
    Has no effect outside of device discovery, has no effect if overridden by the `name` argument to Device.discover().
    """

    DEFAULT_RESTART_TIMEOUT: float = 1.0
    """Default timeout to wait for the device to come back online after restart.

    This is a class variable that can be overridden by subclasses to provide a default timeout for device restart.
    Has no effect outside of device restart, has no effect if overridden by the `timeout` argument to Device.restart().
    """

    def __init__(
        self,
        client: Client,
        node_id: int,
        discover_registers: bool | Iterable[str] = True,
    ) -> None:
        """Initialize a new Device instance.

        Args:
            client: The Client instance to use for bus interaction.
            node_id: The node ID of the device to interact with. Must be different from the client's own node ID.
            discover_registers: Whether to automatically discover registers on the device.
                If `True`, all registers will be discovered.
                If a list of strings, only the specified registers will be looked up.
                If `False`, no registers will be discovered automatically.

        Raises:
            ValueError: If the node_id is the same as the client's own node ID.
        """
        if node_id == client.node.id:
            raise ValueError("Device node_id cannot be the same as client node ID")

        self.client = client
        self.registry = Registry(node_id, self.client.node.make_client)

        self._node_id = node_id
        self._initialized = asyncio.Event()
        self._info: uavcan.node.GetInfo_1.Response | None = None

        async def initialize():
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.get_info())
                if isinstance(discover_registers, Iterable):
                    for name in discover_registers:
                        tg.create_task(self.registry.refresh_register(name, full=True))
                elif discover_registers:
                    tg.create_task(self.registry.discover_registers())

            self._initialized.set()

        asyncio.get_event_loop().create_task(initialize())

    async def wait_for_initialization(self, timeout: float | None = None) -> None:
        """Wait for the device to be initialized.

        Args:
            timeout: Maximum time in seconds to wait for the device to be initialized.
                If `None`, wait indefinitely.
        """
        async with asyncio.timeout(timeout):
            await self._initialized.wait()

    @classmethod
    async def discover(
        cls,
        client: Client,
        name: str | None = None,
        uid: str | bytes | None = None,
        exclude_uids: Container[str | bytes] | None = None,
        *,
        timeout: float = 3.0,
        **kwargs,
    ) -> Self:
        """Discover a device on the network and create a Device instance for it.

        Search for a device on the network based on its name and/or UID, and return a Device instance configured to
        interact with that device. At least one of `name` or `uid` must be provided.

        Args:
            client: The Client instance to use for communication.
            name: The name of the device to discover. If provided, the first device with a matching name will be used.
            uid: The unique ID of the device to discover. Can be provided as a string (hex format) or bytes.
            exclude_uids: A container (set, list, ...) of unique IDs to exclude from discovery.
            timeout: Maximum time in seconds to wait for device discovery.
            **kwargs: Additional arguments to pass to the Device constructor.

        Returns:
            Device: A Device instance configured to interact with the discovered device.

        Raises:
            ValueError: If neither name nor uid is provided.
            TimeoutError: If no matching device is found within the timeout period.

        Example:
            >>> async with Client("my_client") as client:
            ...     # Discover by name
            ...     device = await Device.discover(client, name="my_device")
            ...     # Discover by UID
            ...     device = await Device.discover(client, uid="1234567890abcdef")

        This can also be used to initialize multiple devices at once:
            >>> async with (
            ...     Client("my_client") as client,
            ...     await Device.discover(client, name="com.starcopter.aeric.mmb") as mmb,
            ...     await Device.discover(client, name="com.zubax.telega") as telega,
            ... ):
            ...     print(mmb.registry)
            ...     print(telega.registry)
        """
        node_id = await discover_device_node_id(client, name or cls.DEFAULT_NAME, uid, exclude_uids, timeout=timeout)
        return cls(client, node_id, **kwargs)

    async def __aenter__(self) -> Self:
        """Async context manager entry point.

        This method ensures the client is properly started and the device is initialized
        before returning the Device instance.

        Returns:
            Device: The Device instance ready for use.

        Example:
            >>> async with Device(client, node_id=42) as device:
            ...     # Device is ready for use
            ...     print(device.registry)
        """
        await self.client.__aenter__()
        await self.wait_for_initialization()
        return self

    async def __aexit__(self, exc_t, exc_v, exc_tb) -> None:
        """Async context manager exit point."""
        await self.client.__aexit__(None, None, None)

    @property
    def node_id(self) -> int:
        """Node ID of the device.

        This is a read-only property, use the async set_node_id() method to modify the device's node ID.
        """
        return self._node_id

    @property
    def info(self) -> uavcan.node.GetInfo_1.Response | None:
        """Get cached device info.

        To force an update, use the async get_info() method instead.
        """
        # The node tracker's information is usually more up to date than the locally cached information.
        _heartbeat, info = self.client.node_tracker.registry.get(self.node_id, (None, None))
        if info is not None:
            self._info = info
        return self._info

    @property
    def heartbeat(self) -> uavcan.node.Heartbeat_1 | None:
        """Last received heartbeat from the device."""
        heartbeat, _info = self.client.node_tracker.registry.get(self.node_id, (None, None))
        return heartbeat

    @property
    def uptime(self) -> int:
        """Device's uptime in seconds."""
        return self.heartbeat.uptime if self.heartbeat else 0

    async def get_info(self, refresh: bool = False) -> uavcan.node.GetInfo_1.Response:
        """Get node info of the device.

        Args:
            refresh: Whether to force a refresh of the node info from the device.

        Returns:
            uavcan.node.GetInfo_1.Response: The node information.

        Raises:
            TimeoutError: If the request times out.
        """
        if refresh or self._info is None:
            self._info = await self.client.get_info(self.node_id)
        return self._info

    async def get_app_name(self) -> str:
        """Get the application name of the device.

        Returns:
            str: The application name as a string.

        Raises:
            TimeoutError: If the node info request times out.
        """
        info = await self.get_info()
        return info.name.tobytes().decode()

    async def get_device_uid(self) -> str:
        """Get the unique ID of the device.

        Returns:
            str: The unique ID as a hexadecimal string.

        Raises:
            TimeoutError: If the node info request times out.
        """
        info = await self.get_info()
        return info.unique_id.tobytes().hex()

    async def execute(self, command: uavcan.node.ExecuteCommand_1.Request) -> uavcan.node.ExecuteCommand_1.Response:
        """Execute a command on the device.

        This method sends an ExecuteCommand request to the device and waits for the response.

        Args:
            command: The command to execute on the device.

        Returns:
            uavcan.node.ExecuteCommand_1.Response: The response from the device.

        Raises:
            TimeoutError: If the command execution times out.

        Example:
            >>> command = uavcan.node.ExecuteCommand_1.Request(
            ...     uavcan.node.ExecuteCommand_1.Request.COMMAND_IDENTIFY
            ... )
            >>> response = await device.execute(command)
            >>> print(response.status)
            0
        """
        return await self.client.execute_command(command, server_node_id=self.node_id)

    async def restart(self, wait: bool = True, timeout: float | None = None) -> float:
        """Restart the device.

        This method sends a restart command to the device and optionally waits for it to come back online.

        Args:
            wait: Whether to wait for the device to restart and come back online.
            timeout: Maximum time in seconds to wait for the device to restart.

        Returns:
            float: The time taken for the restart operation in seconds.

        Raises:
            RuntimeError: If the restart request fails.
            TimeoutError: If waiting for the device to restart times out.

        Example:
            >>> duration = await device.restart(wait=True, timeout=5.0)
            >>> print(f"Device restarted in {duration:.2f} seconds")
            Device restarted in 1.23 seconds
        """
        # TODO: clear (refresh?) the registry, else it will contain stale information
        return await self.client.restart_node(self.node_id, wait, timeout or self.DEFAULT_RESTART_TIMEOUT)

    async def update(self, image: Path, wait: bool = True, timeout: float = 5.0) -> float:
        """Update the firmware of the device.

        This method initiates a software update on the device using the provided firmware image.

        Args:
            image: Path to the firmware image file.
            wait: Whether to wait for the device to restart after the update.
            timeout: Maximum time in seconds to wait for the device to restart.

        Returns:
            float: The time taken for the update process to complete in seconds.

        Raises:
            RuntimeError: If the update request fails.
            TimeoutError: If waiting for the update to complete times out.

        Example:
            >>> duration = await device.update(Path("firmware.bin"), wait=True, timeout=10.0)
            >>> print(f"Update completed in {duration:.2f} seconds")
            Update completed in 3.45 seconds
        """
        return await self.client.update(self.node_id, image, wait, timeout)

    async def write_register(self, register_name: str, value: NativeValue) -> NativeValue:
        """Write a value to a register on the device.

        This method writes a value to a register on the device and returns the value that was actually written
        (which may be different from the requested value due to constraints or validation).

        Args:
            register_name: Name of the register to write to.
            value: Value to write to the register. Must be compatible with the register's type.

        Returns:
            NativeValue: The value that was actually written to the register.

        Raises:
            KeyError: If the register does not exist.
            TypeError: If the register is immutable or if the value is not compatible with the register's type.
            AssertionError: If the write operation fails.

        Example:
            >>> await device.write_register("uavcan.node.description", "My device")
            'My device'
        """
        register = self.registry[register_name]  # this may raise a KeyError
        logger.debug("setting %s to %s...", register_name, value)
        success = await register.set_value(value)
        assert success
        return register.value

    async def reset_register(self, register_name: str) -> NativeValue:
        """Reset a register to its default value.

        This method resets a register to its configured default value. This only works if the register has a
        default value configured.

        Args:
            register_name: Name of the register to reset.

        Returns:
            NativeValue: The default value that was written to the register.

        Raises:
            KeyError: If the register does not exist.
            AttributeError: If the register has no configured default value.

        Example:
            >>> await device.reset_register("uavcan.node.description")
            ''
        """
        register = self.registry[register_name]  # this may raise a KeyError
        if not register.has_default:
            raise AttributeError(f"{register_name} has no configured default")
        return await self.write_register(register_name, register.default)

    async def read_register(self, register_name: str, refresh: bool = False) -> NativeValue:
        """Read the current value of a register.

        This method reads the current value of a register from the device. The value is cached unless refresh is True.

        Args:
            register_name: Name of the register to read.
            refresh: Whether to force a refresh of the register value from the device.

        Returns:
            NativeValue: The current value of the register.

        Raises:
            KeyError: If the register does not exist.

        Example:
            >>> value = await device.read_register("uavcan.node.id")
            >>> print(f"Current node ID: {value}")
            Current node ID: 42
            >>> # Force refresh from device
            >>> value = await device.read_register("uavcan.node.id", refresh=True)
        """
        register = self.registry[register_name]  # this may raise a KeyError
        if refresh:
            await register.refresh()
        return register.value

    async def set_node_id(self, node_id: int) -> None:
        """Set the node ID of the device.

        This method configures the node ID on the device, and updates all required local references to be able to
        continue to interact with the device.

        Args:
            node_id: The new node ID to set.

        Raises:
            RuntimeError: If setting the node ID fails.

        Example:
            >>> await device.set_node_id(43)
            >>> print(device.node_id)
            43
        """
        if node_id == self.client.node.id:
            raise ValueError("Device cannot be the same as own node ID")
        if node_id == self.node_id:
            logger.debug("Node ID is already set to %d", node_id)
            return

        register = self.registry["uavcan.node.id"]
        if await register.set_value(node_id):
            self._node_id = node_id
            self.registry.node_id = node_id
        else:
            raise RuntimeError(f"Failed to set node ID to {node_id}")

    @contextlib.asynccontextmanager
    async def temporary_node_id(self, node_id: int) -> AsyncGenerator[None, None]:
        """Temporarily change the node ID of the device.

        This is a context manager that temporarily changes the device's node ID and restores the original node ID when
        exiting the context, even if an exception occurs.

        Args:
            node_id: The temporary node ID to use.

        Example:
            >>> previous_node_id = device.node_id
            >>> async with device.temporary_node_id(42):
            ...     assert device.node_id == 42
            ...     # do something with the device
            ...     print(1 / 0)
            ... except ZeroDivisionError:
            ...     pass
            >>> assert device.node_id == previous_node_id   # the original node ID is restored
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
        """Get the message type for a given port name.

        Private method that extracts the port type from the registry and loads the corresponding Python type.

        Args:
            port_name: Name of the port.

        Returns:
            Type[MessageClass]: The message type class.

        Raises:
            AssertionError: If the port type is not a string or doesn't match the expected format.
            RuntimeError: If the port type cannot be found or loaded.

        Note:
            This method follows the Cyphal Specification v1.0 versioning principles:
            - For major version > 0: loads the newest major version type
            - For major version = 0: loads the exact type (major.minor)
        """
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
        """Get a subscriber for a specific port on the device.

        This method creates a subscriber for a specific port on the device. The port must be configured on the device
        and the message type must be available.

        Args:
            port_name: Name of the port to subscribe to.

        Returns:
            pycyphal.presentation.Subscriber: A subscriber instance for the specified port.

        Raises:
            RuntimeError: If the port type cannot be found or loaded.

        Example:
            >>> from starcopter.aeric import Navlight_0_1
            >>> subscriber = device.get_subscription("navlights_feedback")
            >>> assert subscriber.dtype == Navlight_0_1
            >>> async for message, _ in subscriber:
            ...     print(message)
            ...     break
            starcopter.aeric.Navlight.0.1(brightness=300, color=2)
        """
        port_id = self._get_port_id(port_name)
        port_type = self._get_port_type(port_name)

        return self.client.node.make_subscriber(port_type, port_id)


async def discover_device_node_id(
    client: Client,
    name: str | None = None,
    uid: str | bytes | None = None,
    exclude_uids: Container[str | bytes] | None = None,
    *,
    timeout: float = 3.0,
) -> int:
    """Discover a device on the network when its node ID is not known.

    This function searches for a device on the network based on its name or unique ID and returns its node ID.
    It can search for devices that are already known to the node tracker or wait for new devices to appear.

    Args:
        client: The client to use for discovering the device.
        name: The name of the device to discover. If provided, the first device with a matching name will be returned.
        uid: The unique ID of the device to discover. Can be provided as a string (hex format) or bytes.
        exclude_uids: A container (set, list, ...) of unique IDs to exclude from discovery.
        timeout: Maximum time in seconds to wait for device discovery.

    Returns:
        int: The node ID of the discovered device.

    Raises:
        ValueError: If neither name nor uid is provided.
        TimeoutError: If no matching device is found within the timeout period.

    Example:
        >>> # Discover by name
        >>> node_id = await discover_device_node_id(client, name="my_device")
        >>> print(f"Found device at node ID: {node_id}")
        Found device at node ID: 42

        >>> # Discover by UID
        >>> node_id = await discover_device_node_id(client, uid="1234567890abcdef")
        >>> print(f"Found device at node ID: {node_id}")
        Found device at node ID: 17
    """
    from pycyphal.application.node_tracker import Entry

    if not name and not uid:
        raise ValueError("Either name or UID must be provided")
    if isinstance(uid, str):
        uid: bytes = bytes.fromhex(uid)

    loop = asyncio.get_event_loop()
    fut_node_id: asyncio.Future[int] = loop.create_future()

    def _matches(entry: Entry) -> bool:
        if entry.info is None:
            return False
        if name is not None and entry.info.name.tobytes().decode() != name:
            return False
        if uid is not None and entry.info.unique_id.tobytes() != uid:
            return False
        if exclude_uids is not None and entry.info.unique_id.tobytes() in exclude_uids:
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
            raise TimeoutError(f"Failed to discover a device with {', '.join(attrs)} within {timeout} seconds") from ex

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
