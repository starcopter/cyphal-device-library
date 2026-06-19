"""Tests for DSDL type loading."""

import pytest

from cyphal_device_library.util.message_types import load_message_type


def test_load_message_type_uavcan_primitive() -> None:
    message_type = load_message_type("uavcan.primitive.Empty.1.0")
    assert message_type.__name__ == "Empty_1_0"


def test_load_message_type_invalid() -> None:
    with pytest.raises(ValueError):
        load_message_type("not-a-valid-type")


def test_load_message_type_missing_module() -> None:
    with pytest.raises(RuntimeError):
        load_message_type("missing.namespace.Message.1.0")
