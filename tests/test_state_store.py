"""Tests for durable light state encoding."""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).parents[1] / "custom_components/steinel_ble/state_store.py"
_SPEC = importlib.util.spec_from_file_location("steinel_test_state_store", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
restore_light_states = _MODULE.restore_light_states
serialize_light_states = _MODULE.serialize_light_states


def test_state_round_trip() -> None:
    encoded = serialize_light_states(
        {256: True}, {256: 26}, {256: 3000}, {256: (120.0, 40.0)}
    )
    assert restore_light_states(encoded) == (
        {256: True},
        {256: 26},
        {256: 3000},
        {256: (120.0, 40.0)},
    )


def test_malformed_state_is_ignored() -> None:
    assert restore_light_states(
        {"bad": {"on": True}, "256": {"brightness": 999, "hs": [1]}}
    ) == ({}, {}, {}, {})


def test_partial_state_is_supported() -> None:
    assert restore_light_states({"257": {"on": False}}) == (
        {257: False},
        {},
        {},
        {},
    )
