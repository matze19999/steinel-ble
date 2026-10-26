"""Configuration flow for Steinel Connect Bluetooth Mesh."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_ADVERTISEMENT_TIMEOUT,
    CONF_BRIGHTNESS_DELAY,
    CONF_COMMAND_TIMEOUT,
    CONF_CONNECT_ATTEMPTS,
    CONF_DISCONNECT_WHEN_IDLE,
    CONF_ELEMENTS,
    CONF_EXISTING_MESH,
    CONF_IDLE_DISCONNECT_DELAY,
    CONF_MODEL_SCHEMA_VERSION,
    CONF_NET_KEY,
    CONF_PRODUCT_ID,
    CONF_PROVISION_ATTEMPTS,
    CONF_RESTORE_BRIGHTNESS,
    CONF_SENSOR_INTERVAL,
    CONF_SENSOR_PROPERTIES,
    CONF_SOURCE_ADDRESS,
    CONF_STATIC_OOB,
    DEFAULT_ADVERTISEMENT_TIMEOUT,
    DEFAULT_BRIGHTNESS_DELAY,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CONNECT_ATTEMPTS,
    DEFAULT_DISCONNECT_WHEN_IDLE,
    DEFAULT_IDLE_DISCONNECT_DELAY,
    DEFAULT_PROVISION_ATTEMPTS,
    DEFAULT_RESTORE_BRIGHTNESS,
    DEFAULT_SENSOR_INTERVAL,
    DOMAIN,
    MESH_PROXY_SERVICE,
    STEINEL_COMPANY_ID,
)
from .mesh_import import import_mesh_node, node_app_keys, read_mesh_export

_MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)


class SteinelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Steinel Mesh device from Bluetooth discovery or an address."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow for an existing node."""
        return SteinelOptionsFlow()

    def __init__(self) -> None:
        self._address: str | None = None
        self._name = "Steinel Connect"
        self._product_id: int | None = None
        self._requires_reset = False
        self._static_oob = ""
        self._mesh_export: dict[str, Any] | None = None
        self._import_node: str | None = None

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
            if user_input.get(CONF_EXISTING_MESH):
                return await self.async_step_mesh_import()
            static_oob = user_input.get(CONF_STATIC_OOB, "").strip()
            if not self._valid_oob(static_oob):
                errors[CONF_STATIC_OOB] = "invalid_oob"
            else:
                return self._create_entry(self._address, static_oob)
        return self.async_show_form(
            step_id="reset_help",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_EXISTING_MESH, default=False): BooleanSelector(),
                    vol.Optional(
                        CONF_STATIC_OOB, default=self._static_oob
                    ): TextSelector(TextSelectorConfig(type="password")),
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
            if user_input.get(CONF_EXISTING_MESH):
                return await self.async_step_mesh_import()
            static_oob = user_input.get(CONF_STATIC_OOB, "").strip()
            if not self._valid_oob(static_oob):
                errors[CONF_STATIC_OOB] = "invalid_oob"
            else:
                return self._create_entry(self._address, static_oob)
        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_EXISTING_MESH, default=False): BooleanSelector(),
                    vol.Optional(CONF_STATIC_OOB, default=""): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
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
                if user_input.get(CONF_EXISTING_MESH):
                    return await self.async_step_mesh_import()
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
                    vol.Optional(CONF_EXISTING_MESH, default=False): BooleanSelector(),
                    vol.Optional(CONF_STATIC_OOB, default=""): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_mesh_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept an App export without retaining the entire export in the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                export = read_mesh_export(user_input["mesh_json"])
                if not any(node_app_keys(export, node) for node in export["nodes"]):
                    raise ValueError("No bound application keys")
            except (ValueError, KeyError, TypeError):
                errors["base"] = "invalid_mesh_export"
            else:
                self._mesh_export = export
                return await self.async_step_mesh_node()
        return self.async_show_form(
            step_id="mesh_import",
            data_schema=vol.Schema(
                {
                    vol.Required("mesh_json"): TextSelector(
                        TextSelectorConfig(multiline=True)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_mesh_node(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the exported node that matches the Bluetooth device."""
        assert self._mesh_export is not None
        nodes = [
            node
            for node in self._mesh_export["nodes"]
            if node_app_keys(self._mesh_export, node)
        ]
        if user_input is not None and any(
            node["unicastAddress"] == user_input["node"] for node in nodes
        ):
            self._import_node = user_input["node"]
            return await self.async_step_mesh_credentials()
        return self.async_show_form(
            step_id="mesh_node",
            data_schema=vol.Schema(
                {
                    vol.Required("node"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {
                                    "value": node["unicastAddress"],
                                    "label": (
                                        f"{node.get('name', 'Node')} "
                                        f"(0x{node['unicastAddress']})"
                                    ),
                                }
                                for node in nodes
                            ]
                        )
                    )
                }
            ),
        )

    async def async_step_mesh_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the runtime state absent from standard Mesh exports."""
        assert self._mesh_export is not None and self._import_node is not None
        node = next(
            item
            for item in self._mesh_export["nodes"]
            if item["unicastAddress"] == self._import_node
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = import_mesh_node(
                    self._mesh_export,
                    self._import_node,
                    int(user_input["app_key_index"]),
                    int(user_input[CONF_SOURCE_ADDRESS].removeprefix("0x"), 16),
                    int(user_input["iv_index"]),
                    int(user_input["sequence"]),
                )
                for entry in self._async_current_entries():
                    if entry.data.get(CONF_NET_KEY) != data[CONF_NET_KEY]:
                        continue
                    if (
                        entry.data.get(CONF_SOURCE_ADDRESS, 1)
                        == data[CONF_SOURCE_ADDRESS]
                    ):
                        raise ValueError(
                            "Source address is already used by Home Assistant"
                        )
                    if entry.data.get("unicast_address") == data["unicast_address"]:
                        return self.async_abort(reason="already_configured")
            except (ValueError, KeyError, TypeError):
                errors["base"] = "invalid_mesh_credentials"
            else:
                assert self._address is not None
                data[CONF_ADDRESS] = self._address
                if self._product_id is not None:
                    data[CONF_PRODUCT_ID] = self._product_id
                self._mesh_export = None
                return self.async_create_entry(
                    title=node.get("name") or self._name, data=data
                )
        return self.async_show_form(
            step_id="mesh_credentials",
            data_schema=vol.Schema(
                {
                    vol.Required("app_key_index"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                str(index)
                                for index in node_app_keys(self._mesh_export, node)
                            ]
                        )
                    ),
                    vol.Required(CONF_SOURCE_ADDRESS): str,
                    vol.Required("iv_index"): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=0xFFFFFFFF)
                    ),
                    vol.Required("sequence", default=0): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=0xFFFFFE)
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


class SteinelOptionsFlow(OptionsFlow):
    """Configure connection, provisioning and polling behaviour."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            rescan = user_input.pop("rescan_models", False)
            if rescan:
                data = dict(self.config_entry.data)
                for key in (
                    CONF_ELEMENTS,
                    CONF_MODEL_SCHEMA_VERSION,
                    CONF_SENSOR_PROPERTIES,
                ):
                    data.pop(key, None)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        number = lambda minimum, maximum, step=1: NumberSelector(  # noqa: E731
            NumberSelectorConfig(
                min=minimum,
                max=maximum,
                step=step,
                mode=NumberSelectorMode.BOX,
            )
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONNECT_ATTEMPTS,
                        default=options.get(
                            CONF_CONNECT_ATTEMPTS, DEFAULT_CONNECT_ATTEMPTS
                        ),
                    ): number(1, 12),
                    vol.Required(
                        CONF_COMMAND_TIMEOUT,
                        default=options.get(
                            CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT
                        ),
                    ): number(5, 60),
                    vol.Required(
                        CONF_PROVISION_ATTEMPTS,
                        default=options.get(
                            CONF_PROVISION_ATTEMPTS, DEFAULT_PROVISION_ATTEMPTS
                        ),
                    ): number(1, 6),
                    vol.Required(
                        CONF_SENSOR_INTERVAL,
                        default=options.get(
                            CONF_SENSOR_INTERVAL, DEFAULT_SENSOR_INTERVAL
                        ),
                    ): number(30, 3600, 10),
                    vol.Required(
                        CONF_RESTORE_BRIGHTNESS,
                        default=options.get(
                            CONF_RESTORE_BRIGHTNESS, DEFAULT_RESTORE_BRIGHTNESS
                        ),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_BRIGHTNESS_DELAY,
                        default=options.get(
                            CONF_BRIGHTNESS_DELAY, DEFAULT_BRIGHTNESS_DELAY
                        ),
                    ): number(0, 2, 0.05),
                    vol.Required(
                        CONF_DISCONNECT_WHEN_IDLE,
                        default=options.get(
                            CONF_DISCONNECT_WHEN_IDLE,
                            DEFAULT_DISCONNECT_WHEN_IDLE,
                        ),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_IDLE_DISCONNECT_DELAY,
                        default=options.get(
                            CONF_IDLE_DISCONNECT_DELAY,
                            DEFAULT_IDLE_DISCONNECT_DELAY,
                        ),
                    ): number(10, 600, 5),
                    vol.Required(
                        CONF_ADVERTISEMENT_TIMEOUT,
                        default=options.get(
                            CONF_ADVERTISEMENT_TIMEOUT,
                            DEFAULT_ADVERTISEMENT_TIMEOUT,
                        ),
                    ): number(30, 900, 10),
                    vol.Required("rescan_models", default=False): BooleanSelector(),
                }
            ),
        )
