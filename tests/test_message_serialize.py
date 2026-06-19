"""Tests for DSDL message serialization."""

import numpy as np
import uavcan.node
import uavcan.primitive

from cyphal_device_library.util.message_serialize import serialize_message


def test_serialize_message_none_and_scalars() -> None:
    assert serialize_message(None) is None
    assert serialize_message(True) is True
    assert serialize_message(42) == 42
    assert serialize_message(3.14) == 3.14


def test_serialize_message_string_truncation() -> None:
    assert serialize_message("short") == "short"
    assert serialize_message("x" * 20, max_str=10) == "x" * 10 + "…"


def test_serialize_message_bytes() -> None:
    short = b"\x01\x02\xab"
    assert serialize_message(short) == {"_type": "bytes", "hex": short.hex()}
    assert serialize_message(bytearray(short)) == {"_type": "bytes", "hex": short.hex()}
    assert serialize_message(memoryview(short)) == {"_type": "bytes", "hex": short.hex()}

    long_bytes = bytes(range(256))
    truncated = serialize_message(long_bytes, max_items=8)
    assert truncated == {
        "_type": "bytes",
        "hex": long_bytes[:8].hex(),
        "truncated": True,
    }


def test_serialize_message_numpy_array() -> None:
    assert serialize_message(np.array([1, 2, 3])) == [1, 2, 3]

    truncated = serialize_message(np.array(list(range(100))), max_items=5)
    assert truncated == [0, 1, 2, 3, 4, "<truncated>"]


def test_serialize_message_sequence_max_items() -> None:
    items = list(range(100))
    assert serialize_message(items, max_items=10) == list(range(10))
    assert serialize_message(tuple(items), max_items=10) == list(range(10))


def test_serialize_message_max_depth() -> None:
    nested = [[[["leaf"]]]]
    assert serialize_message(nested, max_depth=1) == [["<max_depth>"]]
    assert serialize_message(nested, max_depth=2) == [[["<max_depth>"]]]


def test_serialize_message_dsdl_field_traversal() -> None:
    heartbeat = uavcan.node.Heartbeat_1_0(
        uptime=42,
        health=uavcan.node.Health_1_0(0),
        mode=uavcan.node.Mode_1_0(0),
        vendor_specific_status_code=7,
    )

    assert serialize_message(heartbeat) == {
        "uptime": 42,
        "health": {"value": 0},
        "mode": {"value": 0},
        "vendor_specific_status_code": 7,
    }

    string_message = uavcan.primitive.String_1_0(value=b"hello")
    assert serialize_message(string_message) == {
        "value": [104, 101, 108, 108, 111],
    }


def test_serialize_message_empty_dsdl_message_falls_back_to_str() -> None:
    empty = uavcan.primitive.Empty_1_0()
    assert serialize_message(empty) == str(empty)


def test_serialize_message_unknown_object_falls_back_to_str() -> None:
    class _Custom:
        def __str__(self) -> str:
            return "custom-value"

    assert serialize_message(_Custom()) == "custom-value"
