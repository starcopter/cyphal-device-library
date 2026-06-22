"""Tests for registry JSON serialization helpers."""

import uavcan.register

from cyphal_device_library.registry import Register, Registry, registry_to_json_entries


def test_registry_to_json_entries_serializes_values() -> None:
    registry = Registry(node_id=42, client_factory=lambda _subject: None)
    response = uavcan.register.Access_1.Response(
        value=uavcan.register.Value_1(natural16=uavcan.primitive.array.Natural16_1([6060])),
        mutable=True,
        persistent=True,
    )
    registry._insert("uavcan.pub.power_data.id", response)

    entries = registry_to_json_entries(registry)

    assert entries == [
        {
            "name": "uavcan.pub.power_data.id",
            "dtype": "natural16[1]",
            "value": 6060,
            "mutable": True,
            "persistent": True,
        }
    ]
