"""Lifecycle and state management for a Steinel Mesh node."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo

from .advertisement import parse_steinel_advertisement
from .const import (
    CONF_ADDRESS,
    CONF_ADVERTISEMENT_TIMEOUT,
    CONF_APP_KEY,
    CONF_BOOTLOADER,
    CONF_BRIGHTNESS_DELAY,
    CONF_COMMAND_TIMEOUT,
    CONF_CONNECT_ATTEMPTS,
    CONF_DEVICE_KEY,
    CONF_DISCONNECT_WHEN_IDLE,
    CONF_ELEMENT_COUNT,
    CONF_ELEMENTS,
    CONF_EXISTING_MESH,
    CONF_FIRMWARE,
    CONF_HARDWARE,
    CONF_HASH_ID,
    CONF_IDLE_DISCONNECT_DELAY,
    CONF_IMPORTED_ELEMENTS,
    CONF_IV_INDEX,
    CONF_IV_UPDATE,
    CONF_LIGHT_STATES,
    CONF_MODEL_SCHEMA_VERSION,
    CONF_NET_KEY,
    CONF_NODE_STATE,
    CONF_PRODUCT_ID,
    CONF_PROVISION_ATTEMPTS,
    CONF_RESTORE_BRIGHTNESS,
    CONF_SENSOR_INTERVAL,
    CONF_SENSOR_PROPERTIES,
    CONF_SENSOR_SCHEMA_VERSION,
    CONF_SEQUENCE,
    CONF_SOURCE_ADDRESS,
    CONF_STATIC_OOB,
    CONF_UNICAST_ADDRESS,
    DEFAULT_ADVERTISEMENT_TIMEOUT,
    DEFAULT_BRIGHTNESS_DELAY,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CONNECT_ATTEMPTS,
    DEFAULT_DISCONNECT_WHEN_IDLE,
    DEFAULT_IDLE_DISCONNECT_DELAY,
    DEFAULT_PROVISION_ATTEMPTS,
    DEFAULT_RESTORE_BRIGHTNESS,
    DEFAULT_SENSOR_INTERVAL,
    DEFAULT_UNICAST_ADDRESS,
    DOMAIN,
    MESH_PROVISIONING_DATA_IN,
    MESH_PROVISIONING_DATA_OUT,
    MESH_PROVISIONING_SERVICE,
    MESH_PROXY_DATA_IN,
    MESH_PROXY_DATA_OUT,
    MESH_PROXY_SERVICE,
    MODEL_GENERIC_ONOFF_SERVER,
    MODEL_LIGHT_CTL_SERVER,
    MODEL_LIGHT_HSL_SERVER,
    MODEL_LIGHT_LC_SERVER,
    MODEL_LIGHT_LIGHTNESS_SERVER,
    MODEL_SCHEMA_VERSION,
    MODEL_SENSOR_SERVER,
    MODEL_STEINEL_SENSOR_EXTENSION,
    NODE_COMPLETE,
    NODE_PENDING,
    OP_GENERIC_ONOFF_GET,
    OP_GENERIC_ONOFF_SET,
    OP_GENERIC_ONOFF_STATUS,
    OP_LIGHT_CTL_GET,
    OP_LIGHT_CTL_SET,
    OP_LIGHT_CTL_STATUS,
    OP_LIGHT_HSL_GET,
    OP_LIGHT_HSL_SET,
    OP_LIGHT_HSL_STATUS,
    OP_LIGHT_LC_MODE_GET,
    OP_LIGHT_LC_MODE_SET,
    OP_LIGHT_LC_MODE_STATUS,
    OP_LIGHT_LC_OCCUPANCY_GET,
    OP_LIGHT_LC_OCCUPANCY_SET,
    OP_LIGHT_LC_OCCUPANCY_STATUS,
    OP_LIGHT_LC_ONOFF_GET,
    OP_LIGHT_LC_ONOFF_SET,
    OP_LIGHT_LC_ONOFF_STATUS,
    OP_LIGHT_LC_PROPERTY_GET,
    OP_LIGHT_LC_PROPERTY_SET,
    OP_LIGHT_LC_PROPERTY_STATUS,
    OP_LIGHT_LIGHTNESS_GET,
    OP_LIGHT_LIGHTNESS_SET,
    OP_LIGHT_LIGHTNESS_STATUS,
    OP_SENSOR_GET,
    OP_SENSOR_STATUS,
    OP_STEINEL_SENSOR_DATA_STATUS,
    SENSOR_SCHEMA_VERSION,
    STEINEL_COMPANY_ID,
)
from .direct import DirectResetClient
from .gatt import MeshGattTransport
from .mesh import ElementComposition, MeshNode
from .provisioning import Provisioner, ProvisioningError
from .sensor_protocol import (
    SENSOR_PROPERTIES,
    decode_sensor_status,
    decode_sensor_value,
)
from .sequence_store import SequenceStore
from .state_store import restore_light_states, serialize_light_states

_LOGGER = logging.getLogger(__name__)


class SteinelConnectionError(HomeAssistantError):
    """Raised when no suitable Mesh GATT role is reachable."""


class SteinelCoordinator:
    """Coordinate one BLE address and its provisioned Mesh node."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.transport: MeshGattTransport | None = None
        self.node: MeshNode | None = None
        self.elements: list[ElementComposition] = []
        self.available = False
        restored = restore_light_states(entry.data.get(CONF_LIGHT_STATES))
        self.is_on, self.brightness, self.color_temperature, self.hs_color = restored
        self.sensor_values: dict[tuple[int, str], Any] = {}
        self.lc_properties: dict[tuple[int, int], bytes] = {}
        self.lc_modes: dict[tuple[int, bool], bool] = {}
        self.sensor_settings: dict[tuple[int, int], bytes] = {}
        self.l810_profile: int | None = None
        self._lc_lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._sensor_request_lock = asyncio.Lock()
        self._motion_task: asyncio.Task[None] | None = None
        self._l810_forwarded_motion: bytes | None = None
        self._sequence_store: SequenceStore | None = None
        if str(entry.data.get(CONF_PRODUCT_ID)) == "7801":
            self._sequence_store = SequenceStore(
                hass, entry.entry_id, entry.data.get(CONF_SEQUENCE, 0)
            )
        self._connect_lock = asyncio.Lock()
        self._disconnect_event = asyncio.Event()
        self._reconnect_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._sensor_task: asyncio.Task[None] | None = None
        self._state_task: asyncio.Task[None] | None = None
        self._advertisement_task: asyncio.Task[None] | None = None
        self._cancel_advertisement: Callable[[], None] | None = None
        self.last_advertisement: datetime | None = None
        self.advertisement_available = False
        self._light_commands = 0
        self._suppress_disconnect = False
        self._connection_failed = False
        self._shutting_down = False
        self.reconnect_count = 0
        self.last_connected: datetime | None = None
        self.last_error: str | None = None
        self.loaded_options = dict(entry.options)
        domain_data = hass.data.setdefault(DOMAIN, {})
        self._proxy_lock: asyncio.Lock = domain_data.setdefault(
            "proxy_connection_lock", asyncio.Lock()
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device registry information."""
        product_id = self.entry.data.get(CONF_PRODUCT_ID)
        model = (
            f"Connect product {product_id}"
            if product_id is not None
            else "Connect Bluetooth Mesh"
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self.address)},
            manufacturer="Steinel",
            model=model,
            name=self.entry.title,
            sw_version=self.entry.data.get(CONF_FIRMWARE),
            hw_version=(
                str(self.entry.data[CONF_HARDWARE])
                if CONF_HARDWARE in self.entry.data
                else None
            ),
            serial_number=(
                str(self.entry.data[CONF_HASH_ID])
                if CONF_HASH_ID in self.entry.data
                else None
            ),
        )

    @property
    def reachable(self) -> bool:
        """Return reachability from an active proxy or recent advertising."""
        connected = bool(
            self.transport
            and self.transport.client
            and self.transport.client.is_connected
        )
        return connected or self.advertisement_available

    def supports_sensor_lighting(self, address: int) -> bool:
        """Find an LC controller with occupancy sensing anywhere on its node."""
        if not any(
            element.address == address and MODEL_LIGHT_LC_SERVER in element.sig_models
            for element in self.elements
        ):
            return False
        detected = self.entry.data.get(CONF_SENSOR_PROPERTIES, {})
        return any(
            {"motion", "presence"}.intersection(detected.get(str(element.address), []))
            for element in self.elements
        )

    async def async_setup(self) -> None:
        """Provision when needed, then connect and configure the node."""
        imported = self.entry.data.get(CONF_EXISTING_MESH, False)
        if imported and not all(
            self.entry.data.get(key)
            for key in (
                CONF_NET_KEY,
                CONF_APP_KEY,
                CONF_DEVICE_KEY,
                CONF_IMPORTED_ELEMENTS,
            )
        ):
            raise ConfigEntryError(
                "Imported Mesh credentials or composition are missing"
            )
        self._start_advertisement_monitor()
        if CONF_NET_KEY not in self.entry.data:
            self._update_entry(
                {
                    CONF_NET_KEY: os.urandom(16).hex(),
                    CONF_APP_KEY: os.urandom(16).hex(),
                    CONF_IV_INDEX: 0,
                    CONF_SEQUENCE: 0,
                    CONF_UNICAST_ADDRESS: DEFAULT_UNICAST_ADDRESS,
                }
            )
        try:
            if not imported and (
                CONF_DEVICE_KEY in self.entry.data
                and self.entry.data.get(CONF_NODE_STATE) == NODE_PENDING
                and self._advertises(MESH_PROVISIONING_SERVICE)
            ):
                self._remove_entry_keys(CONF_DEVICE_KEY, CONF_ELEMENT_COUNT)
            if not imported and CONF_DEVICE_KEY not in self.entry.data:
                await self._async_provision()
            await self._async_connect_proxy()
            if imported or (
                not self.entry.data.get(CONF_ELEMENTS)
                or self.entry.data.get(CONF_MODEL_SCHEMA_VERSION, 0)
                < MODEL_SCHEMA_VERSION
            ):
                await self._async_configure()
            else:
                self.elements = self._deserialize_elements(
                    self.entry.data[CONF_ELEMENTS]
                )
            if (
                self.entry.data.get(CONF_SENSOR_SCHEMA_VERSION, 0)
                < SENSOR_SCHEMA_VERSION
            ):
                await self._async_detect_sensor_properties()
            await self._async_setup_l810_sensors()
            await self._async_refresh_light_states()
            self.available = True
            self.last_connected = datetime.now(UTC)
            self._start_reconnect_monitor()
            self._start_sensor_polling()
            self._arm_idle_disconnect()
        except (
            BleakError,
            TimeoutError,
            ConnectionError,
            ProvisioningError,
            SteinelConnectionError,
        ) as err:
            self.last_error = str(err)
            await self.async_shutdown()
            raise ConfigEntryNotReady(str(err)) from err

    async def async_shutdown(self) -> None:
        """Disconnect and release the proxy slot."""
        self.available = False
        self._shutting_down = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
            self._reconnect_task = None
        if self._idle_task:
            self._idle_task.cancel()
            await asyncio.gather(self._idle_task, return_exceptions=True)
            self._idle_task = None
        for task_name in (
            "_sensor_task",
            "_motion_task",
            "_state_task",
            "_advertisement_task",
        ):
            task = getattr(self, task_name)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, task_name, None)
        if self._cancel_advertisement:
            self._cancel_advertisement()
            self._cancel_advertisement = None
        self._persist_light_states()
        if self.transport:
            await self.transport.disconnect()
        self.transport = None
        self.node = None
        self._notify()

    def _start_advertisement_monitor(self) -> None:
        """Listen for passive advertisements from this address."""
        if self._cancel_advertisement is not None:
            return
        self._cancel_advertisement = bluetooth.async_register_callback(
            self.hass,
            self._advertisement_received,
            {"address": self.address},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=False
        )
        if info:
            self._advertisement_received(info, None)

    @callback
    def _advertisement_received(self, info, _change) -> None:
        """Record passive reachability and advertised device metadata."""
        self.last_advertisement = datetime.now(UTC)
        self.advertisement_available = True
        data = parse_steinel_advertisement(
            info.manufacturer_data.get(STEINEL_COMPANY_ID, b""), info.name
        )
        allowed = {
            "product_id": CONF_PRODUCT_ID,
            "firmware": CONF_FIRMWARE,
            "bootloader": CONF_BOOTLOADER,
            "hardware": CONF_HARDWARE,
            "hash_id": CONF_HASH_ID,
        }
        updates = {
            target: data[source]
            for source, target in allowed.items()
            if source in data and self.entry.data.get(target) != data[source]
        }
        if updates:
            self._update_entry(updates)
        if self._advertisement_task:
            self._advertisement_task.cancel()
        self._advertisement_task = self.entry.async_create_background_task(
            self.hass,
            self._async_expire_advertisement(),
            f"steinel_ble advertisement timeout {self.address}",
        )
        self._notify()

    async def _async_expire_advertisement(self) -> None:
        await asyncio.sleep(
            float(
                self._option(CONF_ADVERTISEMENT_TIMEOUT, DEFAULT_ADVERTISEMENT_TIMEOUT)
            )
        )
        self.advertisement_available = False
        self._notify()

    def _option(self, key: str, default: Any) -> Any:
        """Return one typed config-entry option with a stable default."""
        return self.entry.options.get(key, default)

    @callback
    def _transport_disconnected(self) -> None:
        """Mark the node unavailable and wake the reconnect monitor."""
        if self._shutting_down or self._suppress_disconnect:
            return
        self._connection_failed = True
        self.available = False
        self._disconnect_event.set()
        self._notify()

    def _start_reconnect_monitor(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._shutting_down = False
            self._reconnect_task = self.entry.async_create_background_task(
                self.hass,
                self._async_reconnect_monitor(),
                f"steinel_ble reconnect {self.address}",
            )

    async def _async_reconnect_monitor(self) -> None:
        """Reconnect in the background after an unexpected BLE disconnect."""
        while not self._shutting_down:
            await self._disconnect_event.wait()
            self._disconnect_event.clear()
            delay = 2
            while not self._shutting_down and not self.available:
                await asyncio.sleep(delay)
                try:
                    async with self._connect_lock:
                        if self.available:
                            break
                        if self.transport:
                            await self.transport.disconnect()
                        await self._async_connect_proxy()
                    self.available = True
                    self.last_connected = datetime.now(UTC)
                    self.last_error = None
                    self.reconnect_count += 1
                    self._notify()
                except Exception as err:  # keep retrying while the entry is loaded
                    self.last_error = str(err)
                    delay = min(delay * 2, 60)

    def _cancel_idle_disconnect(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

    def _arm_idle_disconnect(self) -> None:
        """Release the BLE slot after inactivity when explicitly enabled."""
        self._cancel_idle_disconnect()
        if not self._option(CONF_DISCONNECT_WHEN_IDLE, DEFAULT_DISCONNECT_WHEN_IDLE):
            return
        self._idle_task = self.entry.async_create_background_task(
            self.hass,
            self._async_idle_disconnect(),
            f"steinel_ble idle disconnect {self.address}",
        )

    async def _async_idle_disconnect(self) -> None:
        await asyncio.sleep(
            float(
                self._option(CONF_IDLE_DISCONNECT_DELAY, DEFAULT_IDLE_DISCONNECT_DELAY)
            )
        )
        async with self._connect_lock:
            if not self.transport:
                return
            self._suppress_disconnect = True
            try:
                await self.transport.disconnect()
            finally:
                self._suppress_disconnect = False
            self.transport = None
            self.node = None

    async def _async_ble_device(self):
        device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if device is None:
            raise SteinelConnectionError(
                f"{self.address} is not reachable through a connectable "
                "Bluetooth adapter"
            )
        return device

    def _advertises(self, service_uuid: str) -> bool:
        """Return whether the latest connectable advertisement has a service."""
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        return bool(
            info
            and service_uuid
            in {uuid.lower() for uuid in getattr(info, "service_uuids", [])}
        )

    async def _async_provision(self) -> None:
        device = await self._async_ble_device()
        transport = MeshGattTransport(
            device,
            self.entry.title,
            MESH_PROVISIONING_DATA_IN,
            MESH_PROVISIONING_DATA_OUT,
            lambda _type, _payload: asyncio.sleep(0),
        )
        provisioner = Provisioner(transport)
        transport.handler = provisioner.handle_pdu
        await transport.connect(subscribe=False)
        try:
            if not transport.has_service(MESH_PROVISIONING_SERVICE):
                stale_proxy_role = transport.has_service(MESH_PROXY_SERVICE)
                cache_cleared = await transport.clear_cache()
                _LOGGER.debug(
                    "Provisioning service missing for %s; stale proxy role=%s, "
                    "backend cache cleared=%s",
                    self.address,
                    stale_proxy_role,
                    cache_cleared,
                )
                await transport.disconnect()
                await transport.connect(subscribe=False)
                if not transport.has_service(MESH_PROVISIONING_SERVICE):
                    if transport.has_service(MESH_PROXY_SERVICE):
                        await transport.disconnect()
                        await self._async_reset_foreign_mesh()
                        transport.device = await self._async_ble_device()
                        await transport.connect(subscribe=False)
                        if not transport.has_service(MESH_PROVISIONING_SERVICE):
                            raise ConfigEntryError(
                                "Device remained provisioned after Global Reset"
                            )
                    else:
                        raise SteinelConnectionError(
                            "Mesh Provisioning GATT service is unavailable"
                        )
            await transport.subscribe()

            async def persist_pending(device_key: bytes, elements: int) -> None:
                self._update_entry(
                    {
                        CONF_DEVICE_KEY: device_key.hex(),
                        CONF_ELEMENT_COUNT: elements,
                        CONF_NODE_STATE: NODE_PENDING,
                    }
                )

            static_hex = self.entry.data.get(CONF_STATIC_OOB, "")
            result = await provisioner.provision(
                bytes.fromhex(self.entry.data[CONF_NET_KEY]),
                0,
                self.entry.data[CONF_IV_INDEX],
                self.entry.data[CONF_UNICAST_ADDRESS],
                bytes.fromhex(static_hex) if static_hex else None,
                persist_pending,
                invite_attempts=int(
                    self._option(CONF_PROVISION_ATTEMPTS, DEFAULT_PROVISION_ATTEMPTS)
                ),
            )
            self._update_entry(
                {
                    CONF_DEVICE_KEY: result.device_key.hex(),
                    CONF_ELEMENT_COUNT: result.element_count,
                    CONF_NODE_STATE: NODE_COMPLETE,
                }
            )
            ir.async_delete_issue(self.hass, DOMAIN, f"reset_{self.entry.entry_id}")
        finally:
            await transport.disconnect()

    async def _async_reset_foreign_mesh(self) -> None:
        """Erase an unknown Mesh and verify the fresh PB-GATT role."""
        last_error: Exception | None = None
        for attempt in range(5):
            direct: DirectResetClient | None = None
            try:
                device = await self._async_ble_device()
                direct = DirectResetClient(device, self.entry.title)
                await direct.connect()
                await direct.reset()
            except Exception as err:
                last_error = err
                _LOGGER.debug(
                    "Global Reset attempt %s failed or rebooted without a response",
                    attempt + 1,
                    exc_info=True,
                )
            finally:
                if direct is not None:
                    await direct.disconnect()

            verify_deadline = asyncio.get_running_loop().time() + (
                40 if attempt == 4 else 12
            )
            while asyncio.get_running_loop().time() < verify_deadline:
                await asyncio.sleep(2)
                probe: MeshGattTransport | None = None
                try:
                    device = await self._async_ble_device()
                    probe = MeshGattTransport(
                        device,
                        self.entry.title,
                        MESH_PROVISIONING_DATA_IN,
                        MESH_PROVISIONING_DATA_OUT,
                        lambda _type, _payload: asyncio.sleep(0),
                    )
                    await probe.connect(subscribe=False)
                    if probe.has_service(MESH_PROVISIONING_SERVICE):
                        await asyncio.sleep(2)
                        return
                    await probe.clear_cache()
                except Exception as err:
                    last_error = err
                finally:
                    if probe is not None:
                        await probe.disconnect()
            if attempt < 4:
                await asyncio.sleep(2)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"reset_{self.entry.entry_id}",
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="reset_failed",
            translation_placeholders={"name": self.entry.title},
        )
        raise SteinelConnectionError(
            "Global Reset did not expose the Mesh Provisioning service: "
            f"{last_error or 'verification timed out'}"
        )

    async def _async_connect_proxy(self) -> None:
        """Queue proxy connection establishment across integration entries."""
        last_error: Exception | None = None
        attempts = int(self._option(CONF_CONNECT_ATTEMPTS, DEFAULT_CONNECT_ATTEMPTS))
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(min(5 * attempt, 15))
            try:
                async with self._proxy_lock:
                    await self._async_connect_proxy_attempt()
                return
            except (BleakError, ConnectionError, SteinelConnectionError) as err:
                last_error = err
                _LOGGER.debug(
                    "Proxy connection attempt %s failed", attempt + 1, exc_info=True
                )
        raise SteinelConnectionError(str(last_error or "Mesh Proxy was not discovered"))

    async def _async_connect_proxy_attempt(self) -> None:
        """Perform one connection attempt in the shared proxy slot."""
        device = await self._async_ble_device()
        transport = MeshGattTransport(
            device,
            self.entry.title,
            MESH_PROXY_DATA_IN,
            MESH_PROXY_DATA_OUT,
            lambda _type, _payload: asyncio.sleep(0),
            self._transport_disconnected,
        )
        await transport.connect(subscribe=False)
        if (
            not transport.has_service(MESH_PROXY_SERVICE)
            and await transport.clear_cache()
        ):
            await transport.disconnect()
            await transport.connect(subscribe=False)
        if not transport.has_service(MESH_PROXY_SERVICE):
            await transport.disconnect()
            raise SteinelConnectionError("Mesh Proxy GATT service is unavailable")
        node = MeshNode(
            transport,
            bytes.fromhex(self.entry.data[CONF_NET_KEY]),
            bytes.fromhex(self.entry.data[CONF_APP_KEY]),
            bytes.fromhex(self.entry.data[CONF_DEVICE_KEY]),
            self.entry.data[CONF_IV_INDEX],
            self.entry.data.get(CONF_SEQUENCE, 0),
            self.entry.data[CONF_UNICAST_ADDRESS],
            self._sequence_changed,
            self._access_received,
            float(self._option(CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT)),
            sequence_allocator=self._sequence_store.async_next
            if getattr(self, "_sequence_store", None)
            else None,
        )
        transport.handler = node.handle_pdu
        node.src = self.entry.data.get(CONF_SOURCE_ADDRESS, 0x0001)
        node.iv_update = bool(self.entry.data.get(CONF_IV_UPDATE, False))
        node.iv_state_verified = CONF_IV_UPDATE in self.entry.data
        node.iv_sync_pending = bool(self.entry.data.get(CONF_EXISTING_MESH))
        self.transport, self.node = transport, node
        try:
            # Attach the handler before subscribing: proxies may send their
            # first authenticated beacon as soon as notifications are enabled.
            await transport.subscribe()
            if self.entry.data.get(CONF_EXISTING_MESH):
                if await node.synchronize_iv_index():
                    self._update_entry(
                        {CONF_IV_INDEX: node.iv_index, CONF_IV_UPDATE: node.iv_update}
                    )
                    _LOGGER.info(
                        "Authenticated Mesh IV Index %s (IV Update=%s) for %s",
                        node.iv_index,
                        node.iv_update,
                        self.address,
                    )
                elif node.rx_stats["network_iv_mismatch"]:
                    raise SteinelConnectionError(
                        "Received Mesh traffic has an IVI mismatch with the IV Index, "
                        "and no valid network beacon was received. Check the exported "
                        "NetKey and current IV Index, and Bluetooth signal strength."
                    )
                else:
                    _LOGGER.debug(
                        "No authenticated Mesh beacon received for %s; "
                        "using configured IV Index %s",
                        self.address,
                        node.iv_index,
                    )
            await node.initialize_proxy_filter()
        except BaseException:
            await transport.disconnect()
            self.transport = self.node = None
            raise
        self._connection_failed = False
        self.last_connected = datetime.now(UTC)
        self.last_error = None

    async def _async_configure(self) -> None:
        assert self.node is not None
        if self.entry.data.get(CONF_EXISTING_MESH):
            self.elements = self._deserialize_elements(
                self.entry.data[CONF_IMPORTED_ELEMENTS]
            )
        else:
            self.elements = await self.node.configure(
                self.entry.data.get(CONF_ELEMENT_COUNT, 1)
            )
        self._update_entry(
            {
                CONF_ELEMENTS: self._serialize_elements(self.elements),
                CONF_MODEL_SCHEMA_VERSION: MODEL_SCHEMA_VERSION,
                CONF_NODE_STATE: NODE_COMPLETE,
            }
        )

    async def _async_refresh_light_states(self) -> None:
        """Best-effort initial state queries for every exposed light element."""
        if self.node is None:
            return
        primary = self.entry.data[CONF_UNICAST_ADDRESS]
        for element in self.elements:
            is_light = (
                element.address == primary
                and MODEL_GENERIC_ONOFF_SERVER in element.sig_models
            ) or bool(
                element.sig_models
                & {
                    MODEL_LIGHT_LIGHTNESS_SERVER,
                    MODEL_LIGHT_CTL_SERVER,
                    MODEL_LIGHT_HSL_SERVER,
                    MODEL_LIGHT_LC_SERVER,
                }
            )
            if not is_light:
                continue
            queries = []
            if MODEL_GENERIC_ONOFF_SERVER in element.sig_models:
                queries.append((OP_GENERIC_ONOFF_GET, OP_GENERIC_ONOFF_STATUS))
            if MODEL_LIGHT_LIGHTNESS_SERVER in element.sig_models:
                queries.append((OP_LIGHT_LIGHTNESS_GET, OP_LIGHT_LIGHTNESS_STATUS))
            if MODEL_LIGHT_CTL_SERVER in element.sig_models:
                queries.append((OP_LIGHT_CTL_GET, OP_LIGHT_CTL_STATUS))
            if MODEL_LIGHT_HSL_SERVER in element.sig_models:
                queries.append((OP_LIGHT_HSL_GET, OP_LIGHT_HSL_STATUS))
            if MODEL_LIGHT_LC_SERVER in element.sig_models:
                queries.append((OP_LIGHT_LC_ONOFF_GET, OP_LIGHT_LC_ONOFF_STATUS))
            for opcode, response in queries:
                try:
                    await self.node.request(
                        element.address, opcode, b"", response, timeout=5
                    )
                except Exception:
                    _LOGGER.debug(
                        "Initial state query 0x%04X failed for element 0x%04X",
                        opcode,
                        element.address,
                        exc_info=True,
                    )

    async def _async_setup_l810_sensors(self) -> None:
        """Bind and discover the L 810 SC motion and illuminance elements."""
        if str(self.entry.data.get(CONF_PRODUCT_ID)) != "7801":
            return
        assert self.node is not None
        primary = self.entry.data[CONF_UNICAST_ADDRESS]
        expected = ((primary + 2, "motion"), (primary + 3, "illuminance"))
        elements = {element.address: element for element in self.elements}
        models_changed = False
        for address, _name in expected:
            element = elements.get(address)
            if element is None or MODEL_SENSOR_SERVER in element.sig_models:
                continue
            if self.entry.data.get(CONF_EXISTING_MESH):
                continue
            bind = (
                address.to_bytes(2, "little")
                + b"\x00\x00"
                + MODEL_SENSOR_SERVER.to_bytes(2, "little")
            )
            try:
                _src, status = await self.node.request(
                    primary, 0x803D, bind, 0x803E, device_key=True, timeout=2
                )
            except Exception:
                _LOGGER.debug(
                    "L 810 Sensor Server bind failed on 0x%04X", address, exc_info=True
                )
                continue
            if len(status) >= 7 and status[0] == 0 and (status[1:7] == bind):
                element.sig_models.add(MODEL_SENSOR_SERVER)
                models_changed = True
        if models_changed:
            self._update_entry({CONF_ELEMENTS: self._serialize_elements(self.elements)})
        properties = dict(self.entry.data.get(CONF_SENSOR_PROPERTIES, {}))
        changed = False
        for address, name in expected:
            element = elements.get(address)
            if element is None or MODEL_SENSOR_SERVER not in element.sig_models:
                continue
            names = list(properties.get(str(address), []))
            if name in names:
                continue
            try:
                raw = await self.async_get_sensor(
                    address, SENSOR_PROPERTIES[name], timeout=2
                )
            except Exception:
                _LOGGER.debug("L 810 %s discovery failed", name, exc_info=True)
                continue
            self.sensor_values[address, name] = decode_sensor_value(name, raw)
            properties[str(address)] = [*names, name]
            changed = True
        if changed:
            self._update_entry({CONF_SENSOR_PROPERTIES: properties})

    async def _async_detect_sensor_properties(self) -> None:
        """Probe vendor sensor properties once and persist only valid replies."""
        if str(self.entry.data.get(CONF_PRODUCT_ID)) == "7801":
            await self._async_setup_l810_sensors()
            self._update_entry({CONF_SENSOR_SCHEMA_VERSION: SENSOR_SCHEMA_VERSION})
            return
        detected: dict[str, list[str]] = {}
        for element in self.elements:
            if (
                STEINEL_COMPANY_ID,
                MODEL_STEINEL_SENSOR_EXTENSION,
            ) not in element.vendor_models:
                continue
            names: list[str] = []
            for name, property_id in SENSOR_PROPERTIES.items():
                try:
                    raw = await self.async_get_sensor(
                        element.address, property_id, timeout=2
                    )
                except Exception:
                    continue
                if decode_sensor_value(name, raw).value is not None:
                    names.append(name)
            detected[str(element.address)] = names
        self._update_entry(
            {
                CONF_SENSOR_PROPERTIES: detected,
                CONF_SENSOR_SCHEMA_VERSION: SENSOR_SCHEMA_VERSION,
            }
        )

    async def async_get_lc_property(self, address: int, property_id: int) -> bytes:
        """Read a standard Light LC property."""
        parameters = await self._async_lc_request(
            address,
            OP_LIGHT_LC_PROPERTY_GET,
            property_id.to_bytes(2, "little"),
            OP_LIGHT_LC_PROPERTY_STATUS,
        )
        return self._store_lc_property(address, property_id, parameters)

    async def async_set_lc_property(
        self, address: int, property_id: int, value: bytes
    ) -> bytes:
        """Set a standard Light LC property and wait for confirmation."""
        parameters = await self._async_lc_request(
            address,
            OP_LIGHT_LC_PROPERTY_SET,
            property_id.to_bytes(2, "little") + value,
            OP_LIGHT_LC_PROPERTY_STATUS,
        )
        return self._store_lc_property(address, property_id, parameters)

    def _store_lc_property(
        self, address: int, property_id: int, parameters: bytes
    ) -> bytes:
        if (
            len(parameters) < 2
            or int.from_bytes(parameters[:2], "little") != property_id
        ):
            raise HomeAssistantError("Unexpected Light LC property response")
        value = parameters[2:]
        self.lc_properties[(address, property_id)] = value
        self._notify()
        return value

    async def _async_lc_request(
        self, address: int, opcode: int, parameters: bytes, response: int
    ) -> bytes:
        # Property Status has no transaction ID: concurrent property requests
        # to one controller must not consume each other's status replies.
        async with self._lc_lock:
            try:
                node = await self.async_ensure_connected()
                _src, data = await node.request(address, opcode, parameters, response)
                return data
            except (TimeoutError, BleakError, ConnectionError) as err:
                raise self._command_error(err, address) from err
            finally:
                self._arm_idle_disconnect()

    async def async_get_lc_mode(self, address: int, occupancy: bool) -> bool:
        data = await self._async_lc_request(
            address,
            OP_LIGHT_LC_OCCUPANCY_GET if occupancy else OP_LIGHT_LC_MODE_GET,
            b"",
            OP_LIGHT_LC_OCCUPANCY_STATUS if occupancy else OP_LIGHT_LC_MODE_STATUS,
        )
        return self._store_lc_mode(address, occupancy, data)

    async def async_set_lc_mode(
        self, address: int, occupancy: bool, state: bool
    ) -> bool:
        data = await self._async_lc_request(
            address,
            OP_LIGHT_LC_OCCUPANCY_SET if occupancy else OP_LIGHT_LC_MODE_SET,
            bytes([int(state)]),
            OP_LIGHT_LC_OCCUPANCY_STATUS if occupancy else OP_LIGHT_LC_MODE_STATUS,
        )
        return self._store_lc_mode(address, occupancy, data)

    def _store_lc_mode(self, address: int, occupancy: bool, data: bytes) -> bool:
        if len(data) != 1 or data[0] not in (0, 1):
            raise HomeAssistantError("Invalid Light LC mode response")
        value = bool(data[0])
        self.lc_modes[(address, occupancy)] = value
        self._notify()
        return value

    async def async_ensure_connected(self) -> MeshNode:
        """Reconnect the proxy before a command if necessary."""
        self._cancel_idle_disconnect()
        async with self._connect_lock:
            if (
                self.transport
                and self.transport.client
                and self.transport.client.is_connected
                and self.node
                and not self._connection_failed
            ):
                return self.node
            if self.transport:
                await self.transport.disconnect()
            await self._async_connect_proxy()
            self.available = True
            self._notify()
            assert self.node is not None
            return self.node

    def _command_error(self, err: Exception, address: int) -> HomeAssistantError:
        """Keep a missed model reply separate from a lost BLE connection."""
        self.last_error = (
            "Timed out waiting for a Bluetooth Mesh status response "
            f"from element 0x{address:04X}"
            if isinstance(err, TimeoutError)
            else str(err)
        )
        connected = bool(
            self.transport
            and self.transport.client
            and self.transport.client.is_connected
        )
        if not isinstance(err, TimeoutError) or not connected:
            self._transport_disconnected()
        _LOGGER.warning("Mesh command failed for %s: %s", self.address, self.last_error)
        return HomeAssistantError(self.last_error)

    async def async_set_light(
        self,
        address: int,
        *,
        on: bool,
        brightness: int | None = None,
        use_lc: bool = False,
    ) -> None:
        """Set Generic/LC OnOff and optional Light Lightness Actual."""
        self._light_commands += 1
        try:
            node = await self.async_ensure_connected()
            remembered_brightness = self.brightness.get(address)
            if brightness is not None:
                level = round(brightness * 65535 / 255)
                await node.request(
                    address,
                    OP_LIGHT_LIGHTNESS_SET,
                    level.to_bytes(2, "little") + bytes((node.next_tid(),)),
                    OP_LIGHT_LIGHTNESS_STATUS,
                )
            # A non-zero Light Lightness Actual Set also turns the light on
            # through the model's state bindings. Sending an OnOff Set after
            # it makes some Steinel firmware restore its configured OnPowerUp
            # lightness, which is commonly 100 percent.
            if brightness is None or not on:
                opcode = OP_LIGHT_LC_ONOFF_SET if use_lc else OP_GENERIC_ONOFF_SET
                response = (
                    OP_LIGHT_LC_ONOFF_STATUS if use_lc else OP_GENERIC_ONOFF_STATUS
                )
                await node.request(
                    address, opcode, bytes((int(on), node.next_tid())), response
                )
            if (
                brightness is None
                and on
                and remembered_brightness
                and any(
                    element.address == address
                    and MODEL_LIGHT_LIGHTNESS_SERVER in element.sig_models
                    for element in self.elements
                )
                and self._option(CONF_RESTORE_BRIGHTNESS, DEFAULT_RESTORE_BRIGHTNESS)
            ):
                # The L 845 C first applies its configured LC/OnPowerUp level
                # after OnOff Set. Restore Home Assistant's last acknowledged
                # lightness after that internal state transition has settled.
                await asyncio.sleep(
                    float(self._option(CONF_BRIGHTNESS_DELAY, DEFAULT_BRIGHTNESS_DELAY))
                )
                level = round(remembered_brightness * 65535 / 255)
                await node.request(
                    address,
                    OP_LIGHT_LIGHTNESS_SET,
                    level.to_bytes(2, "little") + bytes((node.next_tid(),)),
                    OP_LIGHT_LIGHTNESS_STATUS,
                )
            self.is_on[address] = on
            if brightness is not None:
                self.brightness[address] = brightness
            self.available = True
            self.last_error = None
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(err, address) from err
        finally:
            self._light_commands -= 1
            self._schedule_state_persist()
            self._arm_idle_disconnect()
            self._notify()

    async def async_set_ctl(
        self, address: int, brightness: int, temperature_kelvin: int
    ) -> None:
        """Set Light CTL lightness and color temperature."""
        self._light_commands += 1
        try:
            node = await self.async_ensure_connected()
            lightness = round(brightness * 65535 / 255)
            parameters = (
                lightness.to_bytes(2, "little")
                + temperature_kelvin.to_bytes(2, "little")
                + b"\x00\x00"
                + bytes((node.next_tid(),))
            )
            await node.request(
                address,
                OP_LIGHT_CTL_SET,
                parameters,
                OP_LIGHT_CTL_STATUS,
            )
            self.brightness[address] = brightness
            self.is_on[address] = lightness > 0
            self.available = True
            self.last_error = None
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(err, address) from err
        finally:
            self._light_commands -= 1
            self._schedule_state_persist()
            self._arm_idle_disconnect()
            self._notify()

    async def async_set_hsl(
        self, address: int, brightness: int, hue: float, saturation: float
    ) -> None:
        """Set Light HSL lightness, hue, and saturation."""
        self._light_commands += 1
        try:
            node = await self.async_ensure_connected()
            lightness = round(brightness * 65535 / 255)
            hue_raw = round((hue % 360) * 65535 / 360)
            saturation_raw = round(max(0, min(100, saturation)) * 65535 / 100)
            parameters = (
                lightness.to_bytes(2, "little")
                + hue_raw.to_bytes(2, "little")
                + saturation_raw.to_bytes(2, "little")
                + bytes((node.next_tid(),))
            )
            await node.request(
                address,
                OP_LIGHT_HSL_SET,
                parameters,
                OP_LIGHT_HSL_STATUS,
            )
            self.brightness[address] = brightness
            self.is_on[address] = lightness > 0
            self.available = True
            self.last_error = None
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(err, address) from err
        finally:
            self._light_commands -= 1
            self._schedule_state_persist()
            self._arm_idle_disconnect()
            self._notify()

    async def async_get_sensor(
        self, address: int, property_id: int, timeout: float | None = None
    ) -> bytes:
        """Read one property from a Steinel Sensor Extension server."""

        def matches(parameters: bytes) -> bool:
            try:
                return property_id in decode_sensor_status(parameters)
            except ValueError:
                return False

        try:
            node = await self.async_ensure_connected()
            if str(self.entry.data.get(CONF_PRODUCT_ID)) == "7801":
                # Firmware 1.1.30 returns lux only in unfiltered SIG Sensor Status.
                async with self._sensor_request_lock:
                    _, parameters = await node.request(
                        address,
                        OP_SENSOR_GET,
                        b""
                        if property_id == SENSOR_PROPERTIES["illuminance"]
                        else property_id.to_bytes(2, "little"),
                        OP_SENSOR_STATUS,
                        timeout=timeout
                        or float(
                            self._option(CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT)
                        ),
                        response_matcher=matches,
                    )
                values = decode_sensor_status(parameters)
                if property_id not in values:
                    raise ValueError(
                        f"Sensor Status does not contain 0x{property_id:04X}"
                    )
                return values[property_id]
            _src, _opcode, parameters = await node.request_vendor(
                address,
                0xD0,
                property_id.to_bytes(2, "little"),
                timeout=timeout
                or float(self._option(CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT)),
                response_opcode=OP_STEINEL_SENSOR_DATA_STATUS,
                response_matcher=matches,
            )
            return decode_sensor_status(parameters)[property_id]
        finally:
            self._arm_idle_disconnect()

    async def _async_sensor_setting_request(
        self,
        node: MeshNode,
        address: int,
        opcode: int,
        setting_id: int,
        value: bytes = b"",
    ) -> bytes:
        """Request one authenticated STEINEL sensor setting."""
        prefix = SENSOR_PROPERTIES["motion"].to_bytes(
            2, "little"
        ) + setting_id.to_bytes(2, "little")
        src, response_opcode, parameters = await node.request_vendor(
            address,
            opcode,
            prefix + value,
            timeout=float(self._option(CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT)),
            response_opcode=0xD76305,
            response_matcher=lambda parameters: parameters.startswith(prefix),
        )
        if src != address or response_opcode != 0xD76305:
            raise ValueError("Unexpected STEINEL sensor setting response")
        if len(parameters) < 5 or not parameters.startswith(prefix):
            raise ValueError("Malformed STEINEL sensor setting response")
        if parameters[4] not in (1, 3) or (opcode == 0xD5 and parameters[4] != 3):
            raise ValueError("STEINEL sensor setting access denied")
        raw = parameters[5:]
        self.sensor_settings[(address, setting_id)] = raw
        self._notify()
        return raw

    async def async_get_sensor_setting(self, address: int, setting_id: int) -> bytes:
        """Read an L 810 sensor setting."""
        try:
            node = await self.async_ensure_connected()
            async with self._sensor_request_lock:
                return await self._async_sensor_setting_request(
                    node, address, 0xD4, setting_id
                )
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(err, address) from err
        finally:
            self._arm_idle_disconnect()

    async def async_set_sensor_setting(
        self, address: int, setting_id: int, value: bytes
    ) -> bytes:
        """Write an L 810 sensor setting."""
        try:
            node = await self.async_ensure_connected()
            async with self._sensor_request_lock:
                return await self._async_sensor_setting_request(
                    node, address, 0xD5, setting_id, value
                )
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(err, address) from err
        finally:
            self._arm_idle_disconnect()

    async def async_set_sensor_range(
        self, address: int, range_index: int, percentage: float
    ) -> bytes:
        """Change one L 810 range while preserving the other range."""
        if range_index not in (0, 1) or not 0 <= percentage <= 100:
            raise ValueError("STEINEL range value outside the supported range")
        try:
            node = await self.async_ensure_connected()
            async with self._sensor_request_lock:
                current = bytearray(
                    await self._async_sensor_setting_request(
                        node, address, 0xD4, 0xFFF5
                    )
                )
                if len(current) != 4 or range_index not in (0, 1):
                    raise ValueError("Malformed STEINEL range setting")
                current[0 if range_index == 0 else 3] = round(percentage * 2)
                return await self._async_sensor_setting_request(
                    node, address, 0xD5, 0xFFF5, bytes(current)
                )
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(err, address) from err
        finally:
            self._arm_idle_disconnect()

    async def async_get_l810_profile(self) -> int:
        """Read the active L 810 operating profile."""
        try:
            node = await self.async_ensure_connected()
            controller = self.entry.data[CONF_UNICAST_ADDRESS] + 1
            async with self._lc_lock:
                src, opcode, parameters = await node.request_vendor(
                    controller,
                    0xC4,
                    b"\x1f\x00",
                    response_opcode=0xC76305,
                    response_matcher=lambda parameters: parameters.startswith(
                        b"\x1f\x00"
                    ),
                )
            if src != controller or opcode != 0xC76305:
                raise ValueError("Unexpected STEINEL profile response")
            if len(parameters) != 4 or not parameters.startswith(b"\x1f\x00"):
                raise ValueError("Malformed STEINEL profile response")
            self.l810_profile = int.from_bytes(parameters[2:4], "little")
            self._notify()
            return self.l810_profile
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(
                err, self.entry.data[CONF_UNICAST_ADDRESS] + 1
            ) from err
        finally:
            self._arm_idle_disconnect()

    async def async_set_l810_profile(self, profile: int) -> None:
        """Activate and remember a built-in L 810 operating profile."""
        if profile not in (3, 4, 5, 6):
            raise ValueError("Unsupported STEINEL operating profile")
        try:
            node = await self.async_ensure_connected()
            primary = self.entry.data[CONF_UNICAST_ADDRESS]
            controller = primary + 1
            scene = profile.to_bytes(2, "little")
            async with self._lc_lock:
                _, scene_status = await node.request(
                    primary, 0x8242, scene + bytes((node.next_tid(),)), 0x5E
                )
                if (
                    len(scene_status) < 3
                    or scene_status[0] != 0
                    or (
                        scene_status[1:3] != scene
                        and (len(scene_status) < 6 or scene_status[3:5] != scene)
                    )
                ):
                    raise ValueError("STEINEL operating scene was not acknowledged")
                _, _, parameters = await node.request_vendor(
                    controller,
                    0xC5,
                    b"\x1f\x00" + scene,
                    response_opcode=0xC76305,
                    response_matcher=lambda parameters: parameters.startswith(
                        b"\x1f\x00"
                    ),
                )
                if parameters != b"\x1f\x00" + scene:
                    raise ValueError("STEINEL operating profile was not acknowledged")
                if profile == 3:
                    _, status = await node.request(
                        controller,
                        OP_LIGHT_LC_MODE_SET,
                        b"\x01",
                        OP_LIGHT_LC_MODE_STATUS,
                    )
                    if not self._store_lc_mode(controller, False, status):
                        raise ValueError(
                            "STEINEL automatic light control was not enabled"
                        )
                    _, status = await node.request(
                        controller,
                        OP_LIGHT_LC_OCCUPANCY_SET,
                        b"\x01",
                        OP_LIGHT_LC_OCCUPANCY_STATUS,
                    )
                    if not self._store_lc_mode(controller, True, status):
                        raise ValueError("STEINEL motion automation was not enabled")
                self.l810_profile = profile
                self._notify()
        except (TimeoutError, BleakError, ConnectionError) as err:
            raise self._command_error(
                err, self.entry.data[CONF_UNICAST_ADDRESS] + 1
            ) from err
        finally:
            self._arm_idle_disconnect()

    async def _async_l810_motion_poll_loop(self) -> None:
        """Poll L 810 motion after startup without monopolizing Bluetooth."""
        await asyncio.sleep(5)
        address = self.entry.data[CONF_UNICAST_ADDRESS] + 2
        while not self._shutting_down:
            if not self._light_commands:
                try:
                    raw = await self.async_get_sensor(
                        address, SENSOR_PROPERTIES["motion"], timeout=2
                    )
                except Exception:
                    _LOGGER.debug("L 810 motion polling failed", exc_info=True)
                else:
                    value = decode_sensor_value("motion", raw)
                    self.sensor_values[address, "motion"] = value
                    if value.value is not None and raw != self._l810_forwarded_motion:
                        try:
                            await self._async_forward_l810_motion(raw)
                            self._l810_forwarded_motion = raw
                        except Exception:
                            _LOGGER.debug(
                                "Forwarding L 810 motion to LC failed", exc_info=True
                            )
                    self._notify()
            # Motion pulses are shorter than the shared sensor polling interval.
            await asyncio.sleep(0.25)

    async def _async_forward_l810_motion(self, raw: bytes) -> None:
        """Forward the L 810 sensor state to its separate Light LC element."""
        if decode_sensor_value("motion", raw).value is None:
            return
        node = await self.async_ensure_connected()
        controller = self.entry.data[CONF_UNICAST_ADDRESS] + 1
        parameters = b"@\x08" + raw[:1]
        await node.send_access(controller, OP_SENSOR_STATUS, parameters)

    async def _async_forward_l810_illuminance(self, raw: bytes) -> None:
        """Forward measured daylight to the separate L 810 Light LC element."""
        if len(raw) != 3 or raw == b"\xff\xff\xff":
            return
        node = await self.async_ensure_connected()
        controller = self.entry.data[CONF_UNICAST_ADDRESS] + 1
        parameters = b"\xc4\t" + raw
        await node.send_access(controller, OP_SENSOR_STATUS, parameters)

    def _start_sensor_polling(self) -> None:
        """Start one polling loop for all properties of this node."""
        properties = self.entry.data.get(CONF_SENSOR_PROPERTIES, {})
        if properties and (self._sensor_task is None or self._sensor_task.done()):
            self._sensor_task = self.entry.async_create_background_task(
                self.hass,
                self._async_sensor_poll_loop(),
                f"steinel_ble sensor polling {self.address}",
            )

        if (
            str(self.entry.data.get(CONF_PRODUCT_ID)) == "7801"
            and any("motion" in names for names in properties.values())
            and (self._motion_task is None or self._motion_task.done())
        ):
            self._motion_task = self.entry.async_create_background_task(
                self.hass,
                self._async_l810_motion_poll_loop(),
                "steinel_ble L 810 motion polling",
            )

    async def _async_sensor_poll_loop(self) -> None:
        await self._async_poll_sensors()
        while not self._shutting_down:
            await asyncio.sleep(
                float(self._option(CONF_SENSOR_INTERVAL, DEFAULT_SENSOR_INTERVAL))
            )
            await self._async_poll_sensors()

    async def _async_poll_sensors(self) -> None:
        """Refresh all properties in one low-priority connection session."""
        if self._light_commands:
            return
        properties = self.entry.data.get(CONF_SENSOR_PROPERTIES, {})
        changed = False
        for address_text, names in properties.items():
            for name in names:
                if self._light_commands:
                    break
                property_id = SENSOR_PROPERTIES.get(name)
                if property_id is None:
                    continue
                # The dedicated L 810 loop owns motion reads and forwarding.
                if (
                    str(self.entry.data.get(CONF_PRODUCT_ID)) == "7801"
                    and name == "motion"
                ):
                    continue
                try:
                    raw = await self.async_get_sensor(int(address_text), property_id)
                except Exception:
                    _LOGGER.debug(
                        "Sensor property 0x%04X read failed for element 0x%04X",
                        property_id,
                        int(address_text),
                        exc_info=True,
                    )
                    continue
                value = decode_sensor_value(name, raw)
                self.sensor_values[int(address_text), name] = value
                if (
                    name == "illuminance"
                    and value.value is not None
                    and (str(self.entry.data.get(CONF_PRODUCT_ID)) == "7801")
                    and (int(address_text) == self.entry.data[CONF_UNICAST_ADDRESS] + 3)
                    and (not self._light_commands)
                ):
                    try:
                        await self._async_forward_l810_illuminance(raw)
                    except Exception:
                        _LOGGER.debug(
                            "Forwarding L 810 illuminance to LC failed", exc_info=True
                        )
                changed = True
        if changed:
            self._notify()

    @callback
    def _schedule_state_persist(self) -> None:
        """Persist acknowledged light state with a short write debounce."""
        if self._shutting_down:
            return
        if self._state_task:
            self._state_task.cancel()
        self._state_task = self.entry.async_create_background_task(
            self.hass,
            self._async_persist_light_states(),
            f"steinel_ble persist state {self.address}",
        )

    async def _async_persist_light_states(self) -> None:
        await asyncio.sleep(2)
        self._persist_light_states()

    @callback
    def _persist_light_states(self) -> None:
        states = serialize_light_states(
            self.is_on, self.brightness, self.color_temperature, self.hs_color
        )
        if self.entry.data.get(CONF_LIGHT_STATES) != states:
            self._update_entry({CONF_LIGHT_STATES: states})

    @callback
    def _sequence_changed(self, sequence: int) -> None:
        self._update_entry({CONF_SEQUENCE: sequence})

    @callback
    def _access_received(self, src: int, opcode: int, parameters: bytes) -> None:
        if opcode == OP_LIGHT_LC_PROPERTY_STATUS and len(parameters) >= 2:
            property_id = int.from_bytes(parameters[:2], "little")
            self.lc_properties[(src, property_id)] = parameters[2:]
        elif (
            opcode in (OP_LIGHT_LC_MODE_STATUS, OP_LIGHT_LC_OCCUPANCY_STATUS)
            and len(parameters) == 1
            and parameters[0] in (0, 1)
        ):
            self.lc_modes[(src, opcode == OP_LIGHT_LC_OCCUPANCY_STATUS)] = bool(
                parameters[0]
            )
        if opcode in (OP_SENSOR_STATUS, OP_STEINEL_SENSOR_DATA_STATUS):
            self._sensor_extension_received(src, opcode, parameters)
        if (
            opcode == 0xC76305
            and src == self.entry.data[CONF_UNICAST_ADDRESS] + 1
            and len(parameters) == 4
            and parameters[:2] == b"\x1f\x00"
        ):
            self.l810_profile = int.from_bytes(parameters[2:], "little")
        if opcode in (OP_GENERIC_ONOFF_STATUS, OP_LIGHT_LC_ONOFF_STATUS) and parameters:
            self.is_on[src] = bool(parameters[0])
        elif opcode == OP_LIGHT_LIGHTNESS_STATUS and len(parameters) >= 2:
            level = int.from_bytes(parameters[:2], "little")
            self.brightness[src] = round(level * 255 / 65535)
            self.is_on[src] = level > 0
        elif opcode == OP_LIGHT_CTL_STATUS and len(parameters) >= 4:
            level = int.from_bytes(parameters[:2], "little")
            self.brightness[src] = round(level * 255 / 65535)
            self.color_temperature[src] = int.from_bytes(parameters[2:4], "little")
            self.is_on[src] = level > 0
        elif opcode == OP_LIGHT_HSL_STATUS and len(parameters) >= 6:
            level = int.from_bytes(parameters[:2], "little")
            hue = int.from_bytes(parameters[2:4], "little") * 360 / 65535
            saturation = int.from_bytes(parameters[4:6], "little") * 100 / 65535
            self.brightness[src] = round(level * 255 / 65535)
            self.hs_color[src] = (hue, saturation)
            self.is_on[src] = level > 0
        self._schedule_state_persist()
        self._notify()

    @callback
    def _sensor_extension_received(
        self, src: int, opcode: int, parameters: bytes
    ) -> None:
        """Apply all properties in a SIG or STEINEL sensor data report."""
        try:
            values = decode_sensor_status(parameters)
        except ValueError:
            _LOGGER.debug(
                "Malformed sensor status 0x%X from element 0x%04X: %s",
                opcode,
                src,
                parameters.hex(),
            )
            return
        for name, known_id in SENSOR_PROPERTIES.items():
            if known_id in values:
                self.sensor_values[(src, name)] = decode_sensor_value(
                    name, values[known_id]
                )

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe an entity to coordinator changes."""
        self._listeners.append(listener)

        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _update_entry(self, updates: dict[str, Any]) -> None:
        data = {**self.entry.data, **updates}
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    @callback
    def _remove_entry_keys(self, *keys: str) -> None:
        data = dict(self.entry.data)
        for key in keys:
            data.pop(key, None)
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    @staticmethod
    def _serialize_elements(elements: list[ElementComposition]) -> list[dict[str, Any]]:
        return [
            {
                "address": item.address,
                "sig_models": sorted(item.sig_models),
                "vendor_models": [list(model) for model in sorted(item.vendor_models)],
            }
            for item in elements
        ]

    @staticmethod
    def _deserialize_elements(data: list[dict[str, Any]]) -> list[ElementComposition]:
        return [
            ElementComposition(
                item["address"],
                set(item["sig_models"]),
                {tuple(model) for model in item["vendor_models"]},
            )
            for item in data
        ]
