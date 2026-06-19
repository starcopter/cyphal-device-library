"""Bus-wide publication watching using Client and Device."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pycyphal.presentation
import uavcan.node
import uavcan.primitive

from .client import Client
from .device import Device
from .publications import PublicationPort, discover_publication_ports_remote
from .registry import Registry
from .util.message_serialize import serialize_message

LOGGER = logging.getLogger(__name__)

HEARTBEAT_SUBJECT_ID = uavcan.node.Heartbeat_1_0._FIXED_PORT_ID_

DEFAULT_MAX_MESSAGES = 200
DEFAULT_MAX_MESSAGES_PER_PORT = 50
DEFAULT_NOTIFY_BATCH = 30


@dataclass
class PortStats:
    """Message statistics for one subject port."""

    count: int = 0
    bytes_total: int = 0
    last_at_unix: float = 0.0
    rate_hz: float = 0.0
    _window_count: int = 0
    _window_start: float = field(default_factory=time.time)

    def record(self, byte_count: int = 0) -> None:
        now = time.time()
        self.count += 1
        self.bytes_total += byte_count
        self.last_at_unix = now
        self._window_count += 1
        elapsed = now - self._window_start
        if elapsed >= 1.0:
            self.rate_hz = self._window_count / elapsed
            self._window_count = 0
            self._window_start = now

    def to_dict(self, *, node_id: int, port_name: str | None, subject_id: int) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "port_name": port_name,
            "subject_id": subject_id,
            "count": self.count,
            "rate_hz": round(self.rate_hz, 3),
            "last_at_unix": self.last_at_unix,
            "bytes_total": self.bytes_total,
        }


@dataclass
class ParsedMessage:
    """One received and parsed publication message."""

    node_id: int
    port_name: str | None
    subject_id: int
    type_name: str
    timestamp_unix: float
    transfer_id: int | None
    fields: dict[str, Any]
    parse_status: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "port_name": self.port_name,
            "subject_id": self.subject_id,
            "type_name": self.type_name,
            "timestamp_unix": self.timestamp_unix,
            "transfer_id": self.transfer_id,
            "fields": self.fields,
            "parse_status": self.parse_status,
        }


@dataclass
class DeviceWatchState:
    """Watch state for one remote node."""

    node_id: int
    device_info: dict[str, Any]
    device: Device | None = None
    publications: dict[str, PublicationPort] = field(default_factory=dict)
    subscriber_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    unstructured_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    port_stats: dict[int, PortStats] = field(default_factory=dict)
    known_subject_ids: set[int] = field(default_factory=set)


class BusPublicationWatcher:
    """Watch Cyphal publications from multiple devices using one :class:`~cyphal_device_library.client.Client`.

    The watcher polls :attr:`Client.node_tracker` for online nodes. For each new remote
    node it:

    1. Discovers ``uavcan.pub.<port_name>.{id,type,dt_ms}`` registers.
    2. Creates a :class:`~cyphal_device_library.device.Device` with the publication
       registers pre-fetched.
    3. Subscribes to each catalogued port via :meth:`Device.get_subscription` when the
       DSDL type is available locally, or via an unstructured subscriber otherwise.
    4. Subscribes to :data:`uavcan.node.Heartbeat_1_0` on the fixed subject ID so
       heartbeat traffic on non-catalogued ports can be observed.

    Received messages are stored in :attr:`message_buffer` and queued in an internal
    pending list for incremental delivery. Per-port counters are kept in each device's
    watch state; transfers on subject IDs that are not listed in the device's publication
    catalog are tracked in :attr:`unknown_ports`.

    Typical lifecycle::

        async with Client("my_app", transport=transport) as client:
            watcher = BusPublicationWatcher(client)
            await watcher.start()
            try:
                while True:
                    snapshot = watcher.build_status_payload()
                    for message in snapshot["messages"]:
                        print(message)
                    await asyncio.sleep(0.5)
            finally:
                await watcher.stop()

    For push-style consumers, call :meth:`drain_pending_messages` or
    :meth:`build_status_payload` periodically instead of reading
    :attr:`message_buffer` directly.

    Args:
        client: Started client whose node tracker and presentation layer are used for
            all subscriptions.
        max_messages: Maximum number of parsed messages retained in
            :attr:`message_buffer`.
        max_messages_per_port: Reserved for future per-port buffering limits.
    """

    def __init__(
        self,
        client: Client,
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_messages_per_port: int = DEFAULT_MAX_MESSAGES_PER_PORT,
    ) -> None:
        self.client = client
        self.max_messages = max_messages
        self.max_messages_per_port = max_messages_per_port
        self.devices: dict[int, DeviceWatchState] = {}
        self.unknown_ports: dict[int, dict[int, PortStats]] = {}
        self.message_buffer: deque[ParsedMessage] = deque(maxlen=max_messages)
        self._pending_messages: list[ParsedMessage] = []
        self._stop_event = asyncio.Event()
        self._device_loop_task: asyncio.Task[None] | None = None
        self._promiscuous_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._device_loop_task is not None and not self._device_loop_task.done()

    async def start(self) -> None:
        """Start device tracking and promiscuous transfer observation."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._device_loop_task = asyncio.create_task(self._device_loop(), name="pubwatch-devices")
        self._promiscuous_task = asyncio.create_task(self._promiscuous_loop(), name="pubwatch-promiscuous")

    async def stop(self) -> None:
        """Stop all watch tasks and tear down device subscriptions."""
        self._stop_event.set()
        for task in (self._device_loop_task, self._promiscuous_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._device_loop_task = None
        self._promiscuous_task = None

        async with self._lock:
            for state in list(self.devices.values()):
                await self._teardown_device(state)
            self.devices.clear()
            self.unknown_ports.clear()

    def drain_pending_messages(self, *, limit: int = DEFAULT_NOTIFY_BATCH) -> list[ParsedMessage]:
        """Return and clear pending messages for push notifications."""
        batch = self._pending_messages[:limit]
        del self._pending_messages[:limit]
        return batch

    def build_status_payload(self, *, message_limit: int = DEFAULT_NOTIFY_BATCH) -> dict[str, Any]:
        """Build a status snapshot."""
        devices_payload = []
        for state in sorted(self.devices.values(), key=lambda item: item.node_id):
            devices_payload.append(
                {
                    **state.device_info,
                    "publications": [port.to_dict() for port in state.publications.values()],
                }
            )

        unknown_payload = []
        for node_id, ports in sorted(self.unknown_ports.items()):
            for subject_id, stats in sorted(ports.items()):
                unknown_payload.append(stats.to_dict(node_id=node_id, port_name=None, subject_id=subject_id))

        port_stats_payload = []
        for state in self.devices.values():
            for subject_id, stats in state.port_stats.items():
                port_name = next(
                    (port.port_name for port in state.publications.values() if port.subject_id == subject_id),
                    None,
                )
                port_stats_payload.append(
                    stats.to_dict(node_id=state.node_id, port_name=port_name, subject_id=subject_id)
                )

        return {
            "devices": devices_payload,
            "messages": [message.to_dict() for message in self.drain_pending_messages(limit=message_limit)],
            "unknown_ports": unknown_payload,
            "port_stats": port_stats_payload,
            "updated_at_unix": time.time(),
        }

    async def _device_loop(self) -> None:
        """Poll node tracker and reconcile watched devices with the current bus."""
        while not self._stop_event.is_set():
            entries = dict(self.client.node_tracker.registry)
            current_ids = set(entries)
            known_ids = set(self.devices)

            # New nodes: discover publications and start per-port subscribers.
            for node_id in current_ids - known_ids:
                if node_id == self.client.node.id:
                    continue
                entry = entries[node_id]
                device_info = self._serialize_node_entry(node_id, entry)
                async with self._lock:
                    self.devices[node_id] = DeviceWatchState(node_id=node_id, device_info=device_info)
                try:
                    await self._setup_device(self.devices[node_id])
                except Exception as exc:
                    LOGGER.warning("Failed to set up publication watch for node %s: %s", node_id, exc)

            # Departed nodes: cancel subscribers and drop cached state.
            for node_id in known_ids - current_ids:
                async with self._lock:
                    state = self.devices.pop(node_id, None)
                if state is not None:
                    await self._teardown_device(state)
                self.unknown_ports.pop(node_id, None)

            # Refresh heartbeat/name metadata for nodes still online.
            for node_id, entry in entries.items():
                if node_id in self.devices:
                    self.devices[node_id].device_info = self._serialize_node_entry(node_id, entry)

            await asyncio.sleep(0.5)

    async def _setup_device(self, state: DeviceWatchState) -> None:
        # List uavcan.pub.* registers and build the publication catalog.
        registry = Registry(state.node_id, self.client.node.make_client)
        publications = await discover_publication_ports_remote(registry)
        state.publications = {port.port_name: port for port in publications}
        state.known_subject_ids = {port.subject_id for port in publications}

        # Pre-fetch only publication-related registers on the Device.
        register_names: list[str] = []
        for port in publications:
            register_names.extend(
                [
                    f"uavcan.pub.{port.port_name}.id",
                    f"uavcan.pub.{port.port_name}.type",
                ]
            )
            if port.dt_ms is not None:
                register_names.append(f"uavcan.pub.{port.port_name}.dt_ms")

        device = Device(
            self.client,
            state.node_id,
            discover_registers=register_names or False,
            owns_client=False,
        )
        await device.wait_for_initialization(timeout=10.0)
        state.device = device

        # Typed subscription when DSDL is available; unstructured fallback otherwise.
        for port in publications:
            if port.parse_status != "ok" or port.message_type is None:
                await self._ensure_unstructured_subscription(state, port.subject_id)
                continue
            task = asyncio.create_task(
                self._subscriber_loop(state, port),
                name=f"pubwatch-sub-{state.node_id}-{port.port_name}",
            )
            state.subscriber_tasks[port.port_name] = task

        # Observe heartbeat on the standard subject even when not in the pub catalog.
        await self._ensure_unstructured_subscription(state, HEARTBEAT_SUBJECT_ID)

    async def _teardown_device(self, state: DeviceWatchState) -> None:
        for task in list(state.subscriber_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.subscriber_tasks.clear()

        for task in list(state.unstructured_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.unstructured_tasks.clear()

        if state.device is not None:
            state.device.close()
            state.device = None

    async def _subscriber_loop(self, state: DeviceWatchState, port: PublicationPort) -> None:
        assert state.device is not None
        assert port.message_type is not None

        subscriber = state.device.get_subscription(port.port_name)
        async for message, metadata in subscriber:
            if self._stop_event.is_set():
                return
            # Ignore transfers relayed from other nodes on the same subject ID.
            if metadata.source_node_id != state.node_id:
                continue
            await self._record_message(
                state=state,
                port_name=port.port_name,
                subject_id=port.subject_id,
                type_name=port.type_name,
                fields=serialize_message(message),
                transfer_id=getattr(metadata, "transfer_id", None),
                parse_status="ok",
            )

    async def _ensure_unstructured_subscription(self, state: DeviceWatchState, subject_id: int) -> None:
        if subject_id in state.unstructured_tasks:
            return

        subscriber = self.client.node.make_subscriber(uavcan.primitive.Unstructured_1, subject_id)
        task = asyncio.create_task(
            self._unstructured_loop(state, subject_id, subscriber),
            name=f"pubwatch-unstructured-{state.node_id}-{subject_id}",
        )
        state.unstructured_tasks[subject_id] = task

    async def _unstructured_loop(
        self,
        state: DeviceWatchState,
        subject_id: int,
        subscriber: pycyphal.presentation.Subscriber[Any],
    ) -> None:
        async for message, metadata in subscriber:
            if self._stop_event.is_set():
                return
            if metadata.source_node_id != state.node_id:
                continue

            port_name = None
            parse_status = "missing_dsdl"
            if subject_id in state.known_subject_ids:
                # Catalogued port but no local DSDL — keep hex payload, retain port name.
                port = next((item for item in state.publications.values() if item.subject_id == subject_id), None)
                port_name = port.port_name if port else None
            else:
                # Subject not listed in uavcan.pub.* — count as unknown traffic.
                self._record_unknown(state.node_id, subject_id, byte_count=len(bytes(message.value)))

            raw = bytes(message.value)
            await self._record_message(
                state=state,
                port_name=port_name,
                subject_id=subject_id,
                type_name="uavcan.primitive.Unstructured",
                fields={"value": {"_type": "bytes", "hex": raw.hex()}},
                transfer_id=getattr(metadata, "transfer_id", None),
                parse_status=parse_status,
            )

    async def _promiscuous_loop(self) -> None:
        """Reserved for future transport-level promiscuous capture."""
        while not self._stop_event.is_set():
            await asyncio.sleep(1.0)

    async def _record_message(
        self,
        *,
        state: DeviceWatchState,
        port_name: str | None,
        subject_id: int,
        type_name: str,
        fields: dict[str, Any],
        transfer_id: int | None,
        parse_status: str,
    ) -> None:
        stats = state.port_stats.setdefault(subject_id, PortStats())
        stats.record(byte_count=len(str(fields).encode("utf-8")))
        parsed = ParsedMessage(
            node_id=state.node_id,
            port_name=port_name,
            subject_id=subject_id,
            type_name=type_name,
            timestamp_unix=time.time(),
            transfer_id=transfer_id,
            fields=fields,
            parse_status=parse_status,
        )
        self.message_buffer.append(parsed)  # rolling history for status queries
        self._pending_messages.append(parsed)  # batch drained by build_status_payload / notify
        if len(self._pending_messages) > self.max_messages:
            del self._pending_messages[: len(self._pending_messages) - self.max_messages]

    def _record_unknown(self, node_id: int, subject_id: int, *, byte_count: int) -> None:
        node_stats = self.unknown_ports.setdefault(node_id, {})
        stats = node_stats.setdefault(subject_id, PortStats())
        stats.record(byte_count=byte_count)

    @staticmethod
    def _serialize_node_entry(node_id: int, entry: Any) -> dict[str, Any]:
        heartbeat = entry.heartbeat
        vssc = int(heartbeat.vendor_specific_status_code)
        payload: dict[str, Any] = {
            "node_id": node_id,
            "uptime_s": int(heartbeat.uptime),
            "health": int(heartbeat.health.value),
            "mode": int(heartbeat.mode.value),
            "vssc": vssc,
            "vssc_hex": f"0x{vssc:02x}",
            "name": None,
            "hardware_version": None,
            "software_version": None,
            "git_hash": None,
            "crc": None,
            "unique_id": None,
        }
        if entry.info is not None:
            info = entry.info
            git_hash = f"{info.software_vcs_revision_id:016x}" if info.software_vcs_revision_id else ""
            crc = int(info.software_image_crc[0]) if info.software_image_crc.size > 0 else None
            payload.update(
                {
                    "name": info.name.tobytes().decode(errors="replace"),
                    "hardware_version": f"{info.hardware_version.major}.{info.hardware_version.minor}",
                    "software_version": f"{info.software_version.major}.{info.software_version.minor}",
                    "git_hash": git_hash,
                    "crc": f"{crc:016x}" if crc is not None else "",
                    "unique_id": info.unique_id.tobytes().hex(),
                }
            )
        return payload
