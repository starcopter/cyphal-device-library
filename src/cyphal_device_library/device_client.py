import asyncio
import contextlib
import importlib
import logging
import os
import re
import warnings
from pathlib import Path
from typing import AsyncGenerator, Type, TypeVar
import yaml

import pycyphal
import pycyphal.application
import uavcan.node
import uavcan.primitive
import uavcan.primitive.array
import uavcan.register
from pycyphal.application.node_tracker import Entry

from .client import Client
from .registry import NativeValue, Registry

logger = logging.getLogger("test.device_client")
MessageClass = TypeVar("MessageClass")


class DeviceClient(Client):
    """A Client to interact directly with a single device under test on a Cyphal bus.

    This is a wrapper around the more general `Client` class.

    The device client is useful when you want to interact with a single device on a Cyphal bus, for example to test
    a single device or to perform a self-test. When you want to interact with multiple devices at the same time,
    use the more general `Client` class instead.

    The device under test is specified by the `dut` argument, which can be either a node ID, a name, or None.

    If a node ID is specified, the device client will directly use that node ID as the device under test.
    If a name is specified, the device client will use the first discovered device with a matching name.
    If nothing is specified, the device client will first look for the `DEVICE_UNDER_TEST` environment variable
    (which can hold a node ID or a name), and finally use the `DEFAULT_DUT_NAME` class attribute.

    The sequence is as follows:
    1. If `dut` is a node ID, use it directly.
    2. If `dut` is a name, wait for the first discovered device with a matching name.
    3. If `dut` is None, look for the `DEVICE_UNDER_TEST` environment variable.
       1. if the `DEVICE_UNDER_TEST` environment variable is an integer, use it as a node ID.
       2. if the `DEVICE_UNDER_TEST` environment variable is a string, use it as a name and wait for discovery.
    4. If the `DEVICE_UNDER_TEST` environment variable is not set, use the `DEFAULT_DUT_NAME` class attribute.
       1. if the `DEFAULT_DUT_NAME` class attribute is a string, use it as a name and wait for discovery.

    If no device under test is found, the device client will raise a warning, but continue running.
    The device ID can be set later using the `dut` property.
    """

    DEFAULT_NAME: str = "com.starcopter.device_client"
    DEFAULT_DUT_NAME: str = ...

    def __init__(
        self,
        name: str | None = None,
        dut: int | str | None = None,
        *,
        transport: pycyphal.transport.Transport | None = None,
    ) -> None:
        """
        A client for a device under test (DUT).

        Args:
            name: The name of the client.
            dut: The node ID or name of the device under test.
            transport: The transport to use for the client. If not specified, the client will read
                transport information from environment variables.
        """
        super().__init__(name or self.DEFAULT_NAME, transport=transport)
        assert self.node.id is not None, "node must not be anonymous"

        self.registry = Registry(None, self.node.make_client)
        self._initialized = asyncio.Event()
        self._dut_initialized = asyncio.Event()

        if dut is None:
            env_dut = os.environ.get("DEVICE_UNDER_TEST")
            if env_dut is not None:
                logger.info("Using device under test from environment variable: %s", env_dut)
                try:
                    dut = int(env_dut)
                except ValueError:
                    dut = env_dut
            elif self.DEFAULT_DUT_NAME is not ...:
                dut = self.DEFAULT_DUT_NAME

        if dut is None:
            warnings.warn("No device under test specified")

        self.dut = dut if isinstance(dut, int) else None

        if isinstance(dut, str):

            def _discover_device(node_id: int, old_entry: Entry | None, new_entry: Entry | None) -> None:
                if (
                    new_entry is not None
                    and new_entry.info is not None
                    and new_entry.info.name.tobytes().decode() == dut
                ):
                    logger.info(
                        "Found device under test: node ID %d, name %s, UID %s",
                        node_id,
                        new_entry.info.name.tobytes().decode(),
                        new_entry.info.unique_id.tobytes().hex(),
                    )
                    self.dut = node_id

            async def _wait_for_device_discovery() -> None:
                self.node_tracker.add_update_handler(_discover_device)
                try:
                    await self._initialized.wait()
                finally:
                    self.node_tracker.remove_update_handler(_discover_device)

            asyncio.get_event_loop().create_task(_wait_for_device_discovery())

        async def initialize():
            await self._dut_initialized.wait()
            await asyncio.gather(self.wait_for_info(), self.registry.discover_registers())
            self._initialized.set()

        asyncio.get_event_loop().create_task(initialize())

    async def __aenter__(self) -> "DeviceClient":
        self.start()
        await self._initialized.wait()
        return self

    async def __aexit__(self, exc_t, exc_v, exc_tb) -> None:
        self.close()

    @property
    def dut(self) -> int | None:
        """Device Under Test Node ID, may be None."""
        return self._dut

    @dut.setter
    def dut(self, value: int | None) -> None:
        if value == self.node.id:
            raise ValueError("Device under test cannot be the same as own node ID")
        self._dut = value
        self.registry.node_id = value
        if value is not None:
            self._dut_initialized.set()
        else:
            self._dut_initialized.clear()

    @property
    def info(self) -> uavcan.node.GetInfo_1.Response | None:
        """Node info, gathered from the node tracker."""
        _, info = self.node_tracker.registry.get(self.dut, (None, None))
        return info

    @property
    def heartbeat(self) -> uavcan.node.Heartbeat_1 | None:
        """Last received heartbeat."""
        heartbeat, _ = self.node_tracker.registry.get(self.dut, (None, None))
        return heartbeat

    @property
    def uptime(self) -> int:
        """Uptime of the device under test, in seconds."""
        try:
            return self.heartbeat.uptime
        except AttributeError:
            # no heartbeat yet
            return 0

    async def wait_for_info(self) -> pycyphal.application.NodeInfo:
        """Wait for the node info of the device under test to be available."""
        update = asyncio.Event()

        def _notify(node_id: int, old_entry: Entry | None, new_entry: Entry | None) -> None:
            if node_id == self.dut and new_entry is not None and new_entry.info is not None:
                update.set()

        while not self.info:
            update.clear()
            self.node_tracker.add_update_handler(_notify)

            try:
                await update.wait()
            finally:
                self.node_tracker.remove_update_handler(_notify)

        assert isinstance(self.info, uavcan.node.GetInfo_1.Response)
        return self.info

    async def get_app_name(self) -> str:
        """Get the application name of the device under test."""
        info = await self.wait_for_info()
        return info.name.tobytes().decode()

    async def get_device_uid(self) -> str:
        """Get the unique ID of the device under test."""
        info = await self.wait_for_info()
        return info.unique_id.tobytes().hex()

    async def execute(self, command: uavcan.node.ExecuteCommand_1.Request) -> uavcan.node.ExecuteCommand_1.Response:
        """Execute a command on the device under test.

        Args:
            command: The command to execute.

        Returns:
            The response from the device under test.
        """
        return await self.execute_command(command, server_node_id=self.dut)

    async def restart(self, wait: bool = True, timeout: float = 1.0) -> float:
        """Restart the device under test.

        Args:
            wait: Whether to wait for the device under test to restart.
            timeout: The timeout in seconds to wait for the device under test to restart.

        Returns:
            If `wait` is True, the time the DUT took to come back online.
            If `wait` is False, the time until the DUT took to respond to the request.
        """
        return await self.restart_node(self.dut, wait, timeout)

    async def update(self, image: Path, wait: bool = True, timeout: float = 5.0) -> float:
        """Update the firmware of the device under test.

        Args:
            image: Path to the firmware image file.
            wait: Whether to wait for the device to restart after the update.
            timeout: Maximum time in seconds to wait for the device to restart.

        Returns:
            float: Time taken for the update process to complete.
        """
        return await super().update(self.dut, image, wait, timeout)

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
            self.dut = node_id
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
        if previous_value == node_id:
            yield
            return

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
    
    async def save_mmb_params(self, yaml_file: str) -> None:
        """
        Load register values from a YAML file and write them into the MMB registers.
        
        Args:
            yaml_file: Path to the YAML file containing register values.

        Raises:
            FileNotFoundError: If the specified YAML file does not exist.
            Exception: If an error occurs while writing to the registers, 
            e.g. if the register does not exist
        """

        try:
            with open(yaml_file, 'r') as file: 
                mmb_data = yaml.safe_load(file)

            logger.info(f"Found MMB data in {yaml_file}")
            
            try:
                for key, value in mmb_data.items():
                    #print(f"{key}: {value}")
                    await self.write_register(key, mmb_data[key])

                await self.restart()
                logger.info("Successfully saved data in MMB")
                
                            
            except Exception as e:
                logger.error(f"Could not save data in MMB: {e}")

        except FileNotFoundError:
            logger.error("Could not find yaml file")

    
    async def motor_params_into_yaml(self, target_node_id: int, yaml_motor: str) -> None:
        """Reads motor parameters from the connected arm and saves them under the specified node ID in a YAML file.
        
        Args:
            target_node_id: The node ID under which the motor parameters are to be saved.
            yaml_motor: Path to the YAML file
        """
        try: 
            resistance = await self.read_register("motor.resistance")
            inductance = await self.read_register("motor.inductance_dq")
            flux = await self.read_register("motor.flux_linkage")
        except Exception as e:
            logger.error(f"Could not read motor parameter from node {target_node_id}: {e}")
            return
        
        if os.path.exists(yaml_motor):
            with open(yaml_motor, "r") as f:
                existing_data = yaml.safe_load(f)
        else:
            existing_data = {}

        existing_data[target_node_id] = {
            "motor.resistance": resistance,
            "motor.flux_linkage": flux,
            "motor.inductance_dq": inductance,
        }

        sorted_data = dict(sorted(existing_data.items(), key=lambda item: int(item[0])))
        
        try:
            with open(yaml_motor, "w") as f:
                yaml.dump(sorted_data, f, sort_keys=False, default_flow_style=None)

            logger.info(f"Successfully saved data in {yaml_motor}")

        except Exception as e:
            logger.error(f"Could not save data in {yaml_motor}: {e}")

