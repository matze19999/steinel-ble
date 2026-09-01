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
    CONF_FIRMWARE,
    CONF_HARDWARE,
    CONF_HASH_ID,
    CONF_IDLE_DISCONNECT_DELAY,
    CONF_IV_INDEX,
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
    MODEL_LIGHT_LIGHTNESS_SERVER,
    MODEL_SCHEMA_VERSION,
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
    OP_LIGHT_LC_ONOFF_SET,
    OP_LIGHT_LC_ONOFF_STATUS,
    OP_LIGHT_LC_PROPERTY_GET,
    OP_LIGHT_LC_PROPERTY_SET,
    OP_LIGHT_LC_PROPERTY_STATUS,
    OP_LIGHT_LIGHTNESS_GET,
    OP_LIGHT_LIGHTNESS_SET,
    OP_LIGHT_LIGHTNESS_STATUS,
    SENSOR_SCHEMA_VERSION,
    STEINEL_COMPANY_ID,
)
from .direct import DirectResetClient
from .gatt import MeshGattTransport
from .mesh import ElementComposition, MeshNode
from .provisioning import Provisioner, ProvisioningError
from .sensor_protocol import (
    SENSOR_PROPERTIES,
    decode_sensor_value,
    strip_property_prefix,
)
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
        self._listeners: list[Callable[[], None]] = []
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
        """Return whether an element has motion and ambient-light sensing."""
        properties = set(
            self.entry.data.get(CONF_SENSOR_PROPERTIES, {}).get(str(address), [])
        )
        return {"motion", "illuminance"}.issubset(properties)

    async def async_setup(self) -> None:
        """Provision when needed, then connect and configure the node."""
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
            if (
                CONF_DEVICE_KEY in self.entry.data
                and self.entry.data.get(CONF_NODE_STATE) == NODE_PENDING
                and self._advertises(MESH_PROVISIONING_SERVICE)
            ):
                self._remove_entry_keys(CONF_DEVICE_KEY, CONF_ELEMENT_COUNT)
            if CONF_DEVICE_KEY not in self.entry.data:
                await self._async_provision()
            await self._async_connect_proxy()
            if (
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
        for task_name in ("_sensor_task", "_state_task", "_advertisement_task"):
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
        self._advertisement_task = self.hass.async_create_task(
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
        self.available = False
        self._disconnect_event.set()
        self._notify()

    def _start_reconnect_monitor(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._shutting_down = False
            self._reconnect_task = self.hass.async_create_task(
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
        self._idle_task = self.hass.async_create_task(
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
        await transport.subscribe()
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
        )
        transport.handler = node.handle_pdu
        self.transport, self.node = transport, node
        await node.initialize_proxy_filter()
        self.last_connected = datetime.now(UTC)
        self.last_error = None

    async def _async_configure(self) -> None:
        assert self.node is not None
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

    async def _async_detect_sensor_properties(self) -> None:
        """Probe vendor sensor properties once and persist only valid replies."""
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
        node = await self.async_ensure_connected()
        _src, parameters = await node.request(
            address,
            OP_LIGHT_LC_PROPERTY_GET,
            property_id.to_bytes(2, "little"),
            OP_LIGHT_LC_PROPERTY_STATUS,
        )
        return strip_property_prefix(property_id, parameters)

    async def async_set_lc_property(
        self, address: int, property_id: int, value: bytes
    ) -> None:
        """Set a standard Light LC property and wait for confirmation."""
        node = await self.async_ensure_connected()
        await node.request(
            address,
            OP_LIGHT_LC_PROPERTY_SET,
            property_id.to_bytes(2, "little") + value,
            OP_LIGHT_LC_PROPERTY_STATUS,
        )
        self._arm_idle_disconnect()

    async def async_ensure_connected(self) -> MeshNode:
        """Reconnect the proxy before a command if necessary."""
        self._cancel_idle_disconnect()
        async with self._connect_lock:
            if (
                self.transport
                and self.transport.client
                and self.transport.client.is_connected
                and self.node
            ):
                return self.node
            if self.transport:
                await self.transport.disconnect()
            await self._async_connect_proxy()
            self.available = True
            self._notify()
            assert self.node is not None
            return self.node

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
        except Exception:
            self.available = False
            raise
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
        except Exception:
            self.available = False
            raise
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
        except Exception:
            self.available = False
            raise
        finally:
            self._light_commands -= 1
            self._schedule_state_persist()
            self._arm_idle_disconnect()
            self._notify()

    async def async_get_sensor(
        self, address: int, property_id: int, timeout: float | None = None
    ) -> bytes:
        """Read one property from a Steinel Sensor Extension server."""
        try:
            node = await self.async_ensure_connected()
            _src, _opcode, parameters = await node.request_vendor(
                address,
                0xD0,
                property_id.to_bytes(2, "little"),
                timeout=timeout
                or float(self._option(CONF_COMMAND_TIMEOUT, DEFAULT_COMMAND_TIMEOUT)),
            )
            return strip_property_prefix(property_id, parameters)
        finally:
            self._arm_idle_disconnect()

    def _start_sensor_polling(self) -> None:
        """Start one polling loop for all properties of this node."""
        properties = self.entry.data.get(CONF_SENSOR_PROPERTIES, {})
        if properties and (self._sensor_task is None or self._sensor_task.done()):
            self._sensor_task = self.hass.async_create_task(
                self._async_sensor_poll_loop(),
                f"steinel_ble sensor polling {self.address}",
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
                try:
                    raw = await self.async_get_sensor(int(address_text), property_id)
                except Exception:
                    continue
                value = decode_sensor_value(name, raw)
                self.sensor_values[(int(address_text), name)] = value
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
        self._state_task = self.hass.async_create_task(
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
        if opcode >= 0xC00000 and opcode & 0xFFFF == 0x6305:
            self._sensor_extension_received(src, opcode, parameters)
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
        """Apply unsolicited Sensor Extension reports without polling."""
        vendor_opcode = opcode >> 16
        if vendor_opcode not in (0xD0, 0xD1, 0xDC) or len(parameters) < 3:
            return
        property_id = int.from_bytes(parameters[:2], "little")
        for name, known_id in SENSOR_PROPERTIES.items():
            if property_id == known_id:
                self.sensor_values[(src, name)] = decode_sensor_value(
                    name, parameters[2:]
                )
                break

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
