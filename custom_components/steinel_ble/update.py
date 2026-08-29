"""STEINEL firmware update entity.

STEINEL's online firmware catalog (``https://connectapp.steinel.de/api/changes``)
links firmware records to an internal product UUID, and there is currently no
known, verified way to resolve the numeric product id broadcast by a lamp to
that UUID without the (unpublished) companion "products" endpoint the
official app also relies on. Rather than guess at that mapping - which would
risk silently offering the wrong signed firmware image for a bricking-risk
DFU flash - each lamp is configured (via the integration options) with an
explicit ``.sfu``/``.zip`` package URL or local path, expected version,
hardware revision and product id, mirroring exactly what
``steinel_ble.py firmware-update`` already validates and applies through the
fully-tested Nordic Secure DFU path. See STEINEL_BLE_TOOL.md/section 9 of
STEINEL_BLE_KOMMUNIKATION.md for the reasoning and the verified example URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FIRMWARE_HARDWARE,
    CONF_FIRMWARE_PRODUCT_ID,
    CONF_FIRMWARE_SHA256,
    CONF_FIRMWARE_URL,
    CONF_FIRMWARE_VERSION,
    DOMAIN,
    MANUFACTURER,
    STEINEL_COMPANY_ID,
)
from .coordinator import SteinelMeshHub
from .protocol import (
    ProtocolError,
    advertised_firmware_matches_catalog,
    load_nordic_firmware_package,
    parse_steinel_advertisement,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    hub: SteinelMeshHub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SteinelFirmwareUpdate(hass, hub, int(unicast_key, 16), node) for unicast_key, node in hub.network.nodes.items()
    )


class SteinelFirmwareUpdate(UpdateEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "firmware"
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    _attr_title = "STEINEL Connect firmware"

    def __init__(self, hass: HomeAssistant, hub: SteinelMeshHub, unicast: int, node: dict[str, Any]) -> None:
        self.hass = hass
        self._hub = hub
        self._unicast = unicast
        self._address = node["address"]
        self._attr_unique_id = f"{DOMAIN}_{self._address}_firmware"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(CONNECTION_BLUETOOTH, self._address)},
            name=node.get("name") or f"STEINEL {self._address}",
            manufacturer=MANUFACTURER,
            model=f"Mesh node 0x{unicast:04X}",
        )
        self._attr_in_progress = False

    @property
    def _node(self) -> dict[str, Any]:
        return self._hub.network.node_for_unicast(self._unicast) or {}

    @property
    def installed_version(self) -> str | None:
        info = bluetooth.async_last_service_info(self.hass, self._address, connectable=False)
        if info is None:
            return None
        manufacturer = info.manufacturer_data.get(STEINEL_COMPANY_ID)
        if manufacturer is None:
            return None
        try:
            return parse_steinel_advertisement(manufacturer, info.name).get("firmware")
        except ProtocolError:
            return None

    @property
    def latest_version(self) -> str | None:
        configured = self._node.get(CONF_FIRMWARE_VERSION)
        installed = self.installed_version
        if not configured:
            return installed
        # The wire encoding overlaps the advertised patch byte with the
        # product id's high byte, so compare with the same major.minor rule
        # the CLI tool uses instead of a strict string comparison.
        if installed and advertised_firmware_matches_catalog(installed, str(configured)):
            return installed
        return str(configured)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        node = self._node
        url = node.get(CONF_FIRMWARE_URL)
        expected_version = node.get(CONF_FIRMWARE_VERSION)
        expected_hardware = node.get(CONF_FIRMWARE_HARDWARE)
        expected_product_id = node.get(CONF_FIRMWARE_PRODUCT_ID)
        expected_sha256 = node.get(CONF_FIRMWARE_SHA256)
        if not url or expected_version is None or expected_hardware is None or expected_product_id is None:
            raise HomeAssistantError(
                "no firmware package is configured for this lamp yet; add the .sfu URL/path, expected "
                "version, hardware revision and product id via the integration's options first"
            )

        raw = await self._async_fetch(str(url))
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 and digest.lower() != str(expected_sha256).lower():
            raise HomeAssistantError(
                f"downloaded firmware SHA-256 {digest} does not match the configured {expected_sha256}; refusing to flash"
            )
        try:
            package = load_nordic_firmware_package(raw)
        except ProtocolError as exc:
            raise HomeAssistantError(f"invalid firmware package: {exc}") from exc

        self._attr_in_progress = True
        self.async_write_ha_state()

        @callback
        def _on_progress(percent: int, offset: int, total: int) -> None:
            self._attr_in_progress = percent
            self.async_write_ha_state()

        try:
            await self._hub.async_firmware_update(
                self._address,
                package,
                str(expected_version),
                int(expected_hardware),
                int(expected_product_id),
                progress_callback=_on_progress,
            )
        except ProtocolError as exc:
            raise HomeAssistantError(f"firmware update failed: {exc}") from exc
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()

    async def _async_fetch(self, url: str) -> bytes:
        if url.startswith(("http://", "https://")):
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(180):
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.read()
        path = Path(url)
        if not path.is_file():
            raise HomeAssistantError(f"firmware file not found: {path}")
        return await self.hass.async_add_executor_job(path.read_bytes)
