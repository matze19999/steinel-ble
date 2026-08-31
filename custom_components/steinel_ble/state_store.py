"""Serialization helpers for acknowledged light state."""

from __future__ import annotations

from typing import Any


def restore_light_states(
    data: object,
) -> tuple[
    dict[int, bool],
    dict[int, int],
    dict[int, int],
    dict[int, tuple[float, float]],
]:
    """Decode persisted states, ignoring malformed values."""
    on: dict[int, bool] = {}
    brightness: dict[int, int] = {}
    temperature: dict[int, int] = {}
    hs: dict[int, tuple[float, float]] = {}
    if not isinstance(data, dict):
        return on, brightness, temperature, hs
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        try:
            address = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value.get("on"), bool):
            on[address] = value["on"]
        if isinstance(value.get("brightness"), int) and 0 <= value["brightness"] <= 255:
            brightness[address] = value["brightness"]
        if isinstance(value.get("temperature"), int):
            temperature[address] = value["temperature"]
        color = value.get("hs")
        if (
            isinstance(color, list)
            and len(color) == 2
            and all(isinstance(item, (int, float)) for item in color)
        ):
            hs[address] = (float(color[0]), float(color[1]))
    return on, brightness, temperature, hs


def serialize_light_states(
    on: dict[int, bool],
    brightness: dict[int, int],
    temperature: dict[int, int],
    hs: dict[int, tuple[float, float]],
) -> dict[str, dict[str, Any]]:
    """Encode all known state fields by element address."""
    result: dict[str, dict[str, Any]] = {}
    for address in on.keys() | brightness.keys() | temperature.keys() | hs.keys():
        item: dict[str, Any] = {}
        if address in on:
            item["on"] = on[address]
        if address in brightness:
            item["brightness"] = brightness[address]
        if address in temperature:
            item["temperature"] = temperature[address]
        if address in hs:
            item["hs"] = list(hs[address])
        result[str(address)] = item
    return result
