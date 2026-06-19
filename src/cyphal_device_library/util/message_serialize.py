"""Serialize Cyphal DSDL message instances to JSON-safe structures."""

from __future__ import annotations

from typing import Any

import numpy as np
import pycyphal.dsdl


def serialize_message(
    message: Any,
    *,
    max_depth: int = 8,
    max_items: int = 64,
    max_str: int = 512,
    _depth: int = 0,
) -> Any:
    """Convert one DSDL message instance to a JSON-serializable structure."""
    if _depth > max_depth:
        return "<max_depth>"

    if message is None:
        return None

    if isinstance(message, (bool, int, float)):
        return message

    if isinstance(message, str):
        if len(message) > max_str:
            return message[:max_str] + "…"
        return message

    if isinstance(message, (bytes, bytearray, memoryview)):
        raw = bytes(message)
        if len(raw) > max_items:
            return {"_type": "bytes", "hex": raw[:max_items].hex(), "truncated": True}
        return {"_type": "bytes", "hex": raw.hex()}

    if isinstance(message, np.ndarray):
        values = message.tolist()
        if isinstance(values, list) and len(values) > max_items:
            return values[:max_items] + ["<truncated>"]
        return values

    if isinstance(message, (list, tuple)):
        return [
            serialize_message(item, max_depth=max_depth, max_items=max_items, max_str=max_str, _depth=_depth + 1)
            for item in message[:max_items]
        ]

    try:
        model = pycyphal.dsdl.get_model(message)
    except Exception:
        return str(message)

    if model is None:
        return str(message)

    if hasattr(model, "fields") and model.fields:
        result: dict[str, Any] = {}
        for field in model.fields:
            name = field.name
            try:
                value = getattr(message, name)
            except AttributeError:
                continue
            result[name] = serialize_message(
                value,
                max_depth=max_depth,
                max_items=max_items,
                max_str=max_str,
                _depth=_depth + 1,
            )
        return result

    return str(message)
