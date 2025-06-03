import asyncio
import contextlib
import importlib
import logging
import os
import re
from pathlib import Path
from typing import AsyncGenerator, Type, TypeVar

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
    DEFAULT_NAME: str = "com.starcopter.device_client"
    DEVICE_NAME: str = ...

    def __init__(
        self,
        name: str | None = None,
        dut: int | None = None,
        uid: int | None = None,
    ) -> None:
        super().__init__(name or self.DEFAULT_NAME, uid=uid)
        assert self.node.id is not None, "node must not be anonymous"

        self.registry = Registry(None, self.node.make_client)
        self._initialized = asyncio.Event()
        self._dut_initialized = asyncio.Event()

        if dut is None:
            env_dut = os.environ.get("DEVICE_UNDER_TEST")
            if env_dut is not None:
                dut = int(env_dut)

        self.dut = dut

        if self.dut is None and self.DEVICE_NAME is not ...:

            def handler(node_id: int, old_entry: Entry | None, new_entry: Entry | None) -> None:
                if (
                    new_entry is not None
                    and new_entry.info is not None
                    and new_entry.info.name.tobytes().decode() == self.DEVICE_NAME
                ):
                    logger.info(
                        "Found device under test: node ID %d, name %s, UID %s",
                        node_id,
                        new_entry.info.name.tobytes().decode(),
                        new_entry.info.unique_id.tobytes().hex(),
                    )
                    self.dut = node_id

            async def worker() -> None:
                self.node_tracker.add_update_handler(handler)
                try:
                    await self._initialized.wait()
                finally:
                    self.node_tracker.remove_update_handler(handler)

            asyncio.get_event_loop().create_task(worker())

        async def initialize():
            await self._dut_initialized.wait()
            await asyncio.gather(self.get_info(), self.registry.discover_registers())
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
        _, info = self.node_tracker.registry.get(self.dut, (None, None))
        return info

    @property
    def heartbeat(self) -> uavcan.node.Heartbeat_1 | None:
        heartbeat, _ = self.node_tracker.registry.get(self.dut, (None, None))
        return heartbeat

    @property
    def uptime(self) -> int:
        try:
            return self.heartbeat.uptime
        except AttributeError:
            # no heartbeat yet
            return 0

    async def get_info(self, refresh: bool = False) -> pycyphal.application.NodeInfo:
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
        info = await self.get_info()
        return info.name.tobytes().decode()

    async def get_device_uid(self) -> str:
        info = await self.get_info()
        return info.unique_id.tobytes().hex()

    async def execute(self, command: uavcan.node.ExecuteCommand_1.Request) -> uavcan.node.ExecuteCommand_1.Response:
        return await self.execute_command(command, server_node_id=self.dut)

    async def restart(self, wait: bool = True, timeout: float = 1.0) -> float:
        return await self.restart_node(self.dut, wait, timeout)

    async def update(self, image: Path, wait: bool = True, timeout: float = 5.0) -> float:
        return await super().update(self.dut, image, wait, timeout)

    async def write_register(self, register_name: str, value: NativeValue) -> NativeValue:
        register = self.registry[register_name]  # this may raise a KeyError
        logger.debug(f"setting {register_name} to {value}...")
        success = await register.set_value(value)
        assert success
        return register.value

    async def reset_register(self, register_name: str) -> NativeValue:
        register = self.registry[register_name]  # this may raise a KeyError
        if not register.has_default:
            raise AttributeError(f"{register_name} has no configured default")
        return await self.write_register(register_name, register.default)

    async def read_register(self, register_name: str, refresh: bool = False) -> NativeValue:
        register = self.registry[register_name]  # this may raise a KeyError
        if refresh:
            await register.refresh()
        return register.value

    async def set_node_id(self, node_id: int) -> None:
        register = self.registry["uavcan.node.id"]
        if await register.set_value(node_id):
            self.dut = node_id
        else:
            raise RuntimeError(f"Failed to set node ID to {node_id}")

    @contextlib.asynccontextmanager
    async def temporary_node_id(self, node_id: int) -> AsyncGenerator[None, None]:
        register = self.registry["uavcan.node.id"]
        previous_value = register.value
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
            raise RuntimeError(f"Port {port_name}): No matching type found for {port_type}") from ex

        return Message

    def get_subscription(self, port_name: str) -> pycyphal.presentation.Subscriber:
        port_id = self._get_port_id(port_name)
        port_type = self._get_port_type(port_name)

        return self.node.make_subscriber(port_type, port_id)
