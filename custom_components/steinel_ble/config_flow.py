"""Config flow: discover, confirm, provision (or import) a STEINEL lamp.

There is exactly one hub config entry per Home Assistant instance (the
Bluetooth Mesh network state - keys, sequence counter, node list - is
shared, see ``mesh_store.py``); further lamps are added afterwards through
the integration's options flow ("Add device"). Both flows share the same
bluetooth-pick / confirm+identify / provision-and-bind steps through
``_MeshSetupMixin``, and provisioning writes to the real, persisted mesh
store as it happens (mirroring the CLI tool: once a lamp has physically
learned the NetKey there is no undo, so partial progress is kept even if a
later step in the wizard fails and the user retries).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import ble
from .const import (
    CAPABILITY_ONOFF,
    CONF_FIRMWARE_HARDWARE,
    CONF_FIRMWARE_PRODUCT_ID,
    CONF_FIRMWARE_SHA256,
    CONF_FIRMWARE_URL,
    CONF_FIRMWARE_VERSION,
    DEFAULT_CONNECT_TIMEOUT,
    DOMAIN,
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_SERVICE,
    MESH_RETRY_DELAY,
    STEINEL_COMPANY_ID,
)
from .coordinator import SteinelMeshHub
from .protocol import GLOBAL_RESET_DATA, GLOBAL_RESET_OPCODE

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _describe_exc(exc: Exception) -> str:
    """"" -> "TimeoutError"; "not found" -> "BleakError: not found". Several
    of the exceptions connection failures raise (plain asyncio.TimeoutError
    in particular) carry no message text, and an empty {error} placeholder
    is useless for diagnosing what actually happened."""
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _looks_like_steinel(info: BluetoothServiceInfoBleak) -> bool:
    if STEINEL_COMPANY_ID in info.manufacturer_data:
        return True
    services = {uuid.lower() for uuid in info.service_uuids}
    return MESH_PROVISIONING_SERVICE in services or MESH_PROXY_SERVICE in services


class _MeshSetupMixin:
    """Shared steps for the initial setup flow and the "add device" options flow."""

    hass: Any

    def _mixin_init(self) -> None:
        # NOTE: self.hass is not set yet at __init__ time (the flow manager
        # assigns it right after construction), so the hub is created lazily
        # in _async_hub() instead of here.
        self._hub: SteinelMeshHub | None = None
        self._address: str | None = None
        self._name: str | None = None
        self._discovery: BluetoothServiceInfoBleak | None = None
        # Set when the user explicitly chose "reset and add" - allows
        # async_step_confirm to factory-reset a lamp that belongs to an
        # unknown mesh instead of aborting, see async_step_reset_and_add.
        self._reset_before_provision: bool = False

    async def _async_hub(self) -> SteinelMeshHub:
        if self._hub is None:
            self._hub = SteinelMeshHub(self.hass)
        if not self._hub.network.loaded:
            await self._hub.async_setup()
        return self._hub

    def _known_addresses(self, hub: SteinelMeshHub) -> set[str]:
        # Nodes that were provisioned but never finished binding a light
        # model (e.g. the connection dropped mid-flow) stay pickable so the
        # user can retry, instead of becoming permanently invisible.
        return {
            str(node.get("address", "")).upper()
            for node in hub.network.nodes.values()
            if (node.get("capabilities") or {}).get(CAPABILITY_ONOFF)
        }

    async def async_step_reset_and_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Entry point for the "reset lamp and add to mesh" menu option.

        Reuses the normal device picker; the only difference is the flag set
        here, which lets async_step_confirm factory-reset a lamp that turns
        out to belong to an unknown mesh instead of aborting.
        """
        self._reset_before_provision = True
        return await self.async_step_pick_device(user_input)

    async def async_step_pick_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = await self._async_hub()
        known = self._known_addresses(hub)
        candidates = {
            info.address: f"{info.name or info.address} ({info.address})"
            for info in async_discovered_service_info(self.hass, connectable=True)
            if _looks_like_steinel(info) and info.address.upper() not in known
        }

        if user_input is not None:
            address = (user_input.get("manual_address") or user_input.get("address") or "").strip().upper()
            if not address or not _MAC_RE.match(address):
                return self.async_show_form(
                    step_id="pick_device",
                    data_schema=self._pick_device_schema(candidates),
                    errors={"base": "invalid_address"},
                )
            self._address = address
            info = next(
                (c for c in async_discovered_service_info(self.hass, connectable=True) if c.address.upper() == address),
                None,
            )
            self._discovery = info
            self._name = (info.name if info else None) or address
            return await self.async_step_confirm()

        if not candidates:
            return self.async_show_form(
                step_id="pick_device", data_schema=self._pick_device_schema(candidates), errors={"base": "no_devices_found"}
            )
        return self.async_show_form(step_id="pick_device", data_schema=self._pick_device_schema(candidates))

    @staticmethod
    def _pick_device_schema(candidates: dict[str, str]) -> vol.Schema:
        fields: dict[Any, Any] = {}
        if candidates:
            fields[vol.Optional("address")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=k, label=v) for k, v in candidates.items()]
                )
            )
        fields[vol.Optional("manual_address")] = str
        return vol.Schema(fields)

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._address is not None
        if user_input is None:
            hub = await self._async_hub()
            # Best-effort blink while the confirmation form is shown - a
            # weak/unreliable BLE link must not surface as an error here, and
            # the exception has to be swallowed *inside* the background task
            # (not around the synchronous async_create_task() call) or
            # asyncio logs it as "Task exception was never retrieved".
            self.hass.async_create_task(self._async_best_effort_identify(hub, self._address))
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({vol.Optional("name", default=self._name or self._address): str}),
                description_placeholders={"name": self._name or "", "address": self._address},
            )
        self._name = user_input.get("name") or self._name or self._address
        role = await self._async_detect_role()
        if role == "proxy":
            hub = await self._async_hub()
            existing = hub.network.node_for_address(self._address)
            if existing is not None:
                unicast, _node = existing
                # Already provisioned by us in an earlier, interrupted
                # attempt - it will no longer advertise the provisioning
                # service, so just retry binding the light models instead
                # of trying (and failing) to provision it again.
                return await self.async_step_provision(retry_unicast=unicast)
            if self._reset_before_provision:
                return await self.async_step_reset_confirm()
            return self.async_abort(
                reason="already_provisioned_elsewhere",
                description_placeholders={"name": self._name, "address": self._address},
            )
        if role == "unknown":
            return self.async_abort(reason="cannot_connect", description_placeholders={"address": self._address})
        return await self.async_step_provision()

    async def async_step_reset_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm and perform a factory reset of a lamp on an unknown mesh.

        Global Reset (proprietary GATT opcode 0xE5) works independently of
        Bluetooth Mesh provisioning state - no NetKey/AppKey of the foreign
        mesh is needed - so a lamp that belongs to a mesh we don't manage
        can still be reset and then provisioned into ours.
        """
        assert self._address is not None
        if user_input is None:
            return self.async_show_form(
                step_id="reset_confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"name": self._name or "", "address": self._address},
            )
        # Real BLE connects over a proxy in this environment have been seen
        # taking anywhere from instant up to 30s+ (weak RSSI around a lamp
        # that's also seen outright connect failures - status=133/"Invalid
        # Param" conn-parameter-update errors at the ESPHome proxy), and a
        # short timeout here doesn't make the reset any safer, it just makes
        # it more likely to raise a raw connection-library exception
        # (BleakError/TimeoutError, not this integration's own ProtocolError)
        # that would otherwise escape as an unhandled "Unknown error
        # occurred" instead of the proper translated abort reason below. So:
        # a generous per-attempt timeout, several retries, and catch broadly
        # - not just ProtocolError.
        reset_timeout = max(90.0, DEFAULT_CONNECT_TIMEOUT)
        attempts = 5
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with ble.DirectClient(self.hass, self._address, reset_timeout) as client:
                    await client.command(GLOBAL_RESET_OPCODE, GLOBAL_RESET_DATA, reset_timeout)
                    # The reset just changed which GATT service/characteristics
                    # this address exposes (Proxy -> Provisioning); purge any
                    # cached table for it now, while still connected, so the
                    # reconnect for provisioning does a fresh discovery
                    # instead of reusing the now-stale one.
                    await client.clear_cache()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 - any connect/GATT failure is retried the same way
                last_exc = exc
                if attempt < attempts:
                    _LOGGER.debug(
                        "Factory reset attempt %d/%d for %s failed: %s", attempt, attempts, self._address, exc
                    )
                    await asyncio.sleep(MESH_RETRY_DELAY)
        if last_exc is not None:
            _LOGGER.warning("Factory reset of %s failed: %s", self._address, _describe_exc(last_exc))
            return self.async_abort(
                reason="reset_failed", description_placeholders={"error": _describe_exc(last_exc)}
            )
        try:
            await ble.async_wait_for_service(self.hass, {self._address}, MESH_PROVISIONING_SERVICE, 90.0)
        except Exception as exc:  # noqa: BLE001 - see above
            _LOGGER.warning("%s did not re-advertise as unprovisioned after reset: %s", self._address, _describe_exc(exc))
            return self.async_abort(
                reason="reset_not_confirmed", description_placeholders={"error": _describe_exc(exc)}
            )
        return await self.async_step_provision()

    @staticmethod
    async def _async_best_effort_identify(hub: SteinelMeshHub, address: str) -> None:
        with contextlib.suppress(Exception):
            await hub.async_identify(address, True, duration=10)

    async def _async_detect_role(self) -> str:
        services: set[str] | None = None
        if self._discovery is not None:
            services = {uuid.lower() for uuid in self._discovery.service_uuids}
        if not services or (MESH_PROVISIONING_SERVICE not in services and MESH_PROXY_SERVICE not in services):
            try:
                client = await ble.async_connect(self.hass, self._address, "steinel-role-probe", 10.0)
                try:
                    services = {str(s.uuid).lower() for s in client.services}
                finally:
                    with contextlib.suppress(Exception):
                        await client.disconnect()
            except Exception:  # noqa: BLE001
                return "unknown"
        if MESH_PROVISIONING_SERVICE in services:
            return "unprovisioned"
        if MESH_PROXY_SERVICE in services:
            return "proxy"
        return "unknown"

    async def async_step_provision(
        self, user_input: dict[str, Any] | None = None, retry_unicast: int | None = None
    ) -> ConfigFlowResult:
        hub = await self._async_hub()
        try:
            if retry_unicast is not None:
                unicast = retry_unicast
            else:
                # The default timeout budgets a single, fast local BLE connect;
                # over a Bluetooth proxy in a poor-RF environment the initial
                # connect alone can take 20s+ (see the reset step above), so
                # give the whole provisioning handshake the same headroom.
                unicast, _capabilities_meta = await hub.async_provision(
                    self._address, attention=0, timeout=max(25.0, DEFAULT_CONNECT_TIMEOUT)
                )
                node = hub.network.node_for_unicast(unicast)
                node["name"] = self._name
                await hub.network.async_upsert_node(unicast, node)
            capabilities = await hub.async_bind_capabilities(unicast, timeout=max(25.0, DEFAULT_CONNECT_TIMEOUT))
        except Exception as exc:  # noqa: BLE001 - a connect failure raises bleak's own exception types, not
            # just this integration's ProtocolError; either way it must end in a clean abort, not an
            # unhandled "Unknown error occurred" that leaves the user without a way to retry sensibly.
            _LOGGER.warning("Provisioning %s failed: %s", self._address, _describe_exc(exc))
            return self.async_abort(
                reason="provisioning_failed", description_placeholders={"error": _describe_exc(exc)}
            )
        if not capabilities.get(CAPABILITY_ONOFF):
            return self.async_abort(reason="no_light_model", description_placeholders={"name": self._name})
        return await self._async_finish(f"Added {len(hub.network.nodes)} STEINEL device(s)")

    # -- import an existing mesh (e.g. state/mesh.json from steinel_ble.py) --

    async def async_step_import_mesh(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="import_mesh",
                data_schema=vol.Schema({vol.Required("mesh_json"): selector.TextSelector(selector.TextSelectorConfig(multiline=True))}),
            )
        try:
            payload = json.loads(user_input["mesh_json"])
            net_key = bytes.fromhex(str(payload["net_key"]))
            app_key = bytes.fromhex(str(payload["app_key"]))
            if len(net_key) != 16 or len(app_key) != 16:
                raise ValueError("net_key/app_key must each be 16 bytes")
            source = int(str(payload["source"]), 0)
            iv_index = int(payload.get("iv_index", 0))
            sequence = int(payload.get("sequence", 0))
            ttl = int(payload.get("ttl", 5))
            net_key_index = int(payload.get("net_key_index", 0))
            app_key_index = int(payload.get("app_key_index", 0))
            imported_nodes = dict(payload.get("nodes", {}))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.async_show_form(
                step_id="import_mesh",
                data_schema=vol.Schema({vol.Required("mesh_json"): selector.TextSelector(selector.TextSelectorConfig(multiline=True))}),
                errors={"base": "invalid_mesh_json"},
                description_placeholders={"error": str(exc)},
            )

        hub = await self._async_hub()
        network = hub.network
        # Only seed key material on a genuinely fresh network; never
        # silently overwrite an existing one from a second import.
        if not network.nodes and network.sequence == 0:
            network.net_key, network.app_key = net_key, app_key
            network.iv_index, network.sequence, network.source = iv_index, sequence, source
            network.ttl, network.net_key_index, network.app_key_index = ttl, net_key_index, app_key_index
            await network.async_save()
        elif network.net_key != net_key or network.app_key != app_key:
            return self.async_abort(reason="mesh_already_configured")

        imported = 0
        for key, raw_node in imported_nodes.items():
            unicast = int(str(raw_node.get("unicast", key)), 0)
            if network.node_for_unicast(unicast) is not None:
                continue
            node = {
                "address": raw_node["address"],
                "unicast": f"0x{unicast:04X}",
                "elements": int(raw_node.get("elements", 1)),
                "device_key": str(raw_node["device_key"]).upper(),
                "configured": False,
                "capabilities": {},
                "name": raw_node.get("name") or raw_node["address"],
            }
            await network.async_upsert_node(unicast, node)
            try:
                await hub.async_bind_capabilities(unicast)
            except Exception as exc:  # noqa: BLE001 - a connect failure must not abort importing the rest
                _LOGGER.warning(
                    "Could not (re-)confirm model bindings for imported node %s (%s): %s", key, raw_node["address"], exc
                )
            imported += 1

        if imported == 0 and not network.nodes:
            return self.async_abort(reason="no_nodes_imported")
        return await self._async_finish(f"Imported {imported} STEINEL device(s)")

    async def _async_finish(self, note: str) -> ConfigFlowResult:
        raise NotImplementedError


class SteinelConfigFlow(_MeshSetupMixin, ConfigFlow, domain=DOMAIN):
    """Initial setup: creates the one-and-only STEINEL mesh hub entry."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._mixin_init()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SteinelOptionsFlow:
        return SteinelOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_show_menu(step_id="user", menu_options=["pick_device", "reset_and_add", "import_mesh"])

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self._address = discovery_info.address
        self._name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def _async_finish(self, note: str) -> ConfigFlowResult:
        _LOGGER.debug(note)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="STEINEL Mesh", data={})


class SteinelOptionsFlow(_MeshSetupMixin, OptionsFlow):
    """Add further devices to the existing hub, or configure a firmware source."""

    def __init__(self) -> None:
        # NOTE: self.config_entry is a read-only property populated by the
        # flow manager itself on recent Home Assistant versions - assigning
        # it here raises AttributeError ("no setter").
        super().__init__()
        self._mixin_init()
        self._firmware_unicast: int | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["pick_device", "reset_and_add", "import_mesh", "configure_firmware"]
        )

    async def _async_finish(self, note: str) -> ConfigFlowResult:
        _LOGGER.debug(note)
        return self.async_create_entry(title="", data={})

    async def async_step_configure_firmware(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = await self._async_hub()
        nodes = hub.network.nodes
        if user_input is not None and "unicast" in user_input and self._firmware_unicast is None:
            self._firmware_unicast = int(user_input["unicast"], 16)
            user_input = None

        if self._firmware_unicast is None:
            if not nodes:
                return self.async_abort(reason="no_nodes_imported")
            options = [
                selector.SelectOptionDict(value=key, label=f"{node.get('name') or node['address']} ({node['address']})")
                for key, node in nodes.items()
            ]
            return self.async_show_form(
                step_id="configure_firmware", data_schema=vol.Schema({vol.Required("unicast"): selector.SelectSelector(selector.SelectSelectorConfig(options=options))})
            )

        node = hub.network.node_for_unicast(self._firmware_unicast)
        if user_input is not None:
            node[CONF_FIRMWARE_URL] = user_input.get(CONF_FIRMWARE_URL) or None
            node[CONF_FIRMWARE_VERSION] = user_input.get(CONF_FIRMWARE_VERSION) or None
            node[CONF_FIRMWARE_HARDWARE] = user_input.get(CONF_FIRMWARE_HARDWARE)
            node[CONF_FIRMWARE_PRODUCT_ID] = user_input.get(CONF_FIRMWARE_PRODUCT_ID)
            node[CONF_FIRMWARE_SHA256] = user_input.get(CONF_FIRMWARE_SHA256) or None
            await hub.network.async_upsert_node(self._firmware_unicast, node)
            return await self._async_finish("firmware source configured")

        return self.async_show_form(
            step_id="configure_firmware",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_FIRMWARE_URL, default=node.get(CONF_FIRMWARE_URL) or ""): str,
                    vol.Optional(CONF_FIRMWARE_VERSION, default=node.get(CONF_FIRMWARE_VERSION) or ""): str,
                    vol.Optional(CONF_FIRMWARE_HARDWARE, default=node.get(CONF_FIRMWARE_HARDWARE) or 0): int,
                    vol.Optional(CONF_FIRMWARE_PRODUCT_ID, default=node.get(CONF_FIRMWARE_PRODUCT_ID) or 0): int,
                    vol.Optional(CONF_FIRMWARE_SHA256, default=node.get(CONF_FIRMWARE_SHA256) or ""): str,
                }
            ),
            description_placeholders={"name": node.get("name") or node["address"]},
        )
