"""Tests for BusPublicationWatcher lifecycle and state reconciliation."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uavcan.node
import uavcan.primitive
from pycyphal.application.node_tracker import Entry

from cyphal_device_library.publication_watch import (
    HEARTBEAT_SUBJECT_ID,
    BusPublicationWatcher,
    DeviceWatchState,
    ParsedMessage,
    PortStats,
    PublicationPort,
)
from cyphal_device_library.util.message_types import load_message_type

_REAL_ASYNCIO_SLEEP = asyncio.sleep


def _heartbeat_entry(*, uptime: int = 10, vssc: int = 5) -> Entry:
    heartbeat = uavcan.node.Heartbeat_1_0(
        uptime=uptime,
        health=uavcan.node.Health_1_0(0),
        mode=uavcan.node.Mode_1_0(0),
        vendor_specific_status_code=vssc,
    )
    return Entry(heartbeat=heartbeat, info=None)


def _mock_client(*, node_id: int = 1) -> MagicMock:
    client = MagicMock()
    client.node.id = node_id
    client.node.make_client = MagicMock()
    client.node.make_subscriber = MagicMock()
    client.node_tracker.registry = {}
    return client


@pytest.fixture
def instant_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_duration: float) -> None:
        await _REAL_ASYNCIO_SLEEP(0)

    monkeypatch.setattr("cyphal_device_library.publication_watch.asyncio.sleep", _sleep)


async def _run_device_loop_once(watcher: BusPublicationWatcher) -> None:
    """Run one reconciliation pass then stop the watcher."""

    async def _sleep_and_stop(_duration: float) -> None:
        watcher._stop_event.set()
        await _REAL_ASYNCIO_SLEEP(0)

    with patch("cyphal_device_library.publication_watch.asyncio.sleep", _sleep_and_stop):
        await watcher._device_loop()


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle(instant_sleep: None) -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)

    assert watcher.is_running is False

    await watcher.start()
    assert watcher.is_running is True

    await watcher.start()
    assert watcher.is_running is True

    await watcher.stop()
    assert watcher.is_running is False
    assert watcher.devices == {}
    assert watcher.unknown_ports == {}


@pytest.mark.asyncio
async def test_device_loop_adds_remote_nodes(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    client.node_tracker.registry = {42: _heartbeat_entry()}

    watcher = BusPublicationWatcher(client)
    setup_calls: list[int] = []

    async def _setup_device(state: DeviceWatchState) -> None:
        setup_calls.append(state.node_id)
        state.device_info["setup"] = True

    watcher._setup_device = _setup_device  # type: ignore[method-assign]

    await _run_device_loop_once(watcher)

    assert setup_calls == [42]
    assert 42 in watcher.devices
    assert watcher.devices[42].device_info["node_id"] == 42
    assert watcher.devices[42].device_info["uptime_s"] == 10
    assert 1 not in watcher.devices


@pytest.mark.asyncio
async def test_device_loop_removes_departed_nodes(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    watcher.devices[42] = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
    )
    watcher.unknown_ports[42] = {999: PortStats()}

    teardown_calls: list[int] = []

    async def _teardown_device(state: DeviceWatchState) -> None:
        teardown_calls.append(state.node_id)

    watcher._teardown_device = _teardown_device  # type: ignore[method-assign]
    client.node_tracker.registry = {}

    await _run_device_loop_once(watcher)

    assert teardown_calls == [42]
    assert watcher.devices == {}
    assert watcher.unknown_ports == {}


@pytest.mark.asyncio
async def test_device_loop_refreshes_existing_node_metadata(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    client.node_tracker.registry = {42: _heartbeat_entry(uptime=10, vssc=5)}
    watcher = BusPublicationWatcher(client)
    watcher.devices[42] = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42, "uptime_s": 1},
    )

    client.node_tracker.registry[42] = _heartbeat_entry(uptime=99, vssc=7)
    await _run_device_loop_once(watcher)

    assert watcher.devices[42].device_info["uptime_s"] == 99
    assert watcher.devices[42].device_info["vssc"] == 7


@pytest.mark.asyncio
async def test_stop_tears_down_watched_devices() -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    mock_device = MagicMock()

    async def _hang_forever() -> None:
        await asyncio.Event().wait()

    hang_task = asyncio.create_task(_hang_forever())

    watcher.devices[42] = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        device=mock_device,
        subscriber_tasks={"status": hang_task},
    )
    watcher.unknown_ports[42] = {8080: PortStats()}

    await watcher.stop()

    assert watcher.devices == {}
    assert watcher.unknown_ports == {}
    assert hang_task.cancelled() or hang_task.done()
    mock_device.close.assert_called_once()


@pytest.mark.asyncio
async def test_setup_device_uses_typed_and_unstructured_subscriptions() -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    typed_port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    missing_port = PublicationPort(
        port_name="custom",
        subject_id=7070,
        type_name="missing.namespace.Message.1.0",
        message_type=None,
        parse_status="missing_dsdl",
    )

    mock_device = MagicMock()
    mock_device.wait_for_initialization = AsyncMock()

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(return_value=[typed_port, missing_port]),
        ),
        patch("cyphal_device_library.publication_watch.Device", return_value=mock_device) as device_cls,
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()) as ensure_unstructured,
    ):
        state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
        await watcher._setup_device(state)
        for task in state.subscriber_tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    device_cls.assert_called_once()
    assert device_cls.call_args.kwargs["owns_client"] is False
    assert mock_device is state.device
    assert set(state.publications) == {"status", "custom"}
    assert "status" in state.subscriber_tasks
    assert "custom" not in state.subscriber_tasks
    ensure_unstructured.assert_any_await(state, missing_port.subject_id)
    ensure_unstructured.assert_any_await(state, HEARTBEAT_SUBJECT_ID)


@pytest.mark.asyncio
async def test_setup_device_pushes_registry_snapshot() -> None:
    client = _mock_client(node_id=1)
    notifications: list[None] = []
    watcher = BusPublicationWatcher(
        client,
        on_state_changed=lambda: notifications.append(None),
    )
    typed_port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )

    mock_device = MagicMock()
    mock_device.wait_for_initialization = AsyncMock()

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(return_value=[typed_port]),
        ),
        patch(
            "cyphal_device_library.publication_watch.registry_to_json_entries",
            return_value=[{"name": "uavcan.pub.status.id", "dtype": "natural16[1]", "value": [6060]}],
        ) as serialize_registry,
        patch("cyphal_device_library.publication_watch.Device", return_value=mock_device),
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()),
    ):
        state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
        await watcher._setup_device(state)
        for task in state.subscriber_tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    serialize_registry.assert_called_once()
    assert state.registry_entries[0]["name"] == "uavcan.pub.status.id"
    assert notifications == [None]


@pytest.mark.asyncio
async def test_teardown_device_cancels_tasks_and_closes_device() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)

    async def _hang_forever() -> None:
        await asyncio.Event().wait()

    typed_task = asyncio.create_task(_hang_forever(), name="typed")
    unstructured_task = asyncio.create_task(_hang_forever(), name="unstructured")
    mock_device = MagicMock()

    state = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        device=mock_device,
        subscriber_tasks={"status": typed_task},
        unstructured_tasks={HEARTBEAT_SUBJECT_ID: unstructured_task},
    )

    await watcher._teardown_device(state)

    assert typed_task.cancelled() or typed_task.done()
    assert unstructured_task.cancelled() or unstructured_task.done()
    assert state.subscriber_tasks == {}
    assert state.unstructured_tasks == {}
    mock_device.close.assert_called_once()
    assert state.device is None


@pytest.mark.asyncio
async def test_subscriber_loop_records_matching_messages() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    mock_device = MagicMock()
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42}, device=mock_device)
    port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    message = uavcan.primitive.Empty_1_0()
    metadata = SimpleNamespace(source_node_id=42, transfer_id=3)

    async def _subscription() -> object:
        yield message, metadata
        watcher._stop_event.set()

    mock_device.get_subscription.return_value = _subscription()
    task = asyncio.create_task(watcher._subscriber_loop(state, port))
    await task

    assert len(watcher.message_buffer) == 1
    parsed = watcher.message_buffer[0]
    assert parsed.node_id == 42
    assert parsed.port_name == "status"
    assert parsed.parse_status == "ok"


@pytest.mark.asyncio
async def test_subscriber_loop_ignores_other_source_nodes() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    mock_device = MagicMock()
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42}, device=mock_device)
    port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    metadata = SimpleNamespace(source_node_id=99, transfer_id=1)

    async def _subscription() -> object:
        yield uavcan.primitive.Empty_1_0(), metadata
        watcher._stop_event.set()

    mock_device.get_subscription.return_value = _subscription()
    await watcher._subscriber_loop(state, port)

    assert len(watcher.message_buffer) == 0


@pytest.mark.asyncio
async def test_unstructured_loop_tracks_unknown_ports() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    state = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        known_subject_ids={6060},
    )
    subscriber = MagicMock()
    metadata = SimpleNamespace(source_node_id=42, transfer_id=8)
    payload = uavcan.primitive.Unstructured_1_0(value=bytes([1, 2, 3]))

    async def _subscription() -> object:
        yield payload, metadata
        watcher._stop_event.set()

    subscriber.__aiter__ = lambda self: _subscription()
    await watcher._unstructured_loop(state, 9999, subscriber)

    assert 42 in watcher.unknown_ports
    assert 9999 in watcher.unknown_ports[42]
    assert watcher.unknown_ports[42][9999].count == 1
    assert len(watcher.message_buffer) == 1
    assert watcher.message_buffer[0].port_name is None
    assert watcher.message_buffer[0].parse_status == "missing_dsdl"


@pytest.mark.asyncio
async def test_unstructured_loop_keeps_catalogued_port_name_without_dsdl() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    missing_port = PublicationPort(
        port_name="custom",
        subject_id=7070,
        type_name="missing.namespace.Message.1.0",
        message_type=None,
        parse_status="missing_dsdl",
    )
    state = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        publications={"custom": missing_port},
        known_subject_ids={7070},
    )
    subscriber = MagicMock()
    metadata = SimpleNamespace(source_node_id=42, transfer_id=2)

    async def _subscription() -> object:
        yield uavcan.primitive.Unstructured_1_0(value=b"\xaa\xbb"), metadata
        watcher._stop_event.set()

    subscriber.__aiter__ = lambda self: _subscription()
    await watcher._unstructured_loop(state, 7070, subscriber)

    assert watcher.unknown_ports == {}
    parsed = watcher.message_buffer[0]
    assert parsed.port_name == "custom"
    assert parsed.subject_id == 7070


@pytest.mark.asyncio
async def test_record_message_updates_stats_and_pending_queue() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client, max_messages=2)
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})

    for index in range(3):
        await watcher._record_message(
            state=state,
            port_name="status",
            subject_id=6060,
            type_name="uavcan.primitive.Empty.1.0",
            fields={"index": index},
            transfer_id=index,
            parse_status="ok",
        )

    assert len(watcher.message_buffer) == 2
    assert watcher.message_buffer[0].fields["index"] == 1
    assert watcher.message_buffer[1].fields["index"] == 2
    assert len(watcher._pending_messages) == 3
    assert state.port_stats[6060].count == 3


def test_drain_pending_messages_and_build_status_payload() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    state = DeviceWatchState(
        node_id=42,
        device_info=BusPublicationWatcher._serialize_node_entry(42, _heartbeat_entry()),
        publications={
            "status": PublicationPort(
                port_name="status",
                subject_id=6060,
                type_name="uavcan.primitive.Empty.1.0",
                parse_status="ok",
            )
        },
    )
    state.port_stats[6060] = PortStats(count=2, bytes_total=10)
    watcher.devices[42] = state
    watcher.unknown_ports[99] = {8080: PortStats(count=1, bytes_total=4)}
    watcher._pending_messages.extend(
        [
            ParsedMessage(
                node_id=42,
                port_name="status",
                subject_id=6060,
                type_name="uavcan.primitive.Empty.1.0",
                timestamp_unix=1.0,
                transfer_id=1,
                fields={},
            )
        ]
    )

    payload = watcher.build_status_payload(message_limit=10)

    assert payload["devices"][0]["node_id"] == 42
    assert payload["devices"][0]["publications"][0]["port_name"] == "status"
    assert payload["messages"][0]["port_name"] == "status"
    assert payload["message_history"] == []
    assert payload["unknown_ports"][0]["node_id"] == 99
    assert payload["unknown_ports"][0]["subject_id"] == 8080
    assert payload["port_stats"][0]["count"] == 2
    assert watcher.drain_pending_messages() == []

    batch = watcher.drain_pending_messages(limit=1)
    assert batch == []
    assert "updated_at_unix" in payload


def test_build_status_payload_includes_per_subject_message_history() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client, max_messages_per_port=3)
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
    watcher.devices[42] = state

    for index in range(4):
        asyncio.run(
            watcher._record_message(
                state=state,
                port_name="temp_data",
                subject_id=6061,
                type_name="test.Type.1.0",
                fields={"index": index},
                transfer_id=index,
                parse_status="ok",
            )
        )

    payload = watcher.build_status_payload()
    history = payload["message_history"]
    assert len(history) == 3
    assert [item["fields"]["index"] for item in history] == [1, 2, 3]
    assert state.port_stats[6061].count == 4


def test_serialize_node_entry_without_info() -> None:
    payload = BusPublicationWatcher._serialize_node_entry(42, _heartbeat_entry(uptime=12, vssc=9))
    assert payload["node_id"] == 42
    assert payload["uptime_s"] == 12
    assert payload["vssc"] == 9
    assert payload["vssc_hex"] == "0x09"
    assert payload["name"] is None
