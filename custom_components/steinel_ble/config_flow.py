"""Configuration flow for Steinel Connect Bluetooth Mesh."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .const import (
    CONF_PRODUCT_ID,
    CONF_STATIC_OOB,
    DOMAIN,
    MESH_PROXY_SERVICE,
    STEINEL_COMPANY_ID,
)

_MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)


class SteinelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Steinel Mesh device from Bluetooth discovery or an address."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name = "Steinel Connect"
        self._product_id: int | None = None
        self._requires_reset = False
        self._static_oob = ""

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle discovery from Provisioning or Proxy advertising."""
        if not discovery_info.connectable:
            return self.async_abort(reason="not_connectable")
        self._address = discovery_info.address.upper()
        self._name = discovery_info.name or discovery_info.address
        self._requires_reset = MESH_PROXY_SERVICE in {
            uuid.lower() for uuid in discovery_info.service_uuids
        }
        manufacturer = discovery_info.manufacturer_data.get(STEINEL_COMPANY_ID, b"")
        if len(manufacturer) >= 2:
            self._product_id = int.from_bytes(manufacturer[:2], "little")
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {
            "name": self._name,
            "address": self._address,
        }
        if self._requires_reset:
            return await self.async_step_reset_help()
        return await self.async_step_bluetooth_confirm()

    async def async_step_reset_help(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prepare the user for the short proprietary reset window."""
        assert self._address is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            static_oob = user_input.get(CONF_STATIC_OOB, "").strip()
            if not self._valid_oob(static_oob):
                errors[CONF_STATIC_OOB] = "invalid_oob"
            else:
                return self._create_entry(self._address, static_oob)
        return self.async_show_form(
            step_id="reset_help",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_STATIC_OOB, default=self._static_oob
                    ): TextSelector(TextSelectorConfig(type="password"))
                }
            ),
            description_placeholders={"name": self._name, "address": self._address},
            errors=errors,
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device and collect optional Static OOB."""
        assert self._address is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            static_oob = user_input.get(CONF_STATIC_OOB, "").strip()
            if not self._valid_oob(static_oob):
                errors[CONF_STATIC_OOB] = "invalid_oob"
            else:
                return self._create_entry(self._address, static_oob)
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_STATIC_OOB, default=""): TextSelector(
                        TextSelectorConfig(type="password")
                    )
                }
            ),
            description_placeholders={"name": self._name, "address": self._address},
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup by Bluetooth address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            static_oob = user_input.get(CONF_STATIC_OOB, "").strip()
            if not _MAC_RE.fullmatch(address):
                errors[CONF_ADDRESS] = "invalid_address"
            elif not self._valid_oob(static_oob):
                errors[CONF_STATIC_OOB] = "invalid_oob"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                info = bluetooth.async_last_service_info(
                    self.hass, address, connectable=True
                )
                self._name = info.name if info else f"Steinel {address}"
                self._address = address
                if info and MESH_PROXY_SERVICE in {
                    uuid.lower() for uuid in info.service_uuids
                }:
                    self._requires_reset = True
                    self._static_oob = static_oob
                    return await self.async_step_reset_help()
                return self._create_entry(address, static_oob)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_STATIC_OOB, default=""): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                }
            ),
            errors=errors,
        )

    def _create_entry(self, address: str, static_oob: str) -> ConfigFlowResult:
        data: dict[str, Any] = {CONF_ADDRESS: address}
        if static_oob:
            data[CONF_STATIC_OOB] = static_oob.lower()
        if self._product_id is not None:
            data[CONF_PRODUCT_ID] = self._product_id
        return self.async_create_entry(title=self._name, data=data)

    @staticmethod
    def _valid_oob(value: str) -> bool:
        return not value or bool(re.fullmatch(r"[0-9a-fA-F]{32}", value))
