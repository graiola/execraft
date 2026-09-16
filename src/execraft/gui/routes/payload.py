"""Strict JSON payload decoders shared by GUI HTTP routes."""

from __future__ import annotations

from typing import Any, Mapping

from execraft.gui.errors import GuiError


def payload_bool(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    """Decode one JSON boolean without Python truthiness coercion."""

    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise GuiError(f"{key} must be a JSON boolean")
    return value


def payload_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int,
) -> int:
    """Decode one JSON integer while rejecting booleans."""

    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GuiError(f"{key} must be a JSON integer")
    return value


def payload_string_list(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Decode a JSON string list without accepting scalar iteration."""

    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise GuiError(f"{key} must be a JSON array of strings")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def payload_int_list(payload: Mapping[str, Any], key: str) -> list[int]:
    """Decode an integer list used by conflict-checked approval endpoints."""

    value = payload.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in value
    ):
        raise GuiError(f"{key} must be a JSON array of integers")
    return value


def string_mapping(value: Any) -> dict[str, str]:
    """Normalize a JSON object whose values are scalar strings."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GuiError("expected a JSON object")
    return {str(key): str(item) for key, item in value.items()}


def string_list_mapping(
    value: Any,
    *,
    mapping_label: str = "repository-to-paths",
    item_label: str = "selected paths",
) -> dict[str, list[str]]:
    """Normalize a JSON object whose values must be ordered string lists."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GuiError(f"expected a {mapping_label} JSON object")
    result: dict[str, list[str]] = {}
    for key, raw in value.items():
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise GuiError(f"{item_label} for {key!r} must be a list of strings")
        result[str(key)] = list(raw)
    return result
