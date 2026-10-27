"""Diagnostics support for Steinel Connect Bluetooth Mesh."""

from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import SteinelConfigEntry
from .const import CONF_APP_KEY, CONF_DEVICE_KEY, CONF_NET_KEY, CONF_STATIC_OOB

_REDACT = {CONF_APP_KEY, CONF_DEVICE_KEY, CONF_NET_KEY, CONF_STATIC_OOB}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SteinelConfigEntry
) -> dict[str, Any]:
    """Return credential-free diagnostics for a config entry."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {
            "entry": async_redact_data(dict(entry.data), _REDACT),
            "options": dict(entry.options),
            "runtime": {"available": False, "setup_in_progress": True},
        }
    service_info = bluetooth.async_last_service_info(
        hass, coordinator.address, connectable=True
    )
    return {
        "entry": async_redact_data(dict(entry.data), _REDACT),
        "options": dict(entry.options),
        "runtime": {
            "available": coordinator.available,
            "reachable": coordinator.reachable,
            "advertisement_available": coordinator.advertisement_available,
            "last_advertisement": coordinator.last_advertisement,
            "gatt_connected": bool(
                coordinator.transport
                and coordinator.transport.client
                and coordinator.transport.client.is_connected
            ),
            "last_connected": coordinator.last_connected,
            "last_error": coordinator.last_error,
            "reconnect_count": coordinator.reconnect_count,
            "mesh_receive_counts": (
                dict(coordinator.node.rx_stats) if coordinator.node else {}
            ),
            "mesh_iv_state": (
                {
                    "iv_index": coordinator.node.iv_index,
                    "transmit_iv_index": coordinator.node.transmit_iv_index,
                    "iv_update": coordinator.node.iv_update,
                    "iv_state_verified": coordinator.node.iv_state_verified,
                    "authenticated_beacon": (
                        {
                            "iv_index": coordinator.node.beacon.iv_index,
                            "iv_update": coordinator.node.beacon.iv_update,
                            "key_refresh": coordinator.node.beacon.key_refresh,
                        }
                        if coordinator.node.beacon
                        else None
                    ),
                }
                if coordinator.node
                else None
            ),
            "sensor_values": {
                f"{address:04x}_{name}": value.value
                for (address, name), value in coordinator.sensor_values.items()
            },
            "sensor_raw_values": {
                f"{address:04x}_{name}": value.raw.hex()
                for (address, name), value in coordinator.sensor_values.items()
            },
            "lc_properties": {
                f"{address:04x}_{property_id:04x}": value.hex()
                for (address, property_id), value in coordinator.lc_properties.items()
            },
            "lc_modes": {
                f"{address:04x}_{'occupancy' if occupancy else 'automatic'}": value
                for (address, occupancy), value in coordinator.lc_modes.items()
            },
            "elements": coordinator._serialize_elements(coordinator.elements),
        },
        "bluetooth": {
            "source": getattr(service_info, "source", None),
            "rssi": getattr(service_info, "rssi", None),
            "service_uuids": list(getattr(service_info, "service_uuids", [])),
            "connectable": getattr(service_info, "connectable", None),
        },
    }
