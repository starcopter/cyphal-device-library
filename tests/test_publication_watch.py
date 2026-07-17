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
    CATALOG_DISCOVERY_STAGGER_S,
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
        # Yield first so background tasks started during reconcile (e.g. catalog
        # discovery) can run before the loop is marked stopped.
        await _REAL_ASYNCIO_SLEEP(0)
        watcher._stop_event.set()

    with patch("cyphal_device_library.publication_watch.asyncio.sleep", _sleep_and_stop):
        await watcher._device_loop()


async def _await_device_setup_tasks(watcher: BusPublicationWatcher) -> None:
    """Wait for any in-flight per-device setup tasks."""
    tasks = list(watcher._setup_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_catalog_tasks(watcher: BusPublicationWatcher) -> None:
    """Wait for any in-flight catalog discovery tasks."""
    tasks = list(watcher._catalog_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
async def test_reconcile_starts_catalog_discovery_without_subscribers(instant_sleep: None) -> None:
    client = _mock_client()
    client.node_tracker.registry = {42: _heartbeat_entry()}
    watcher = BusPublicationWatcher(client)
    discover = AsyncMock()

    async def _discover(state: DeviceWatchState) -> None:
        await discover(state)
        state.publications["status"] = PublicationPort(
            port_name="status",
            subject_id=6060,
            type_name="uavcan.primitive.Empty.1.0",
            message_type=None,
            parse_status="missing_dsdl",
        )
        state.registry_entries = [{"name": "uavcan.pub.status.id", "value": [6060]}]

    with patch.object(watcher, "_discover_device_catalog", side_effect=_discover):
        await _run_device_loop_once(watcher)
        await _await_catalog_tasks(watcher)

    discover.assert_awaited()
    assert 42 in watcher.devices
    assert watcher.devices[42].subscriber_tasks == {}
    assert "status" in watcher.devices[42].publications


@pytest.mark.asyncio
async def test_reconcile_schedules_staggered_catalog_tasks(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    client.node_tracker.registry = {42: _heartbeat_entry(), 43: _heartbeat_entry()}
    watcher = BusPublicationWatcher(client)
    delays: list[float] = []

    def _start(node_id: int, *, stagger_index: int = 0) -> None:
        delays.append(stagger_index * CATALOG_DISCOVERY_STAGGER_S)
        # still create a no-op completed catalog fill so reconcile finishes cleanly
        state = watcher.devices[node_id]
        state.registry_entries = [{"name": "x"}]

    with patch.object(watcher, "_start_catalog_discovery", side_effect=_start):
        await _run_device_loop_once(watcher)

    assert delays == [0.0, CATALOG_DISCOVERY_STAGGER_S]


@pytest.mark.asyncio
async def test_focus_runs_setup_unfocus_tears_down_subscribers(instant_sleep: None) -> None:
    client = _mock_client()
    client.node_tracker.registry = {42: _heartbeat_entry()}
    watcher = BusPublicationWatcher(client)
    await _run_device_loop_once(watcher)

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "cyphal_device_library.publication_watch.Registry",
            return_value=MagicMock(),
        ),
        patch(
            "cyphal_device_library.publication_watch.registry_to_json_entries",
            return_value=[],
        ),
    ):
        await watcher.focus(42)
        assert 42 in watcher.focused_node_ids

        await watcher.unfocus(42)
        assert 42 not in watcher.focused_node_ids


@pytest.mark.asyncio
async def test_device_loop_adds_remote_nodes(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    client.node_tracker.registry = {42: _heartbeat_entry()}

    watcher = BusPublicationWatcher(client)

    await _run_device_loop_once(watcher)

    assert 42 in watcher.devices
    assert watcher.devices[42].device_info["node_id"] == 42
    assert watcher.devices[42].device_info["uptime_s"] == 10
    assert watcher.devices[42].subscriber_tasks == {}
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
async def test_reconcile_does_not_bump_bus_activity_for_stale_heartbeat(instant_sleep: None) -> None:
    """Frozen heartbeat metadata must not refresh last_bus_activity_unix."""
    client = _mock_client(node_id=1)
    entry = _heartbeat_entry(uptime=10, vssc=5)
    client.node_tracker.registry = {42: entry}
    watcher = BusPublicationWatcher(client)
    watcher.last_bus_activity_unix = 1000.0
    watcher.devices[42] = DeviceWatchState(
        node_id=42,
        device_info=BusPublicationWatcher._serialize_node_entry(42, entry),
    )

    with patch("cyphal_device_library.publication_watch.time.time", return_value=2000.0):
        await _run_device_loop_once(watcher)

    assert watcher.last_bus_activity_unix == 1000.0


@pytest.mark.asyncio
async def test_reconcile_bumps_bus_activity_when_heartbeat_changes(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    client.node_tracker.registry = {42: _heartbeat_entry(uptime=10, vssc=5)}
    watcher = BusPublicationWatcher(client)
    watcher.last_bus_activity_unix = 1000.0
    watcher.devices[42] = DeviceWatchState(
        node_id=42,
        device_info=BusPublicationWatcher._serialize_node_entry(42, _heartbeat_entry(uptime=10, vssc=5)),
    )

    client.node_tracker.registry[42] = _heartbeat_entry(uptime=11, vssc=5)
    with patch("cyphal_device_library.publication_watch.time.time", return_value=2000.0):
        await _run_device_loop_once(watcher)

    assert watcher.last_bus_activity_unix == 2000.0


@pytest.mark.asyncio
async def test_record_message_bumps_bus_activity(instant_sleep: None) -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    watcher.last_bus_activity_unix = 1000.0
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})

    with patch("cyphal_device_library.publication_watch.time.time", return_value=2000.0):
        await watcher._record_message(
            state=state,
            port_name="status",
            subject_id=6060,
            type_name="uavcan.primitive.Empty.1.0",
            fields={},
            transfer_id=1,
            parse_status="ok",
        )

    assert watcher.last_bus_activity_unix == 2000.0


@pytest.mark.asyncio
async def test_device_loop_registers_devices_before_setup_completes(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    client.node_tracker.registry = {
        42: _heartbeat_entry(),
        43: _heartbeat_entry(),
    }

    setup_started: list[int] = []
    setup_release = asyncio.Event()

    async def _setup_device(state: DeviceWatchState) -> None:
        setup_started.append(state.node_id)
        await setup_release.wait()

    watcher = BusPublicationWatcher(client)
    watcher._setup_device = _setup_device  # type: ignore[method-assign]

    await _run_device_loop_once(watcher)

    assert 42 in watcher.devices
    assert 43 in watcher.devices
    assert watcher.devices[42].device_info["node_id"] == 42
    assert watcher.devices[43].device_info["node_id"] == 43
    assert setup_started == []

    focus_tasks = [
        asyncio.create_task(watcher.focus(42)),
        asyncio.create_task(watcher.focus(43)),
    ]
    for _ in range(10):
        await _REAL_ASYNCIO_SLEEP(0)
        if sorted(setup_started) == [42, 43]:
            break

    assert sorted(setup_started) == [42, 43]

    setup_release.set()
    await asyncio.gather(*focus_tasks)


@pytest.mark.asyncio
async def test_device_loop_notifies_when_device_is_registered(instant_sleep: None) -> None:
    client = _mock_client(node_id=1)
    client.node_tracker.registry = {42: _heartbeat_entry()}
    notify_calls = 0

    async def _setup_device(state: DeviceWatchState) -> None:
        await _REAL_ASYNCIO_SLEEP(0)

    def _notify() -> None:
        nonlocal notify_calls
        notify_calls += 1

    watcher = BusPublicationWatcher(client, on_state_changed=_notify)
    watcher._setup_device = _setup_device  # type: ignore[method-assign]

    await _run_device_loop_once(watcher)

    assert notify_calls >= 1
    assert 42 in watcher.devices

    await _await_device_setup_tasks(watcher)


@pytest.mark.asyncio
async def test_stop_tears_down_watched_devices() -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)

    async def _hang_forever() -> None:
        await asyncio.Event().wait()

    hang_task = asyncio.create_task(_hang_forever())

    watcher.devices[42] = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        subscriber_tasks={"status": hang_task},
    )
    watcher.unknown_ports[42] = {8080: PortStats()}

    await watcher.stop()

    assert watcher.devices == {}
    assert watcher.unknown_ports == {}
    assert hang_task.cancelled() or hang_task.done()


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

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(return_value=[typed_port, missing_port]),
        ),
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()) as ensure_unstructured,
    ):
        state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
        watcher.devices[42] = state
        watcher._focused_node_ids.add(42)
        await watcher._setup_device(state)
        await asyncio.sleep(0)
        for task in state.subscriber_tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    assert set(state.publications) == {"status", "custom"}
    assert "status" in state.subscriber_tasks
    assert "custom" not in state.subscriber_tasks
    client.node.make_subscriber.assert_called()
    ensure_unstructured.assert_any_await(state, missing_port.subject_id)
    ensure_unstructured.assert_any_await(state, HEARTBEAT_SUBJECT_ID)


@pytest.mark.asyncio
async def test_setup_device_does_not_construct_device_even_for_node_zero() -> None:
    """Node-ID 0 (motherboard) must not go through Device init re-fetch storm."""
    client = _mock_client(node_id=126)
    watcher = BusPublicationWatcher(client)
    typed_port = PublicationPort(
        port_name="DC_bus",
        subject_id=6008,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(return_value=[typed_port]),
        ),
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()),
    ):
        state = DeviceWatchState(node_id=0, device_info={"node_id": 0})
        watcher.devices[0] = state
        watcher._focused_node_ids.add(0)
        await watcher._setup_device(state)
        await asyncio.sleep(0)
        for task in list(state.subscriber_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    assert "DC_bus" in state.subscriber_tasks
    client.node.make_subscriber.assert_called()


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

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(return_value=[typed_port]),
        ),
        patch(
            "cyphal_device_library.publication_watch.registry_to_json_entries",
            return_value=[{"name": "uavcan.pub.status.id", "dtype": "natural16[1]", "value": [6060]}],
        ) as serialize_registry,
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()),
    ):
        state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
        watcher.devices[42] = state
        watcher._focused_node_ids.add(42)
        await watcher._setup_device(state)
        for task in state.subscriber_tasks.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    serialize_registry.assert_called_once()
    assert state.registry_entries[0]["name"] == "uavcan.pub.status.id"
    assert notifications == [None]


@pytest.mark.asyncio
async def test_teardown_device_cancels_tasks_and_closes_subscribers() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)

    async def _hang_forever() -> None:
        await asyncio.Event().wait()

    typed_task = asyncio.create_task(_hang_forever(), name="typed")
    unstructured_task = asyncio.create_task(_hang_forever(), name="unstructured")
    typed_sub = MagicMock()
    typed_sub.close = MagicMock()

    state = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        subscriber_tasks={"status": typed_task},
        unstructured_tasks={HEARTBEAT_SUBJECT_ID: unstructured_task},
        typed_subscribers={"status": typed_sub},
    )

    await watcher._teardown_device(state)

    assert typed_task.cancelled() or typed_task.done()
    assert unstructured_task.cancelled() or unstructured_task.done()
    assert state.subscriber_tasks == {}
    assert state.unstructured_tasks == {}
    typed_sub.close.assert_called_once()
    assert state.typed_subscribers == {}


@pytest.mark.asyncio
async def test_setup_device_skips_unset_subject_ids_and_still_subscribes() -> None:
    """Unset Cyphal port-IDs (65535) must not abort focus setup or poison unfocus."""
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    unset_typed = PublicationPort(
        port_name="timekeeper_status",
        subject_id=65535,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    unset_unstructured = PublicationPort(
        port_name="custom_unset",
        subject_id=65535,
        type_name="missing.namespace.Message.1.0",
        message_type=None,
        parse_status="missing_dsdl",
    )
    ok_port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(return_value=[unset_typed, unset_unstructured, ok_port]),
        ),
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()) as ensure_unstructured,
    ):
        state = DeviceWatchState(node_id=0, device_info={"node_id": 0})
        watcher.devices[0] = state
        watcher._focused_node_ids.add(0)
        await watcher._setup_device(state)
        for task in list(state.subscriber_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    assert set(state.publications) == {"timekeeper_status", "custom_unset", "status"}
    assert "status" in state.subscriber_tasks
    assert "timekeeper_status" not in state.subscriber_tasks
    ensure_unstructured.assert_any_await(state, HEARTBEAT_SUBJECT_ID)
    assert all(call.args[1] != 65535 for call in ensure_unstructured.await_args_list)


@pytest.mark.asyncio
async def test_teardown_device_ignores_failed_subscriber_task_exceptions() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)

    async def _fail_like_unset_port() -> None:
        raise ValueError("Default port-ID 65535 is not valid for a sub-port")

    failed_task = asyncio.create_task(_fail_like_unset_port(), name="unset-port")
    await asyncio.sleep(0)
    assert failed_task.done()

    state = DeviceWatchState(
        node_id=0,
        device_info={"node_id": 0},
        subscriber_tasks={"timekeeper_status": failed_task},
    )

    await watcher._teardown_device(state)

    assert state.subscriber_tasks == {}
    assert state.typed_subscribers == {}


@pytest.mark.asyncio
async def test_subscriber_loop_records_matching_messages() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
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

    client.node.make_subscriber.return_value = _subscription()
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
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
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

    client.node.make_subscriber.return_value = _subscription()
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
async def test_ensure_unstructured_subscription_uses_typed_heartbeat_subscriber() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})

    await watcher._ensure_unstructured_subscription(state, HEARTBEAT_SUBJECT_ID)

    client.node.make_subscriber.assert_called_once_with(uavcan.node.Heartbeat_1_0, HEARTBEAT_SUBJECT_ID)
    task = state.unstructured_tasks[HEARTBEAT_SUBJECT_ID]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_heartbeat_loop_records_typed_heartbeat_message() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
    subscriber = MagicMock()
    metadata = SimpleNamespace(source_node_id=42, transfer_id=5)
    heartbeat = uavcan.node.Heartbeat_1_0(
        uptime=10,
        health=uavcan.node.Health_1_0(0),
        mode=uavcan.node.Mode_1_0(0),
        vendor_specific_status_code=1,
    )

    async def _subscription() -> object:
        yield heartbeat, metadata
        watcher._stop_event.set()

    subscriber.__aiter__ = lambda self: _subscription()
    await watcher._heartbeat_loop(state, subscriber)

    assert len(watcher.message_buffer) == 1
    parsed = watcher.message_buffer[0]
    assert parsed.subject_id == HEARTBEAT_SUBJECT_ID
    assert parsed.type_name == "uavcan.node.Heartbeat.1.0"
    assert parsed.parse_status == "ok"


@pytest.mark.asyncio
async def test_unstructured_loop_handles_unexpected_typed_message_without_crashing() -> None:
    """Regression test: pycyphal shares one Subscriber impl per subject-ID regardless of
    the requested dtype, so `_unstructured_loop` may receive an already-typed message (as
    happened for HEARTBEAT_SUBJECT_ID before `_heartbeat_loop` existed) instead of
    `Unstructured_1`. It must serialize defensively rather than crash on `message.value`.
    """
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
    subscriber = MagicMock()
    metadata = SimpleNamespace(source_node_id=42, transfer_id=1)
    heartbeat = uavcan.node.Heartbeat_1_0(
        uptime=1,
        health=uavcan.node.Health_1_0(0),
        mode=uavcan.node.Mode_1_0(0),
        vendor_specific_status_code=0,
    )

    async def _subscription() -> object:
        yield heartbeat, metadata
        watcher._stop_event.set()

    subscriber.__aiter__ = lambda self: _subscription()
    await watcher._unstructured_loop(state, HEARTBEAT_SUBJECT_ID, subscriber)

    assert len(watcher.message_buffer) == 1
    parsed = watcher.message_buffer[0]
    assert parsed.parse_status == "ok"
    assert parsed.type_name == "Heartbeat_1_0"


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


def test_build_status_payload_defaults_omit_registry_and_history() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    state = DeviceWatchState(node_id=7, device_info={"node_id": 7, "name": "x"})
    state.registry_entries = [{"name": "uavcan.node.id", "dtype": "natural8", "value": 7}]
    state.publications = {
        "bms_data": PublicationPort(
            port_name="bms_data",
            subject_id=100,
            type_name="x",
            dt_ms=None,
            parse_status="ok",
            message_type=None,
        )
    }
    watcher.devices[7] = state
    watcher._pending_messages.append(
        ParsedMessage(
            node_id=7,
            port_name="bms_data",
            subject_id=100,
            type_name="x",
            timestamp_unix=1.0,
            transfer_id=1,
            fields={"a": 1},
            sequence=1,
        )
    )

    payload = watcher.build_status_payload()

    assert "registry" not in payload["devices"][0]
    assert "message_history" not in payload
    assert len(payload["messages"]) == 1


def test_build_focus_status_payload_includes_registry_and_node_history() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client, max_messages_per_port=3)
    state = DeviceWatchState(node_id=7, device_info={"node_id": 7, "name": "x"})
    state.registry_entries = [{"name": "uavcan.node.id", "dtype": "natural8", "value": 7}]
    watcher.devices[7] = state
    other = DeviceWatchState(node_id=8, device_info={"node_id": 8, "name": "y"})
    watcher.devices[8] = other

    for index, node_id in enumerate((7, 7, 8)):
        asyncio.run(
            watcher._record_message(
                state=state if node_id == 7 else other,
                port_name="temp_data",
                subject_id=6061,
                type_name="test.Type.1.0",
                fields={"index": index},
                transfer_id=index,
                parse_status="ok",
            )
        )

    payload = watcher.build_focus_status_payload(7)

    assert payload["devices"][0]["registry"] == state.registry_entries
    assert "message_history" in payload
    assert all(item["node_id"] == 7 for item in payload["message_history"])
    assert len(payload["messages"]) == 3


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
    assert "message_history" not in payload
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

    payload = watcher.build_status_payload(include_message_history=True)
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


@pytest.mark.asyncio
async def test_unfocus_keeps_publications_and_registry() -> None:
    client = _mock_client()
    watcher = BusPublicationWatcher(client)
    port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    state = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        publications={"status": port},
        registry_entries=[{"name": "uavcan.pub.status.id", "value": [6060]}],
        known_subject_ids={6060},
        port_stats={6060: PortStats()},
    )
    watcher.devices[42] = state
    watcher._focused_node_ids.add(42)
    watcher.unknown_ports[42] = {9999: PortStats()}

    await watcher.unfocus(42)

    assert 42 not in watcher.focused_node_ids
    assert state.publications == {"status": port}
    assert state.registry_entries == [{"name": "uavcan.pub.status.id", "value": [6060]}]
    assert state.known_subject_ids == {6060}
    assert state.port_stats == {}
    assert 42 not in watcher.unknown_ports
    assert state.subscriber_tasks == {}


@pytest.mark.asyncio
async def test_focus_with_warm_catalog_skips_rediscovery() -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    state = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        publications={"status": port},
        registry_entries=[{"name": "uavcan.pub.status.id", "value": [6060]}],
        known_subject_ids={6060},
    )
    watcher.devices[42] = state

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(),
        ) as discover,
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()),
    ):
        await watcher.focus(42)
        for task in list(state.subscriber_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    discover.assert_not_awaited()
    assert "status" in state.subscriber_tasks or client.node.make_subscriber.called


@pytest.mark.asyncio
async def test_focus_awaits_in_flight_catalog_then_subscribes() -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
    watcher.devices[42] = state
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_discover(s: DeviceWatchState) -> None:
        started.set()
        await release.wait()
        s.publications = {"status": port}
        s.registry_entries = [{"name": "uavcan.pub.status.id", "value": [6060]}]
        s.known_subject_ids = {6060}

    with (
        patch.object(watcher, "_discover_device_catalog", side_effect=_slow_discover),
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()),
    ):
        catalog_task = asyncio.create_task(watcher._catalog_discovery_task(42, 0.0))
        watcher._catalog_tasks[42] = catalog_task
        await started.wait()
        focus_task = asyncio.create_task(watcher.focus(42))
        await asyncio.sleep(0)
        assert not focus_task.done()
        release.set()
        await focus_task
        for task in list(state.subscriber_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    assert "status" in state.publications
    assert 42 in watcher.focused_node_ids


@pytest.mark.asyncio
async def test_refocus_starts_subscribers_from_cached_catalog() -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    state = DeviceWatchState(
        node_id=42,
        device_info={"node_id": 42},
        publications={"status": port},
        registry_entries=[{"name": "uavcan.pub.status.id", "value": [6060]}],
        known_subject_ids={6060},
    )
    watcher.devices[42] = state

    with (
        patch(
            "cyphal_device_library.publication_watch.discover_publication_ports_remote",
            AsyncMock(),
        ) as discover,
        patch.object(watcher, "_ensure_unstructured_subscription", AsyncMock()),
    ):
        await watcher.focus(42)
        await watcher.unfocus(42)
        assert state.publications  # still warm
        await watcher.focus(42)
        for task in list(state.subscriber_tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    assert discover.await_count == 0


@pytest.mark.asyncio
async def test_discover_device_catalog_does_not_start_subscribers() -> None:
    client = _mock_client(node_id=1)
    watcher = BusPublicationWatcher(client)
    port = PublicationPort(
        port_name="status",
        subject_id=6060,
        type_name="uavcan.primitive.Empty.1.0",
        message_type=load_message_type("uavcan.primitive.Empty.1.0"),
        parse_status="ok",
    )
    state = DeviceWatchState(node_id=42, device_info={"node_id": 42})
    watcher.devices[42] = state

    with patch(
        "cyphal_device_library.publication_watch.discover_publication_ports_remote",
        AsyncMock(return_value=[port]),
    ):
        await watcher._discover_device_catalog(state)

    assert state.publications == {"status": port}
    assert state.subscriber_tasks == {}
    assert state.unstructured_tasks == {}
