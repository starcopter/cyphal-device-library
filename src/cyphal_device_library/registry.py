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

import uavcan.register
from pycyphal.application._node import Client
from uavcan.primitive import Empty_1 as Empty
from uavcan.primitive import String_1 as String
from uavcan.primitive import Unstructured_1 as Unstructured
from uavcan.primitive.array import Bit_1 as Bit
from uavcan.primitive.array import Integer8_1 as Integer8
from uavcan.primitive.array import Integer16_1 as Integer16
from uavcan.primitive.array import Integer32_1 as Integer32
from uavcan.primitive.array import Integer64_1 as Integer64
from uavcan.primitive.array import Natural8_1 as Natural8
from uavcan.primitive.array import Natural16_1 as Natural16
from uavcan.primitive.array import Natural32_1 as Natural32
from uavcan.primitive.array import Natural64_1 as Natural64
from uavcan.primitive.array import Real16_1 as Real16
from uavcan.primitive.array import Real32_1 as Real32
from uavcan.primitive.array import Real64_1 as Real64
from uavcan.register import Name_1 as Name
from uavcan.register import Value_1 as Value

_logger = logging.getLogger(__name__)
ServiceClass = TypeVar("ServiceClass")

RegisterValue = Union[
    Empty,
    String,
    Unstructured,
    Bit,
    Integer64,
    Integer32,
    Integer16,
    Integer8,
    Natural64,
    Natural32,
    Natural16,
    Natural8,
    Real64,
    Real32,
    Real16,
]

NativeValue = Union[None, float, int, bool, list[float], list[int], list[bool], bytes, str]

RegisterType = Union[
    # Empty is not a permitted type
    Type[String],
    Type[Unstructured],
    Type[Bit],
    Type[Integer64],
    Type[Integer32],
    Type[Integer16],
    Type[Integer8],
    Type[Natural64],
    Type[Natural32],
    Type[Natural16],
    Type[Natural8],
    Type[Real64],
    Type[Real32],
    Type[Real16],
]


class Registry:
    RESPONSE_TIMEOUT = 0.5
    REQUEST_ATTEMPTS = 3

    def __init__(
        self, node_id: int | None, client_factory: Callable[[Type[ServiceClass], int], Client[ServiceClass]]
    ) -> None:
        self.node_id = node_id
        self._client_factory = client_factory
        self._client_locks: defaultdict[Type[ServiceClass], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._registers: dict[str, Register] = dict()

    def _check_node_id(self) -> None:
        if self.node_id is None:
            raise ValueError("set remote node ID first")

    def _insert(self, name: Union[str, Name], response: uavcan.register.Access_1.Response) -> "Register":
        basename = Register.get_basename(name)
        if basename not in self:
            self[basename] = Register(name, response, registry=self)
        else:
            self[basename]._update(name, response)
        return self[basename]

    async def discover_registers(self) -> None:
        self._check_node_id()
        count = 0
        t_start = time.monotonic()
        async with self.list_client() as client:
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

    async def refresh_register(self, name: Union[Name, str, bytes]) -> None:
        self._check_node_id()
        if not isinstance(name, Name):
            name = Name(name)
        command = uavcan.register.Access_1.Request(name)
        async with self.access_client() as client:
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

    async def _yield_client(self, dtype: Type[ServiceClass]) -> AsyncIterator[Client[ServiceClass]]:
        self._check_node_id()
        async with self._client_locks[dtype]:
            client = self._client_factory(dtype, self.node_id)
            client.response_timeout = self.RESPONSE_TIMEOUT
            try:
                yield client
            finally:
                client.close()

    def list_client(self) -> AsyncContextManager[Client[uavcan.register.List_1]]:
        return contextlib.asynccontextmanager(self._yield_client)(uavcan.register.List_1)

    def access_client(self) -> AsyncContextManager[Client[uavcan.register.Access_1]]:
        return contextlib.asynccontextmanager(self._yield_client)(uavcan.register.Access_1)

    async def set_value(self, name: str, value: NativeValue) -> bool:
        self._check_node_id()
        self._check_key(name)
        reg = self[name]
        if not reg.mutable:
            raise TypeError(f"{name} is immutable and thus may not be written to")
        value = reg.proxy.normalize(value)
        _logger.debug("Node %i: setting %s to %r...", self.node_id, name, value)
        request = uavcan.register.Access_1.Request(Name(name), reg.proxy.to_uavcan_value(value))
        async with self.access_client() as client:
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
    def __init__(self, name: Union[str, Name], info: uavcan.register.Access_1.Response, registry: Registry) -> None:
        self._value = uavcan.register.Access_1.Response()
        self._min = uavcan.register.Access_1.Response()
        self._max = uavcan.register.Access_1.Response()
        self._default = uavcan.register.Access_1.Response()

        self._registry = registry

        self.name = name
        self.mutable: Optional[bool] = None
        self.persistent: Optional[bool] = None
        self.proxy = TypeProxy(info.value)

        self._update(name, info)

    def _update(self, name: Union[str, Name], response: uavcan.register.Access_1.Response) -> None:
        name = self._parse_name(name)
        assert name.rstrip("<=>") == self.name
        if not repr(self.proxy.to_uavcan_value(self.proxy.to_native(response.value))) == repr(response.value):
            _logger.warning(
                "%s: type check failed, %s != %s",
                name,
                self.proxy.to_uavcan_value(self.proxy.to_native(response.value)),
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
            if (self.mutable, self.persistent) != (response.mutable, response.persistent):
                if not (self.mutable, self.persistent) == (None, None):
                    _logger.warning(
                        "register changed flags: mutable=%s, persistent=%s", response.mutable, response.persistent
                    )
                self.mutable, self.persistent = response.mutable, response.persistent
        if attr != "_value":
            assert (response.mutable, response.persistent) == (
                False,
                True,
            ), "special function registers shall be persistent and immutable"
        current: uavcan.register.Access_1.Response = getattr(self, attr)
        if current.timestamp.microsecond < response.timestamp.microsecond or response.timestamp.microsecond == 0:
            setattr(self, attr, response)

    @staticmethod
    def _parse_name(name: Union[str, Name]) -> str:
        if isinstance(name, str):
            return name
        if isinstance(name, Name):
            return name.name.tobytes().decode("utf8", errors="replace")
        raise TypeError(f"name: expected str or uavcan.register.Name_1, got {type(name).__name__}")

    @staticmethod
    def get_basename(name: Union[str, Name]) -> str:
        str_name = Register._parse_name(name)
        if not re.match(r"^([a-z]|_[0-9a-z])(_?[0-9a-z])*(\.(_?[0-9a-z])+)+_?[<=>]?$", str_name):
            _logger.warning("register '%s' violates naming conventions", str_name)
        return str_name.rstrip("<=>")

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, name: Union[str, Name]) -> None:
        self._name = self.get_basename(name)

    @property
    def value(self) -> NativeValue:
        return self.proxy.to_native(self._value.value)

    @property
    def min(self) -> NativeValue:
        return self.proxy.to_native(self._min.value)

    @property
    def max(self) -> NativeValue:
        return self.proxy.to_native(self._max.value)

    @property
    def default(self) -> NativeValue:
        return self.proxy.to_native(self._default.value)

    @property
    def has_min(self) -> bool:
        return self.min is not None

    @property
    def has_max(self) -> bool:
        return self.max is not None

    @property
    def has_default(self) -> bool:
        return self.default is not None

    @property
    def dtype(self) -> str:
        return self.proxy.type_str

    @property
    def timestamp(self) -> float:
        return self._value.timestamp.microsecond * 1e-6

    def __str__(self) -> str:
        attrs = ", ".join(
            f"{key}={value}"
            for key, value in {
                "value": self.value,
                "min": self.min,
                "max": self.max,
                "default": self.default,
                "mutable": self.mutable,
                "persistent": self.persistent,
            }.items()
            if value is not None
        )
        return f"{self.proxy.type_str} {self.name}: {attrs}"

    async def refresh(self) -> None:
        tasks = {self._registry.refresh_register(self.name)}
        if self.has_default:
            tasks.add(self._registry.refresh_register(f"{self.name}="))
        if self.has_min:
            tasks.add(self._registry.refresh_register(f"{self.name}<"))
        if self.has_max:
            tasks.add(self._registry.refresh_register(f"{self.name}>"))
        await asyncio.gather(*tasks)

    async def set_value(self, value: NativeValue) -> bool:
        return await self._registry.set_value(self.name, value)

    async def reset_value(self) -> bool:
        if not self.has_default:
            warnings.warn(f"{self.name} has no default value, therefore reset_value() has no effect")
            return
        await self.reset_value(self.default)

    @contextlib.asynccontextmanager
    async def temporary_value(self, value: NativeValue) -> AsyncGenerator[None, None]:
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
        String: "string",
        Unstructured: "unstructured",
        Bit: "bit",
        Integer64: "integer64",
        Integer32: "integer32",
        Integer16: "integer16",
        Integer8: "integer8",
        Natural64: "natural64",
        Natural32: "natural32",
        Natural16: "natural16",
        Natural8: "natural8",
        Real64: "real64",
        Real32: "real32",
        Real16: "real16",
    }

    def __init__(self, value: Value) -> None:
        self.register_type = self._get_register_type(value)
        self.length = None
        native = self.to_native(value, unpack=False)
        if self.register_type not in (Empty, String, Unstructured):
            assert isinstance(native, list)
            self.length = len(native)

    @staticmethod
    def _get_register_type(value: Value) -> RegisterType:
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

    def to_native(self, value: Union[Value, RegisterValue, None], unpack: bool = True) -> NativeValue:
        if isinstance(value, Value):
            attr = self.ACCESSORS[self.register_type]
            value = getattr(value, attr)

        if value is None or isinstance(value, Empty):
            return None
        if isinstance(value, String):
            return value.value.tobytes().split(b"\0", 1)[0].decode("utf8", errors="replace")
        if isinstance(value, Unstructured):
            return value.value.tobytes()
        if isinstance(
            value,
            (
                Bit,
                Integer64,
                Integer32,
                Integer16,
                Integer8,
                Natural64,
                Natural32,
                Natural16,
                Natural8,
                Real64,
                Real32,
                Real16,
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
            return Empty()
        if self.register_type not in (Empty, String, Unstructured):
            if not isinstance(value, list):
                value = [value]
            assert isinstance(value, list)
            if self.length != len(value):
                raise ValueError(f"length mismatch: expected {self.length}, got {len(value)}")
        return self.register_type(value)

    def to_uavcan_value(self, value: NativeValue) -> Value:
        if value is None:
            return Value()
        attr = self.ACCESSORS[self.register_type]
        return Value(**{attr: self.to_uavcan_data_type(value)})

    def normalize(self, value: NativeValue) -> NativeValue:
        return self.to_native(self.to_uavcan_data_type(value))

    @property
    def type_str(self) -> str:
        type_str = self.ACCESSORS[self.register_type]
        if self.register_type not in (Empty, String, Unstructured):
            type_str += f"[{self.length}]"
        return type_str
