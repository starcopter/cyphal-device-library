"""Registry and Register classes for managing Cyphal device registers.

This module provides classes for interacting with Cyphal device registers through the standard register access protocol.
The main classes are:

## Registry

The `Registry` class is capable of discovering all registers on a (remote) Cyphal node, and provides methods for:

- Reading and writing register values
- Managing register metadata (min/max/default values)
- Handling register persistence and mutability

The `Registry` has a dictionary-like interface for accessing registers by name.

## Register

The `Register` class represents a single register on a (remote) Cyphal node.
Each register instance provides:

- Value access and modification
- Metadata access (min/max/default values)
- Type information and validation
- Persistence and mutability flags

## TypeProxy

The `TypeProxy` class is used internally to handle type conversion between Cyphal and native Python types.
"""

import asyncio
import contextlib
import logging
import re
import time
import warnings
from collections import defaultdict
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterator,
    Optional,
    Type,
    TypeVar,
    Union,
)

import pycyphal.presentation
import uavcan.primitive
import uavcan.primitive.array
import uavcan.register

_logger = logging.getLogger(__name__)
ServiceClass = TypeVar("ServiceClass")

RegisterValue = Union[
    uavcan.primitive.Empty_1,
    uavcan.primitive.String_1,
    uavcan.primitive.Unstructured_1,
    uavcan.primitive.array.Bit_1,
    uavcan.primitive.array.Integer64_1,
    uavcan.primitive.array.Integer32_1,
    uavcan.primitive.array.Integer16_1,
    uavcan.primitive.array.Integer8_1,
    uavcan.primitive.array.Natural64_1,
    uavcan.primitive.array.Natural32_1,
    uavcan.primitive.array.Natural16_1,
    uavcan.primitive.array.Natural8_1,
    uavcan.primitive.array.Real64_1,
    uavcan.primitive.array.Real32_1,
    uavcan.primitive.array.Real16_1,
]

NativeValue = Union[None, float, int, bool, list[float], list[int], list[bool], bytes, str]

RegisterType = Union[
    # Empty is not a permitted type
    Type[uavcan.primitive.String_1],
    Type[uavcan.primitive.Unstructured_1],
    Type[uavcan.primitive.array.Bit_1],
    Type[uavcan.primitive.array.Integer64_1],
    Type[uavcan.primitive.array.Integer32_1],
    Type[uavcan.primitive.array.Integer16_1],
    Type[uavcan.primitive.array.Integer8_1],
    Type[uavcan.primitive.array.Natural64_1],
    Type[uavcan.primitive.array.Natural32_1],
    Type[uavcan.primitive.array.Natural16_1],
    Type[uavcan.primitive.array.Natural8_1],
    Type[uavcan.primitive.array.Real64_1],
    Type[uavcan.primitive.array.Real32_1],
    Type[uavcan.primitive.array.Real16_1],
]


class Registry:
    """Registry to interact with registers on a single remote Cyphal node.

    Apart from the methods listed below, the `Registry` class has a dictionary-like interface for accessing registers
    by name. See the `Register` class' documentation for more details.

    Methods:
        discover_registers(): Discover all registers on the remote node.
        refresh_register(name): Refresh a single register's value and metadata from the remote node.
        set_value(name, value): Set a register's value on the remote node.

    Attributes:
        node_id: The ID of the remote node, or None if not set.

    Example:
        >>> registry = Registry(node_id=1, client_factory=some_existing_node.make_client)
        >>> await registry.discover_registers()
        >>> print(registry["navlight.brightness"])
        natural16[1] navlight.brightness: value=100, min=0, max=1000, default=300, mutable=True, persistent=True
        >>> await registry.set_value("navlight.brightness", 50)
        True
        >>> print(registry["navlight.brightness"])
        natural16[1] navlight.brightness: value=50, min=0, max=1000, default=300, mutable=True, persistent=True
    """

    RESPONSE_TIMEOUT = 0.5
    REQUEST_ATTEMPTS = 3

    def __init__(
        self,
        node_id: int | None,
        client_factory: Callable[[Type[ServiceClass], int], pycyphal.presentation.Client[ServiceClass]],
    ) -> None:
        """Initialize a new Registry instance.

        Args:
            node_id: The ID of the remote node to interact with, or None if not yet known.
            client_factory: A callable that creates Cyphal clients for service types. Usually this would be the
                `pycyphal.presentation.Node.make_client` method of the underlying `Node` instance.

        """
        self.node_id = node_id
        self._client_factory = client_factory
        self._client_locks: defaultdict[Type[ServiceClass], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._registers: dict[str, Register] = dict()

    def _check_node_id(self) -> None:
        if self.node_id is None:
            raise ValueError("set remote node ID first")

    def _insert(
        self, name: Union[str, uavcan.register.Name_1], response: uavcan.register.Access_1.Response
    ) -> "Register":
        basename = Register._get_basename(name)
        if basename not in self:
            self[basename] = Register(name, response, registry=self)
        else:
            self[basename]._update(name, response)
        return self[basename]

    async def discover_registers(self) -> None:
        """Discover all registers available on the remote node.

        This method will query the remote node for all available registers and their metadata.
        The discovered registers can then be accessed using the dict-like interface.
        """
        self._check_node_id()
        count = 0
        t_start = time.monotonic()
        async with self._list_client() as client:
            for index in range(2**16):
                command = uavcan.register.List_1.Request(index)
                for _attempt in range(self.REQUEST_ATTEMPTS):
                    result = await client.call(command)
                    if result is not None:
                        break
                    _logger.debug("%s (%i) to node %i failed", command, _attempt, self.node_id)
                if result is None:
                    # none of the {up to N} attempts returned a result
                    _logger.info("Node %i seems not to have the Register API implemented", self.node_id)
                    return
                response: uavcan.register.List_1.Response = result[0]
                if len(response.name.name) == 0:
                    # empty name means the list is exhausted; we've discovered all registers
                    break

                await self.refresh_register(response.name)
                count += 1

        t_end = time.monotonic()
        _logger.debug("%i registers discovered for node %i in %i ms", count, self.node_id, 1000 * (t_end - t_start))

        for reg in self:
            _logger.debug("Node %i has a register %s", self.node_id, reg)

    async def refresh_register(self, name: Union[uavcan.register.Name_1, str, bytes]) -> None:
        """Refresh a single register's value and metadata from the remote node.

        Args:
            name: The name of the register to refresh. Can be a string, bytes, or UAVCAN Name type.
        """
        self._check_node_id()
        if not isinstance(name, uavcan.register.Name_1):
            name = uavcan.register.Name_1(name)
        command = uavcan.register.Access_1.Request(name)
        async with self._access_client() as client:
            for _attempt in range(self.REQUEST_ATTEMPTS):
                result = await client.call(command)
                if result is not None:
                    break
                _logger.debug("%s (%i) to node %i failed", command, _attempt, self.node_id)
        if result is None:
            # none of the {up to N} attempts returned a result
            _logger.info("Access to register %s of node %i failed", Register._parse_name(name), self.node_id)
            return
        response: uavcan.register.Access_1.Response = result[0]
        self._insert(name, response)

    @staticmethod
    def _check_key(key: Any) -> None:
        if not isinstance(key, str):
            raise TypeError(f"key needs to be type str, not {type(key).__name__}")

    def __getitem__(self, key: str) -> "Register":
        self._check_key(key)
        return self._registers[key]

    def __setitem__(self, key: str, value: "Register") -> None:
        self._check_key(key)
        self._registers[key] = value

    def __iter__(self) -> Iterator["Register"]:
        yield from self._registers.values()

    def __contains__(self, key: str) -> bool:
        self._check_key(key)
        return key in self._registers

    def __delitem__(self, key: str) -> None:
        self._check_key(key)
        del self._registers[key]

    async def _yield_client(
        self, dtype: Type[ServiceClass]
    ) -> AsyncIterator[pycyphal.presentation.Client[ServiceClass]]:
        self._check_node_id()
        async with self._client_locks[dtype]:
            client = self._client_factory(dtype, self.node_id)
            client.response_timeout = self.RESPONSE_TIMEOUT
            try:
                yield client
            finally:
                client.close()

    def _list_client(self) -> AsyncContextManager[pycyphal.presentation.Client[uavcan.register.List_1]]:
        return contextlib.asynccontextmanager(self._yield_client)(uavcan.register.List_1)

    def _access_client(self) -> AsyncContextManager[pycyphal.presentation.Client[uavcan.register.Access_1]]:
        return contextlib.asynccontextmanager(self._yield_client)(uavcan.register.Access_1)

    async def set_value(self, name: str, value: NativeValue) -> bool:
        """Set a register's value on the remote node.

        Args:
            name: The name of the register to set.
            value: The new value to set. Must be compatible with the register's type.

        Returns:
            True if the value was successfully set, False otherwise.

        Raises:
            TypeError: If the register is immutable or if the value type is incompatible.
            ValueError: If the node_id is not set.
        """
        self._check_node_id()
        self._check_key(name)
        reg = self[name]
        if not reg._mutable:
            raise TypeError(f"{name} is immutable and thus may not be written to")
        value = reg._proxy.normalize(value)
        _logger.debug("Node %i: setting %s to %r...", self.node_id, name, value)
        request = uavcan.register.Access_1.Request(uavcan.register.Name_1(name), reg._proxy.to_uavcan_value(value))
        async with self._access_client() as client:
            for _attempt in range(self.REQUEST_ATTEMPTS):
                result = await client.call(request)
                if result is not None:
                    break
                _logger.debug("%s (%i) to node %i failed", request, _attempt, self.node_id)
            else:
                _logger.error("Access to register %s of node %i failed", name, self.node_id)
                _logger.debug(request)
                return False
        response = result[0]
        self._insert(name, response)
        success = value == reg.value or isinstance(value, list) and len(value) == 1 and value[0] == reg.value
        if success:
            _logger.info("Node %i: %s set to %s", self.node_id, name, value)
        else:
            _logger.warning("Node %i: setting %s to %r failed, value=%r", self.node_id, name, value, reg.value)
        return success


class Register:
    """Single register on a remote Cyphal node.

    A register represents a single configuration or state value on a remote Cyphal node.
    Each register has a value, type information, and optional metadata like min/max/default values.

    Register instances are usually created by the `Registry` class, and not directly.

    Methods:
        refresh(): Refresh the register's value and metadata from the remote node.
        set_value(value): Set the register's value on the remote node.
        reset_value(): Reset the register to its default value.
        temporary_value(value): Context manager for temporarily setting a value.

    Properties:
        value: The current value of the register.
        min: The minimum allowed value, if defined.
        max: The maximum allowed value, if defined.
        default: The default value, if defined.
        has_min: Whether the register has a minimum value defined.
        has_max: Whether the register has a maximum value defined.
        has_default: Whether the register has a default value defined.
        dtype: The data type of the register as a string.
        timestamp: The timestamp of the last value update, as reported by the remote node.
        name: The name of the register.
        mutable: Whether the register can be modified.
        persistent: Whether the register's value persists across node restarts.

    Example:
        >>> register = registry["navlight.brightness"]
        >>> print(register)
        natural16[1] navlight.brightness: value=100, min=0, max=1000, default=300, mutable=True, persistent=True
        >>> await register.set_value(50)
        >>> print(register)
        natural16[1] navlight.brightness: value=50, min=0, max=1000, default=300, mutable=True, persistent=True
        >>> await register.reset_value()
        >>> print(register)
        natural16[1] navlight.brightness: value=300, min=0, max=1000, default=300, mutable=True, persistent=True
        >>> try:
        ...     async with register.temporary_value(1000):
        ...         print(register)
        ...         await asyncio.sleep(1)
        ...         print(1 / 0)
        ... except ZeroDivisionError:
        ...     pass
        natural16[1] navlight.brightness: value=1000, min=0, max=1000, default=300, mutable=True, persistent=True
        >>> print(register)  # value is back to original
        natural16[1] navlight.brightness: value=300, min=0, max=1000, default=300, mutable=True, persistent=True
    """

    def __init__(
        self, name: Union[str, uavcan.register.Name_1], info: uavcan.register.Access_1.Response, registry: Registry
    ) -> None:
        self._value = uavcan.register.Access_1.Response()
        self._min = uavcan.register.Access_1.Response()
        self._max = uavcan.register.Access_1.Response()
        self._default = uavcan.register.Access_1.Response()

        self._registry = registry

        self.name = name
        self._mutable: Optional[bool] = None
        self._persistent: Optional[bool] = None
        self._proxy = TypeProxy(info.value)

        self._update(name, info)

    def _update(self, name: Union[str, uavcan.register.Name_1], response: uavcan.register.Access_1.Response) -> None:
        name = self._parse_name(name)
        assert name.rstrip("<=>") == self.name
        if not repr(self._proxy.to_uavcan_value(self._proxy.to_native(response.value))) == repr(response.value):
            _logger.warning(
                "%s: type check failed, %s != %s",
                name,
                self._proxy.to_uavcan_value(self._proxy.to_native(response.value)),
                response.value,
            )
        if name.endswith("<"):
            attr = "_min"
        elif name.endswith(">"):
            attr = "_max"
        elif name.endswith("="):
            attr = "_default"
        else:
            attr = "_value"
            if (self._mutable, self._persistent) != (response.mutable, response.persistent):
                if not (self._mutable, self._persistent) == (None, None):
                    _logger.warning(
                        "register changed flags: mutable=%s, persistent=%s", response.mutable, response.persistent
                    )
                self._mutable, self._persistent = response.mutable, response.persistent
        if attr != "_value" and (response.mutable or not response.persistent):
            _logger.warning("special function register '%s' should be persistent and immutable", name)
        current: uavcan.register.Access_1.Response = getattr(self, attr)
        if current.timestamp.microsecond < response.timestamp.microsecond or response.timestamp.microsecond == 0:
            setattr(self, attr, response)

    @staticmethod
    def _parse_name(name: Union[str, uavcan.register.Name_1]) -> str:
        if isinstance(name, str):
            return name
        if isinstance(name, uavcan.register.Name_1):
            return name.name.tobytes().decode("utf8", errors="replace")
        raise TypeError(f"name: expected str or uavcan.register.Name_1, got {type(name).__name__}")

    @staticmethod
    def _get_basename(name: Union[str, uavcan.register.Name_1]) -> str:
        str_name = Register._parse_name(name)
        if not re.match(r"^([a-z]|_[0-9a-z])(_?[0-9a-z])*(\.(_?[0-9a-z])+)+_?[<=>]?$", str_name):
            _logger.warning("register '%s' violates naming conventions", str_name)
        return str_name.rstrip("<=>")

    @property
    def name(self) -> str:
        """Register base name."""
        return self._name

    @name.setter
    def name(self, name: Union[str, uavcan.register.Name_1]) -> None:
        self._name = self._get_basename(name)

    @property
    def value(self) -> NativeValue:
        """Register value as Python type."""
        return self._proxy.to_native(self._value.value)

    @property
    def min(self) -> NativeValue:
        """Minimum value as Python type, or None if not defined."""
        return self._proxy.to_native(self._min.value)

    @property
    def max(self) -> NativeValue:
        """Maximum value as Python type, or None if not defined."""
        return self._proxy.to_native(self._max.value)

    @property
    def default(self) -> NativeValue:
        """Default value as Python type, or None if not defined."""
        return self._proxy.to_native(self._default.value)

    @property
    def mutable(self) -> bool:
        """Whether the register can be modified."""
        return bool(self._mutable)

    @property
    def persistent(self) -> bool:
        """Whether the register's value persists across node restarts."""
        return bool(self._persistent)

    @property
    def has_min(self) -> bool:
        """Whether the register has a minimum value defined."""
        return self.min is not None

    @property
    def has_max(self) -> bool:
        """Whether the register has a maximum value defined."""
        return self.max is not None

    @property
    def has_default(self) -> bool:
        """Whether the register has a default value defined."""
        return self.default is not None

    @property
    def dtype(self) -> str:
        """Register data type as string."""
        return self._proxy.type_str

    @property
    def timestamp(self) -> float:
        """Timestamp of the last value update, as reported by the remote node."""
        return self._value.timestamp.microsecond * 1e-6

    def __repr__(self) -> str:
        return f"<Register {self.dtype} {self.name}>"

    def __str__(self) -> str:
        attrs = ", ".join(
            f"{key}={value}"
            for key, value in {
                "value": self.value,
                "min": self.min,
                "max": self.max,
                "default": self.default,
                "mutable": self._mutable,
                "persistent": self._persistent,
            }.items()
            if value is not None
        )
        return f"{self.dtype} {self.name}: {attrs}"

    async def refresh(self) -> None:
        """Refresh the register's value and metadata from the remote node."""
        tasks = {self._registry.refresh_register(self.name)}
        if self.has_default:
            tasks.add(self._registry.refresh_register(f"{self.name}="))
        if self.has_min:
            tasks.add(self._registry.refresh_register(f"{self.name}<"))
        if self.has_max:
            tasks.add(self._registry.refresh_register(f"{self.name}>"))
        await asyncio.gather(*tasks)

    async def set_value(self, value: NativeValue) -> bool:
        """Set the register's value on the remote node.

        Args:
            value: The new value to set. Must be compatible with the register's type.

        Returns:
            True if the value was successfully set, False otherwise.

        Raises:
            TypeError: If the register is immutable or if the value type is incompatible.
        """
        return await self._registry.set_value(self.name, value)

    async def reset_value(self) -> bool:
        """Reset the register's value to its default value.

        Issues a warning if the register has no default value.

        Returns:
            True if the value was successfully set, False otherwise.

        Raises:
            ValueError: If the register is immutable.
        """
        if not self.has_default:
            warnings.warn(f"{self.name} has no default value, therefore reset_value() has no effect")
            return
        await self.reset_value(self.default)

    @contextlib.asynccontextmanager
    async def temporary_value(self, value: NativeValue) -> AsyncGenerator[None, None]:
        """Context manager for temporarily setting a value.

        This context manager can be used to temporarily set a register to a new value, which is a common pattern in
        testing. Even in case of an exception inside the context block, the register's value will be reset to its
        previous value before the exception is escalated up the call stack.

        Args:
            value: The new value to set. Must be compatible with the register's type.

        Raises:
            TypeError: If the register is immutable or if the value type is incompatible.

        Example:
            >>> register = registry["navlight.brightness"]
            >>> register
            <Register natural16[1] navlight.brightness>
            >>> await register.set_value(100)
            >>> assert register.value == 100
            >>> async with register.temporary_value(1000):
            ...     assert register.value == 1000
            >>> assert register.value == 100
        """
        previous_value = self.value
        success = await self.set_value(value)
        assert success

        try:
            yield
        finally:
            await self.set_value(previous_value)


class TypeProxy:
    """
    A TypeProxy instance is initialized with a uavcan.register.Value.1.0 type and will convert values
    between the UAVCAN data type and a matching native Python data type.
    """

    ACCESSORS = {
        uavcan.primitive.String_1: "string",
        uavcan.primitive.Unstructured_1: "unstructured",
        uavcan.primitive.array.Bit_1: "bit",
        uavcan.primitive.array.Integer64_1: "integer64",
        uavcan.primitive.array.Integer32_1: "integer32",
        uavcan.primitive.array.Integer16_1: "integer16",
        uavcan.primitive.array.Integer8_1: "integer8",
        uavcan.primitive.array.Natural64_1: "natural64",
        uavcan.primitive.array.Natural32_1: "natural32",
        uavcan.primitive.array.Natural16_1: "natural16",
        uavcan.primitive.array.Natural8_1: "natural8",
        uavcan.primitive.array.Real64_1: "real64",
        uavcan.primitive.array.Real32_1: "real32",
        uavcan.primitive.array.Real16_1: "real16",
    }

    def __init__(self, value: uavcan.register.Value_1) -> None:
        self.register_type = self._get_register_type(value)
        self.length = None
        native = self.to_native(value, unpack=False)
        if self.register_type not in (
            uavcan.primitive.Empty_1,
            uavcan.primitive.String_1,
            uavcan.primitive.Unstructured_1,
        ):
            assert isinstance(native, list)
            self.length = len(native)

    @staticmethod
    def _get_register_type(value: uavcan.register.Value_1) -> RegisterType:
        if value.string is not None:
            return type(value.string)
        if value.unstructured is not None:
            return type(value.unstructured)
        if value.bit is not None:
            return type(value.bit)
        if value.integer64 is not None:
            return type(value.integer64)
        if value.integer32 is not None:
            return type(value.integer32)
        if value.integer16 is not None:
            return type(value.integer16)
        if value.integer8 is not None:
            return type(value.integer8)
        if value.natural64 is not None:
            return type(value.natural64)
        if value.natural32 is not None:
            return type(value.natural32)
        if value.natural16 is not None:
            return type(value.natural16)
        if value.natural8 is not None:
            return type(value.natural8)
        if value.real64 is not None:
            return type(value.real64)
        if value.real32 is not None:
            return type(value.real32)
        if value.real16 is not None:
            return type(value.real16)
        raise TypeError(f"Incompatible register type: {value}")

    def to_native(self, value: Union[uavcan.register.Value_1, RegisterValue, None], unpack: bool = True) -> NativeValue:
        if isinstance(value, uavcan.register.Value_1):
            attr = self.ACCESSORS[self.register_type]
            value = getattr(value, attr)

        if value is None or isinstance(value, uavcan.primitive.Empty_1):
            return None
        if isinstance(value, uavcan.primitive.String_1):
            return value.value.tobytes().split(b"\0", 1)[0].decode("utf8", errors="replace")
        if isinstance(value, uavcan.primitive.Unstructured_1):
            return value.value.tobytes()
        if isinstance(
            value,
            (
                uavcan.primitive.array.Bit_1,
                uavcan.primitive.array.Integer64_1,
                uavcan.primitive.array.Integer32_1,
                uavcan.primitive.array.Integer16_1,
                uavcan.primitive.array.Integer8_1,
                uavcan.primitive.array.Natural64_1,
                uavcan.primitive.array.Natural32_1,
                uavcan.primitive.array.Natural16_1,
                uavcan.primitive.array.Natural8_1,
                uavcan.primitive.array.Real64_1,
                uavcan.primitive.array.Real32_1,
                uavcan.primitive.array.Real16_1,
            ),
        ):
            value_list = value.value.tolist()
            if self.length is not None and len(value_list) != self.length:
                raise ValueError(f"length mismatch: expected {self.length}, got {len(value_list)}")
            if unpack and len(value_list) == 1:
                return value_list[0]
            return value_list

        raise TypeError(f"value: incompatible type {type(value).__name__}")

    def to_uavcan_data_type(self, value: NativeValue) -> RegisterValue:
        if value is None:
            return uavcan.primitive.Empty_1()
        if self.register_type not in (
            uavcan.primitive.Empty_1,
            uavcan.primitive.String_1,
            uavcan.primitive.Unstructured_1,
        ):
            if not isinstance(value, list):
                value = [value]
            assert isinstance(value, list)
            if self.length != len(value):
                raise ValueError(f"length mismatch: expected {self.length}, got {len(value)}")
        return self.register_type(value)

    def to_uavcan_value(self, value: NativeValue) -> uavcan.register.Value_1:
        if value is None:
            return uavcan.register.Value_1()
        attr = self.ACCESSORS[self.register_type]
        return uavcan.register.Value_1(**{attr: self.to_uavcan_data_type(value)})

    def normalize(self, value: NativeValue) -> NativeValue:
        return self.to_native(self.to_uavcan_data_type(value))

    @property
    def type_str(self) -> str:
        type_str = self.ACCESSORS[self.register_type]
        if self.register_type not in (
            uavcan.primitive.Empty_1,
            uavcan.primitive.String_1,
            uavcan.primitive.Unstructured_1,
        ):
            type_str += f"[{self.length}]"
        return type_str
