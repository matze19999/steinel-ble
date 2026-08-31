"""Decode identity data advertised by STEINEL Connect devices."""

from __future__ import annotations

from typing import Any


def parse_steinel_advertisement(data: bytes, name: str | None = None) -> dict[str, Any]:
    """Return the stable identity fields from manufacturer data."""
    if name == "SfuTg":
        if len(data) < 2:
            return {}
        result: dict[str, Any] = {"product_id": int.from_bytes(data[:2], "little")}
        if len(data) > 3 and data[3]:
            result["bootloader"] = data[3]
        if len(data) >= 6:
            result["hash_id"] = int.from_bytes(data[4:6], "little")
            if data[5]:
                result["hardware"] = data[5]
        return result
    if len(data) < 4:
        return {}
    result = {
        "product_id": int.from_bytes(data[:2], "little"),
        "firmware": f"{data[3]}.{data[2]}.{data[1]}",
    }
    if len(data) > 5 and data[5]:
        result["bootloader"] = data[5]
    if len(data) > 6 and data[6]:
        result["hardware"] = data[6]
    if len(data) >= 9:
        result["hash_id"] = int.from_bytes(data[7:9], "little")
    return result
