"""Bus-wide publication watching using Client and Device."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pycyphal.presentation
import uavcan.node
import uavcan.primitive

from .client import Client
from .device import Device
from .publications import PublicationPort, discover_publication_ports_remote
from .registry import Registry, registry_to_json_entries
from .util.message_serialize import ensure_json_serializable, serialize_message

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
    sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "port_name": self.port_name,
            "subject_id": self.subject_id,
            "type_name": self.type_name,
            "timestamp_unix": self.timestamp_unix,
            "transfer_id": self.transfer_id,
            "fields": ensure_json_serializable(self.fields),
            "parse_status": self.parse_status,
            "sequence": self.sequence,
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
    registry_entries: list[dict[str, Any]] = field(default_factory=list)
    setup_task: asyncio.Task[None] | None = None


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
        on_state_changed: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.max_messages = max_messages
        self.max_messages_per_port = max_messages_per_port
        self._on_state_changed = on_state_changed
        self.devices: dict[int, DeviceWatchState] = {}
        self.unknown_ports: dict[int, dict[int, PortStats]] = {}
        self.message_buffer: deque[ParsedMessage] = deque(maxlen=max_messages)
        self._port_message_history: dict[tuple[int, int], deque[ParsedMessage]] = {}
        self._pending_messages: list[ParsedMessage] = []
        self._message_sequence = 0
        self._stop_event = asyncio.Event()
        self._device_loop_task: asyncio.Task[None] | None = None
        self._promiscuous_task: asyncio.Task[None] | None = None
        self._setup_tasks: dict[int, asyncio.Task[None]] = {}
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

        for task in list(self._setup_tasks.values()):
            task.cancel()
        for task in list(self._setup_tasks.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._setup_tasks.clear()

        async with self._lock:
            for state in list(self.devices.values()):
                await self._teardown_device(state)
            self.devices.clear()
            self.unknown_ports.clear()
            self._port_message_history.clear()

    def build_message_history_payload(self) -> list[dict[str, Any]]:
        """Return the retained per-subject message history for UI synchronization."""
        result: list[dict[str, Any]] = []
        for bucket in self._port_message_history.values():
            for message in bucket:
                result.append(message.to_dict())
        result.sort(key=lambda item: float(item.get("timestamp_unix", 0)))
        return result

    def _drop_port_message_history(self, node_id: int) -> None:
        for key in [item for item in self._port_message_history if item[0] == node_id]:
            self._port_message_history.pop(key, None)

    def drain_pending_messages(self, *, limit: int | None = None) -> list[ParsedMessage]:
        """Return and clear pending messages for push notifications.

        When *limit* is ``None``, all pending messages are returned. Otherwise at
        most *limit* oldest pending messages are returned.
        """
        if limit is None:
            batch = self._pending_messages[:]
            self._pending_messages.clear()
            return batch
        batch = self._pending_messages[:limit]
        del self._pending_messages[:limit]
        return batch

    def build_status_payload(self, *, message_limit: int | None = None) -> dict[str, Any]:
        """Build a status snapshot."""
        devices_payload = []
        for state in sorted(self.devices.values(), key=lambda item: item.node_id):
            devices_payload.append(
                {
                    **state.device_info,
                    "publications": [port.to_dict() for port in state.publications.values()],
                    "registry": state.registry_entries,
                }
            )

        unknown_payload = []
        for node_id, ports in sorted(self.unknown_ports.items()):
            for subject_id, stats in sorted(ports.items()):
                unknown_payload.append(stats.to_dict(node_id=node_id, port_name=None, subject_id=subject_id))

        port_stats_payload = []
        for state in self.devices.values():
            for subject_id, stats in state.port_stats.items():
                port_name = self._resolve_port_name(state, subject_id)
                port_stats_payload.append(
                    stats.to_dict(node_id=state.node_id, port_name=port_name, subject_id=subject_id)
                )

        return {
            "devices": devices_payload,
            "messages": [message.to_dict() for message in self.drain_pending_messages(limit=message_limit)],
            "message_history": self.build_message_history_payload(),
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

            # New nodes: register immediately, then discover publications in parallel.
            for node_id in current_ids - known_ids:
                if node_id == self.client.node.id:
                    continue
                entry = entries[node_id]
                device_info = self._serialize_node_entry(node_id, entry)
                async with self._lock:
                    self.devices[node_id] = DeviceWatchState(node_id=node_id, device_info=device_info)
                self._notify_state_changed()
                self._start_device_setup(node_id)

            # Departed nodes: cancel in-flight setup, subscribers, and cached state.
            for node_id in known_ids - current_ids:
                await self._cancel_device_setup(node_id)
                async with self._lock:
                    state = self.devices.pop(node_id, None)
                if state is not None:
                    await self._teardown_device(state)
                self.unknown_ports.pop(node_id, None)
                self._drop_port_message_history(node_id)

            # Refresh heartbeat/name metadata for nodes still online.
            for node_id, entry in entries.items():
                if node_id in self.devices:
                    self.devices[node_id].device_info = self._serialize_node_entry(node_id, entry)

            await asyncio.sleep(0.5)

    def _start_device_setup(self, node_id: int) -> None:
        existing = self._setup_tasks.get(node_id)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(self._setup_device_task(node_id), name=f"pubwatch-setup-{node_id}")
        self._setup_tasks[node_id] = task
        state = self.devices.get(node_id)
        if state is not None:
            state.setup_task = task

    async def _cancel_device_setup(self, node_id: int) -> None:
        task = self._setup_tasks.pop(node_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _setup_device_task(self, node_id: int) -> None:
        try:
            state = self.devices.get(node_id)
            if state is None:
                return
            await self._setup_device(state)
            if self.devices.get(node_id) is not state:
                return
            self._notify_state_changed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("Failed to set up publication watch for node %s: %s", node_id, exc)
        finally:
            self._setup_tasks.pop(node_id, None)
            state = self.devices.get(node_id)
            if state is not None:
                state.setup_task = None

    def _device_still_active(self, state: DeviceWatchState) -> bool:
        return self.devices.get(state.node_id) is state

    async def _setup_device(self, state: DeviceWatchState) -> None:
        # List uavcan.pub.* registers and build the publication catalog.
        registry = Registry(state.node_id, self.client.node.make_client)
        publications = await discover_publication_ports_remote(registry)
        if not self._device_still_active(state):
            return
        state.registry_entries = registry_to_json_entries(registry)
        self._notify_state_changed()
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
        if not self._device_still_active(state):
            device.close()
            return
        state.device = device

        subscribed_subjects: set[int] = set()
        typed_by_subject: dict[int, PublicationPort] = {}
        for port in publications:
            if port.parse_status != "ok" or port.message_type is None:
                continue
            existing = typed_by_subject.get(port.subject_id)
            if existing is None or port.port_name < existing.port_name:
                typed_by_subject[port.subject_id] = port

        for port in typed_by_subject.values():
            if not self._device_still_active(state):
                return
            subscribed_subjects.add(port.subject_id)
            task = asyncio.create_task(
                self._subscriber_loop(state, port),
                name=f"pubwatch-sub-{state.node_id}-{port.port_name}",
            )
            state.subscriber_tasks[port.port_name] = task

        for port in publications:
            if port.subject_id in subscribed_subjects:
                continue
            await self._ensure_unstructured_subscription(state, port.subject_id)
            subscribed_subjects.add(port.subject_id)

        # Observe heartbeat on the standard subject even when not in the pub catalog.
        if HEARTBEAT_SUBJECT_ID not in subscribed_subjects:
            await self._ensure_unstructured_subscription(state, HEARTBEAT_SUBJECT_ID)

    async def _teardown_device(self, state: DeviceWatchState) -> None:
        if state.setup_task is not None and not state.setup_task.done():
            state.setup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.setup_task
            state.setup_task = None
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

            port_name = self._resolve_port_name(state, subject_id)
            if subject_id in state.known_subject_ids:
                parse_status = "missing_dsdl"
            else:
                self._record_unknown(state.node_id, subject_id, byte_count=len(bytes(message.value)))
                parse_status = "missing_dsdl"

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

    @staticmethod
    def _resolve_port_name(state: DeviceWatchState, subject_id: int) -> str | None:
        """Return the canonical publication port name for one subject ID."""
        matches = sorted(port.port_name for port in state.publications.values() if port.subject_id == subject_id)
        return matches[0] if matches else None

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
        canonical_port_name = self._resolve_port_name(state, subject_id)
        if canonical_port_name is not None:
            port_name = canonical_port_name

        stats = state.port_stats.setdefault(subject_id, PortStats())
        stats.record(byte_count=len(str(fields).encode("utf-8")))
        self._message_sequence += 1
        parsed = ParsedMessage(
            node_id=state.node_id,
            port_name=port_name,
            subject_id=subject_id,
            type_name=type_name,
            timestamp_unix=time.time(),
            transfer_id=transfer_id,
            fields=fields,
            parse_status=parse_status,
            sequence=self._message_sequence,
        )
        self.message_buffer.append(parsed)  # rolling history for status queries
        self._pending_messages.append(parsed)  # batch drained by build_status_payload / notify
        history_key = (state.node_id, subject_id)
        port_history = self._port_message_history.get(history_key)
        if port_history is None:
            port_history = deque(maxlen=self.max_messages_per_port)
            self._port_message_history[history_key] = port_history
        port_history.append(parsed)

    def _record_unknown(self, node_id: int, subject_id: int, *, byte_count: int) -> None:
        node_stats = self.unknown_ports.setdefault(node_id, {})
        stats = node_stats.setdefault(subject_id, PortStats())
        stats.record(byte_count=byte_count)

    def _notify_state_changed(self) -> None:
        if self._on_state_changed is not None:
            self._on_state_changed()

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
