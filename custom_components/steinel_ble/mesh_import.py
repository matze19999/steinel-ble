"""Read existing nodes and bindings from a STEINEL Mesh JSON export."""

from __future__ import annotations

import json
import re
from typing import Any

from .const import (
    CONF_APP_KEY,
    CONF_DEVICE_KEY,
    CONF_ELEMENT_COUNT,
    CONF_ELEMENTS,
    CONF_EXISTING_MESH,
    CONF_IMPORTED_ELEMENTS,
    CONF_IV_INDEX,
    CONF_MODEL_SCHEMA_VERSION,
    CONF_NET_KEY,
    CONF_NODE_STATE,
    CONF_SEQUENCE,
    CONF_SOURCE_ADDRESS,
    CONF_UNICAST_ADDRESS,
    MODEL_SCHEMA_VERSION,
    NODE_COMPLETE,
)


def mesh_address(value: str) -> int:
    """Parse a hexadecimal address, as defined by the Mesh JSON format."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{4}", value):
        raise ValueError("Invalid Mesh address")
    return int(value, 16)


def _key(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise ValueError("Invalid Mesh key")
    return value.lower()


def _index(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFF:
        raise ValueError("Invalid Mesh key index")
    return value


def read_mesh_export(text: str) -> dict[str, Any]:
    """Validate an export without including credentials in error messages."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        for field in ("netKeys", "appKeys", "nodes"):
            if not isinstance(data.get(field), list) or not data[field]:
                raise ValueError("Missing Mesh data")
        for field in ("netKeys", "appKeys"):
            indexes = set()
            for item in data[field]:
                index = _index(item["index"])
                if index in indexes:
                    raise ValueError("Duplicate key index")
                indexes.add(index)
                _key(item["key"])
                if field == "appKeys":
                    _index(item["boundNetKey"])
        occupied: set[int] = set()
        for node in data["nodes"]:
            primary = mesh_address(node["unicastAddress"])
            elements = node["elements"]
            if not isinstance(elements, list) or not elements:
                raise ValueError("Missing elements")
            indexes = [element["index"] for element in elements]
            if any(type(index) is not int for index in indexes) or sorted(
                indexes
            ) != list(range(len(elements))):
                raise ValueError("Invalid element indexes")
            addresses = set(range(primary, primary + len(elements)))
            if primary == 0 or max(addresses) > 0x7FFF or addresses & occupied:
                raise ValueError("Overlapping or invalid unicast addresses")
            occupied.update(addresses)
            _key(node["deviceKey"])
            for field in ("netKeys", "appKeys"):
                if not isinstance(node.get(field), list):
                    raise ValueError("Missing node key indexes")
                for item in node[field]:
                    _index(item["index"])
            for element in elements:
                if not isinstance(element["models"], list):
                    raise ValueError("Missing models")
                for model in element["models"]:
                    if not re.fullmatch(
                        r"(?:[0-9a-fA-F]{4}|[0-9a-fA-F]{8})", model["modelId"]
                    ):
                        raise ValueError("Invalid model ID")
                    if not isinstance(model.get("bind", []), list):
                        raise ValueError("Invalid bindings")
                    for index in model.get("bind", []):
                        _index(index)
        return data
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise ValueError("Invalid STEINEL Mesh JSON export") from err


def node_app_keys(export: dict[str, Any], node: dict[str, Any]) -> list[int]:
    """Return existing application keys actually bound on this node."""
    if node.get("excluded", False):
        return []
    node_keys = {_index(item["index"]) for item in node.get("appKeys", [])}
    net_indexes = {_index(item["index"]) for item in node.get("netKeys", [])}
    bound = {
        index
        for element in node["elements"]
        for model in element["models"]
        for index in model.get("bind", [])
    }
    available_networks = {item["index"] for item in export["netKeys"]}
    return sorted(
        item["index"]
        for item in export["appKeys"]
        if item["index"] in node_keys & bound
        and item["boundNetKey"] in net_indexes & available_networks
    )


def import_mesh_node(
    export: dict[str, Any],
    primary: str,
    app_index: int,
    source: int,
    iv_index: int,
    sequence: int,
) -> dict[str, Any]:
    """Build an entry using only the chosen node's existing model bindings."""
    if type(source) is not int or not 1 <= source <= 0x7FFF:
        raise ValueError("Invalid source address")
    if type(iv_index) is not int or not 0 <= iv_index <= 0xFFFFFFFF:
        raise ValueError("Invalid IV Index")
    if type(sequence) is not int or not 0 <= sequence <= 0xFFFFFE:
        raise ValueError("Invalid sequence number")
    for item in export["nodes"]:
        start = mesh_address(item["unicastAddress"])
        if start <= source < start + len(item["elements"]):
            raise ValueError("Source address is already occupied")
    node = next(
        (item for item in export["nodes"] if item["unicastAddress"] == primary), None
    )
    if node is None:
        raise ValueError("Node is not in the export")
    if app_index not in node_app_keys(export, node):
        raise ValueError("Application key is not bound to this node")
    app = next(item for item in export["appKeys"] if item["index"] == app_index)
    net = next(
        item for item in export["netKeys"] if item["index"] == app["boundNetKey"]
    )
    if net.get("phase", 0) != 0:
        raise ValueError("Complete Mesh Key Refresh before importing")
    elements = []
    for element in sorted(node["elements"], key=lambda item: item["index"]):
        sig: list[int] = []
        vendor: list[list[int]] = []
        for model in element["models"]:
            if app_index not in model.get("bind", []):
                continue
            model_id = model["modelId"]
            if len(model_id) == 4:
                sig.append(int(model_id, 16))
            else:
                vendor.append([int(model_id[:4], 16), int(model_id[4:], 16)])
        elements.append(
            {
                "address": mesh_address(primary) + element["index"],
                "sig_models": sorted(sig),
                "vendor_models": sorted(vendor),
            }
        )
    return {
        CONF_EXISTING_MESH: True,
        CONF_NET_KEY: _key(net["key"]),
        CONF_APP_KEY: _key(app["key"]),
        CONF_DEVICE_KEY: _key(node["deviceKey"]),
        CONF_UNICAST_ADDRESS: mesh_address(primary),
        CONF_SOURCE_ADDRESS: source,
        CONF_IV_INDEX: iv_index,
        CONF_SEQUENCE: sequence,
        CONF_ELEMENT_COUNT: len(elements),
        CONF_ELEMENTS: elements,
        CONF_IMPORTED_ELEMENTS: elements,
        CONF_MODEL_SCHEMA_VERSION: MODEL_SCHEMA_VERSION,
        CONF_NODE_STATE: NODE_COMPLETE,
    }
