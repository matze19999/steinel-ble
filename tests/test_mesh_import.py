"""Tests for importing existing Mesh credentials without changing bindings."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


def _load_package():
    root = Path(__file__).parents[1] / "custom_components/steinel_ble"
    package_name = "steinel_test_import_package"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package
    for module_name in ("const", "mesh_import"):
        name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(name, root / f"{module_name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.mesh_import"]


mesh_import = _load_package()


@pytest.fixture
def export():
    """Synthetic keys and addresses; no real exported credentials."""
    return {
        "netKeys": [{"index": 7, "key": "11" * 16, "phase": 0}],
        "appKeys": [
            {"index": 12, "boundNetKey": 7, "key": "22" * 16},
            {"index": 13, "boundNetKey": 7, "key": "33" * 16},
        ],
        "nodes": [
            {
                "name": "Phone",
                "unicastAddress": "0001",
                "deviceKey": "44" * 16,
                "netKeys": [{"index": 7}],
                "appKeys": [{"index": 12}],
                "elements": [{"index": 0, "models": [{"modelId": "0001", "bind": []}]}],
            },
            {
                "name": "Test lamp",
                "unicastAddress": "000A",
                "deviceKey": "55" * 16,
                "netKeys": [{"index": 7}],
                "appKeys": [{"index": 12}, {"index": 13}],
                "elements": [
                    {
                        "index": 0,
                        "models": [
                            {"modelId": "1000", "bind": [12]},
                            {"modelId": "1300", "bind": [13]},
                            {"modelId": "0000", "bind": []},
                        ],
                    },
                    {
                        "index": 1,
                        "models": [
                            {"modelId": "130F", "bind": [12]},
                            {"modelId": "05631001", "bind": [12]},
                        ],
                    },
                ],
            },
        ],
        "steinel": {"profiles": [{"sceneNumber": 3}]},
    }


def test_import_uses_selected_keys_and_only_bound_models(export):
    original = copy.deepcopy(export)
    parsed = mesh_import.read_mesh_export(json.dumps(export))
    result = mesh_import.import_mesh_node(parsed, "000A", 12, 0x7FFE, 42, 50)
    assert result["net_key"] == "11" * 16
    assert result["app_key"] == "22" * 16
    assert result["device_key"] == "55" * 16
    assert result["source_address"] == 0x7FFE
    assert result["unicast_address"] == 10
    assert result["iv_index"] == 42
    assert result["sequence"] == 50
    assert result["existing_mesh"] is True
    assert result["node_state"] == "complete"
    assert result["elements"] == [
        {"address": 10, "sig_models": [0x1000], "vendor_models": []},
        {"address": 11, "sig_models": [0x130F], "vendor_models": [[0x0563, 0x1001]]},
    ]
    assert result["imported_elements"] == result["elements"]
    assert "steinel" not in result
    assert "nodes" not in result
    assert export == original


def test_key_selection_changes_exposed_models(export):
    result = mesh_import.import_mesh_node(export, "000A", 13, 0x7FFE, 0, 0)
    assert result["app_key"] == "33" * 16
    assert result["elements"][0]["sig_models"] == [0x1300]
    assert result["elements"][1]["vendor_models"] == []


def test_phone_and_excluded_nodes_are_not_selectable(export):
    assert mesh_import.node_app_keys(export, export["nodes"][0]) == []
    node = export["nodes"][1]
    assert mesh_import.node_app_keys(export, node) == [12, 13]
    node["excluded"] = True
    assert mesh_import.node_app_keys(export, node) == []


@pytest.mark.parametrize("source", [0, 1, 10, 11, 0x8000, True])
def test_source_must_not_overlap_any_exported_element(export, source):
    with pytest.raises(ValueError):
        mesh_import.import_mesh_node(export, "000A", 12, source, 0, 0)


@pytest.mark.parametrize("index", [0, 7, 4095])
def test_unbound_app_key_is_rejected(export, index):
    with pytest.raises(ValueError):
        mesh_import.import_mesh_node(export, "000A", index, 0x7FFE, 0, 0)


@pytest.mark.parametrize("iv,sequence", [(-1, 0), (2**32, 0), (0, -1), (0, 0xFFFFFF)])
def test_invalid_runtime_state_is_rejected(export, iv, sequence):
    with pytest.raises(ValueError):
        mesh_import.import_mesh_node(export, "000A", 12, 0x7FFE, iv, sequence)


def test_key_refresh_requires_completion(export):
    export["netKeys"][0]["phase"] = 2
    with pytest.raises(ValueError, match="Key Refresh"):
        mesh_import.import_mesh_node(export, "000A", 12, 0x7FFE, 0, 0)


@pytest.mark.parametrize("text", ["null", "[]", "{}", "{", '{"netKeys": "bad"}'])
def test_bad_exports_produce_credential_free_errors(text):
    with pytest.raises(ValueError, match="Invalid STEINEL Mesh JSON export"):
        mesh_import.read_mesh_export(text)


@pytest.mark.parametrize("value", ["secret-not-hex", "AB" * 15, "AB" * 17])
def test_invalid_keys_are_not_echoed(export, value):
    export["netKeys"][0]["key"] = value
    with pytest.raises(ValueError) as error:
        mesh_import.read_mesh_export(json.dumps(export))
    assert value not in str(error.value)


def test_overlapping_nodes_are_rejected(export):
    export["nodes"][0]["unicastAddress"] = "000B"
    with pytest.raises(ValueError):
        mesh_import.read_mesh_export(json.dumps(export))


def test_element_indices_determine_addresses_not_list_order(export):
    export["nodes"][1]["elements"].reverse()
    parsed = mesh_import.read_mesh_export(json.dumps(export))
    result = mesh_import.import_mesh_node(parsed, "000A", 12, 0x7FFE, 0, 0)
    assert [element["address"] for element in result["elements"]] == [10, 11]
    assert result["elements"][1]["sig_models"] == [0x130F]


def test_orphan_application_key_is_not_offered(export):
    export["appKeys"][0]["boundNetKey"] = 8
    assert mesh_import.node_app_keys(export, export["nodes"][1]) == [13]
