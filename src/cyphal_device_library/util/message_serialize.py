"""Serialize Cyphal DSDL message instances to JSON-safe structures."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pycyphal.dsdl

JSON_NAN = "NAN"
JSON_POS_INF = "INF"
JSON_NEG_INF = "-INF"


def json_safe_number(value: float) -> float | str:
    """Return a JSON number, or a string label for NaN / ±Inf.

    Python ``json.dumps`` emits bare ``NaN`` / ``Infinity`` by default. Those
    tokens are not valid JSON, so browsers reject the whole websocket payload.
    """
    numeric = float(value)
    if math.isnan(numeric):
        return JSON_NAN
    if math.isinf(numeric):
        return JSON_POS_INF if numeric > 0 else JSON_NEG_INF
    return numeric


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

    if isinstance(message, bool):
        return message
    if isinstance(message, int):
        return message
    if isinstance(message, float):
        return json_safe_number(message)
    if isinstance(message, np.generic):
        if isinstance(message, np.floating):
            return json_safe_number(float(message))
        return message.item()

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
        truncated = isinstance(values, list) and len(values) > max_items
        if truncated:
            values = values[:max_items]
        serialized = [
            serialize_message(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_str=max_str,
                _depth=_depth + 1,
            )
            for item in values
        ]
        if truncated:
            serialized.append("<truncated>")
        return serialized

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
            if not name:
                continue
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


def ensure_json_serializable(
    value: Any,
    *,
    max_depth: int = 8,
    max_items: int = 64,
    max_str: int = 512,
    _depth: int = 0,
) -> Any:
    """Coerce a nested structure to JSON-safe values."""
    if _depth > max_depth:
        return "<max_depth>"

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return json_safe_number(value)

    if isinstance(value, dict):
        return {
            str(key): ensure_json_serializable(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_str=max_str,
                _depth=_depth + 1,
            )
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        items = list(value[:max_items])
        if len(value) > max_items:
            items.append("<truncated>")
        return [
            ensure_json_serializable(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_str=max_str,
                _depth=_depth + 1,
            )
            for item in items
        ]

    return serialize_message(
        value,
        max_depth=max_depth,
        max_items=max_items,
        max_str=max_str,
        _depth=_depth,
    )
