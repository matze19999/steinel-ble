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
            "last_connected": coordinator.last_connected,
            "last_error": coordinator.last_error,
            "reconnect_count": coordinator.reconnect_count,
            "elements": coordinator._serialize_elements(coordinator.elements),
        },
        "bluetooth": {
            "source": getattr(service_info, "source", None),
            "rssi": getattr(service_info, "rssi", None),
            "service_uuids": list(getattr(service_info, "service_uuids", [])),
            "connectable": getattr(service_info, "connectable", None),
        },
    }
