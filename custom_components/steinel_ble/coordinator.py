"""Runtime hub tying the mesh network state, BLE transport and wire protocol
together, plus a per-light polling coordinator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import ble
from .const import (
    BIND_MODELS,
    CAPABILITY_CTL,
    CAPABILITY_HSL,
    CAPABILITY_LIGHTNESS,
    CAPABILITY_ONOFF,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_UPDATE_INTERVAL,
    MESH_IDLE_DISCONNECT_SECONDS,
    MESH_RETRY_ATTEMPTS,
    MESH_RETRY_DELAY,
    NORDIC_DFU_SERVICE,
    STEINEL_COMPANY_ID,
)
from .mesh_store import MeshNetwork
from .protocol import (
    OP_CONFIG_APPKEY_STATUS,
    OP_CONFIG_MODEL_APP_STATUS,
    OP_CTL_STATUS,
    OP_HSL_STATUS,
    OP_LIGHTNESS_STATUS,
    OP_ONOFF_STATUS,
    MeshNetworkCodec,
    ProtocolError,
    ProvisioningCapabilities,
    ProvisioningSecrets,
    aes_cmac,
    config_app_key_add,
    config_model_app_bind,
    ctl_set_payload,
    device_access_segment_count,
    hsl_set_payload,
    lightness_set_payload,
    mesh_k1,
    mesh_s1,
    onoff_set_payload,
    parse_ctl_status,
    parse_hsl_status,
    parse_lightness_status,
    parse_onoff_status,
    provisioning_confirmation_inputs,
    provisioning_data,
    provisioning_ecdh,
    provisioning_public_key,
    provisioning_secrets,
    sig_opcode,
    validate_unicast_allocation,
)

_LOGGER = logging.getLogger(__name__)

_ONOFF_STATUS = sig_opcode(OP_ONOFF_STATUS)
_LIGHTNESS_STATUS = sig_opcode(OP_LIGHTNESS_STATUS)
_CTL_STATUS = sig_opcode(OP_CTL_STATUS)
_HSL_STATUS = sig_opcode(OP_HSL_STATUS)


@dataclass
class LightState:
    onoff: bool | None = None
    lightness: int | None = None
    ctl_lightness: int | None = None
    ctl_temperature: int | None = None
    hsl_lightness: int | None = None
    hue: int | None = None
    saturation: int | None = None
    available: bool = True
    last_seen: float = field(default_factory=time.monotonic)


async def _receive_provisioning_pdu(transport: ble.MeshTransport, expected_opcode: int, timeout: float) -> bytes:
    from .protocol import PROVISIONING_FAILURES

    pdu = await transport.receive(3, timeout)
    if not pdu:
        raise ProtocolError("received an empty provisioning PDU")
    if pdu[0] == 0x09:
        code = pdu[1] if len(pdu) > 1 else 0
        name = PROVISIONING_FAILURES.get(code, "UNKNOWN")
        raise ProtocolError(f"provisionee reported Provisioning Failed 0x{code:02X} ({name})")
    if pdu[0] != expected_opcode:
        raise ProtocolError(f"expected provisioning PDU 0x{expected_opcode:02X}, received 0x{pdu[0]:02X}")
    return pdu


class SteinelMeshHub:
    """Owns the shared mesh network state and all BLE operations for it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.network = MeshNetwork(hass)
        # Pooled Mesh Proxy connections, one per lamp address, reused across
        # consecutive commands/polls and closed again after a short idle
        # period - see const.MESH_IDLE_DISCONNECT_SECONDS.
        self._transports: dict[str, ble.MeshTransport] = {}
        self._transport_locks: dict[str, asyncio.Lock] = {}
        self._idle_handles: dict[str, asyncio.TimerHandle] = {}

    async def async_setup(self) -> None:
        await self.network.async_load()

    async def async_shutdown(self) -> None:
        """Close every pooled connection, e.g. when the integration unloads."""
        for address in list(self._transports):
            await self._async_close_transport(address)

    def _transport_lock(self, address: str) -> asyncio.Lock:
        lock = self._transport_locks.get(address)
        if lock is None:
            lock = self._transport_locks[address] = asyncio.Lock()
        return lock

    def _reset_idle_timer(self, address: str, transport: ble.MeshTransport) -> None:
        handle = self._idle_handles.pop(address, None)
        if handle is not None:
            handle.cancel()

        def _on_idle() -> None:
            self.hass.async_create_task(self._async_idle_close(address, transport))

        self._idle_handles[address] = asyncio.get_running_loop().call_later(MESH_IDLE_DISCONNECT_SECONDS, _on_idle)

    async def _async_idle_close(self, address: str, transport: ble.MeshTransport) -> None:
        # Takes the same lock a command holds for its whole duration, and
        # double-checks this is still the current connection for the
        # address, so an idle timer that fires while (or just before) a new
        # command started can never tear down a connection out from under it.
        async with self._transport_lock(address):
            if self._transports.get(address) is transport:
                await self._async_close_transport(address)

    async def _async_close_transport(self, address: str) -> None:
        handle = self._idle_handles.pop(address, None)
        if handle is not None:
            handle.cancel()
        transport = self._transports.pop(address, None)
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.__aexit__(None, None, None)

    async def _async_get_transport(self, address: str, timeout: float) -> ble.MeshTransport:
        transport = self._transports.get(address)
        if transport is not None and transport.client is not None and transport.client.is_connected:
            self._reset_idle_timer(address, transport)
            return transport
        await self._async_close_transport(address)
        transport = ble.MeshTransport(self.hass, address, timeout=timeout)
        await transport.__aenter__()
        codec = self.network.codec()
        await self._async_allow_all_proxy_traffic(transport, codec)
        self._transports[address] = transport
        self._reset_idle_timer(address, transport)
        return transport

    # -- provisioning ------------------------------------------------------

    async def async_provision(
        self, address: str, attention: int = 0, static_oob: bytes | None = None, timeout: float = 20.0
    ) -> tuple[int, ProvisioningCapabilities]:
        """Provision an unprovisioned lamp into this Home Assistant mesh."""
        import secrets

        from .protocol import allocate_unicast, checked_u8

        checked_u8(attention, "attention duration")
        auth_value = bytes(16) if static_oob is None else static_oob
        if len(auth_value) != 16:
            raise ValueError("Static OOB must be exactly 16 bytes")
        invite = bytes([0x00, attention])

        async with ble.MeshTransport(self.hass, address, provisioning=True, timeout=timeout) as transport:
            await transport.send(3, invite)
            capabilities_pdu = await _receive_provisioning_pdu(transport, 0x01, timeout)
            capabilities = ProvisioningCapabilities.parse(capabilities_pdu)
            if not capabilities.algorithms & 0x0001:
                raise ProtocolError("provisionee does not support the required P-256/AES-CMAC algorithm")
            if static_oob is not None and not capabilities.static_oob:
                raise ProtocolError("Static OOB was requested but the provisionee does not support it")

            unicast = allocate_unicast(self.network.nodes, capabilities.elements)
            validate_unicast_allocation(self.network.nodes, self.network.source, unicast, capabilities.elements)

            auth_method = 0x01 if static_oob is not None else 0x00
            start = bytes([0x02, 0x00, 0x00, auth_method, 0x00, 0x00])
            await transport.send(3, start)

            ec = self._need_ec()
            private_key = ec.generate_private_key(ec.SECP256R1())
            provisioner_public_key = provisioning_public_key(private_key)
            await transport.send(3, b"\x03" + provisioner_public_key)
            device_public_key_pdu = await _receive_provisioning_pdu(transport, 0x03, timeout)
            if len(device_public_key_pdu) != 65:
                raise ProtocolError("provisionee public key PDU is not 65 bytes")
            device_public_key = device_public_key_pdu[1:]
            ecdh_secret = provisioning_ecdh(private_key, device_public_key)
            confirmation_inputs = provisioning_confirmation_inputs(
                invite, capabilities_pdu, start, provisioner_public_key, device_public_key
            )
            confirmation_salt = mesh_s1(confirmation_inputs)
            confirmation_key = mesh_k1(ecdh_secret, confirmation_salt, b"prck")

            provisioner_random = secrets.token_bytes(16)
            provisioner_confirmation = aes_cmac(confirmation_key, provisioner_random + auth_value)
            await transport.send(3, b"\x05" + provisioner_confirmation)
            device_confirmation_pdu = await _receive_provisioning_pdu(transport, 0x05, timeout)
            if len(device_confirmation_pdu) != 17:
                raise ProtocolError("provisionee confirmation PDU is not 17 bytes")

            await transport.send(3, b"\x06" + provisioner_random)
            device_random_pdu = await _receive_provisioning_pdu(transport, 0x06, timeout)
            if len(device_random_pdu) != 17:
                raise ProtocolError("provisionee random PDU is not 17 bytes")
            device_random = device_random_pdu[1:]
            expected_confirmation = aes_cmac(confirmation_key, device_random + auth_value)
            if not secrets.compare_digest(device_confirmation_pdu[1:], expected_confirmation):
                raise ProtocolError("provisionee confirmation is invalid; aborting provisioning")

            derived: ProvisioningSecrets = provisioning_secrets(
                ecdh_secret, confirmation_inputs, provisioner_random, device_random
            )
            plaintext = provisioning_data(self.network.net_key, self.network.net_key_index, 0, self.network.iv_index, unicast)
            from cryptography.hazmat.primitives.ciphers.aead import AESCCM

            encrypted = AESCCM(derived.session_key, tag_length=8).encrypt(derived.session_nonce, plaintext, None)
            await transport.send(3, b"\x07" + encrypted)
            complete = await _receive_provisioning_pdu(transport, 0x08, timeout)
            if len(complete) != 1:
                raise ProtocolError("Provisioning Complete PDU contains unexpected data")

        node = {
            "address": address,
            "unicast": f"0x{unicast:04X}",
            "elements": capabilities.elements,
            "device_key": derived.device_key.hex().upper(),
            "configured": False,
            "capabilities": {},
        }
        await self.network.async_upsert_node(unicast, node)
        return unicast, capabilities

    @staticmethod
    def _need_ec() -> Any:
        from cryptography.hazmat.primitives.asymmetric import ec

        return ec

    async def async_bind_capabilities(self, unicast: int, timeout: float = 20.0) -> dict[str, bool]:
        """Add the shared AppKey and bind the OnOff/Lightness/CTL/HSL models,
        keeping whichever ones the lamp actually supports."""
        node = self.network.node_for_unicast(unicast)
        if node is None:
            raise ProtocolError(f"unknown node 0x{unicast:04X}")
        device_key = bytes.fromhex(node["device_key"])
        elements = int(node.get("elements", 1))
        address = node["address"]
        capabilities: dict[str, bool] = {}

        async with ble.MeshTransport(self.hass, address, timeout=timeout) as transport:
            codec = self.network.codec()
            await self._async_allow_all_proxy_traffic(transport, codec)

            appkey_payload = config_app_key_add(self.network.net_key_index, self.network.app_key_index, self.network.app_key)
            seg_count = device_access_segment_count(appkey_payload)
            first_seq = await self.network.async_reserve_sequence(seg_count)
            for pdu in codec.encode_device_access(unicast, appkey_payload, device_key, first_seq):
                await transport.send(0, pdu)
            status = await self._async_wait_device_status(
                transport, codec, device_key, unicast, OP_CONFIG_APPKEY_STATUS, timeout
            )
            if len(status) < 6 or status[2] not in (0x00, 0x06):
                raise ProtocolError(f"Config AppKey Add failed: status 0x{status[2] if len(status) >= 6 else -1:02X}")

            for offset in range(elements):
                element = unicast + offset
                for name, model in BIND_MODELS.items():
                    seq = await self.network.async_reserve_sequence(1)
                    payload = config_model_app_bind(element, self.network.app_key_index, model)
                    for pdu in codec.encode_device_access(unicast, payload, device_key, seq):
                        await transport.send(0, pdu)
                    # Every bind in this loop shares the same Config Model App
                    # Status opcode, so a slow/duplicate reply to an earlier
                    # bind must not be mistaken for this one's answer - only
                    # accept a response that echoes back this element+model.
                    expected_tail = element.to_bytes(2, "little") + model.to_bytes(2, "little")

                    def _matches_this_bind(response: bytes, _tail: bytes = expected_tail) -> bool:
                        return len(response) >= 9 and response[3:5] + response[7:9] == _tail

                    try:
                        response = await self._async_wait_device_status(
                            transport,
                            codec,
                            device_key,
                            unicast,
                            OP_CONFIG_MODEL_APP_STATUS,
                            timeout,
                            extra_check=_matches_this_bind,
                        )
                        bound = response[2] == 0x00
                    except ProtocolError:
                        bound = False
                    if offset == 0:
                        capabilities[name] = bound

        node["configured"] = any(capabilities.values())
        node["capabilities"] = capabilities
        await self.network.async_upsert_node(unicast, node)
        return capabilities

    async def _async_wait_device_status(
        self,
        transport: ble.MeshTransport,
        codec: MeshNetworkCodec,
        device_key: bytes,
        source: int,
        expected_opcode: bytes,
        timeout: float,
        extra_check: Any = None,
    ) -> bytes:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProtocolError(f"timeout waiting for Config Status {expected_opcode.hex()}")
            pdu_type, network_pdu = await asyncio.wait_for(transport.queue.get(), remaining)
            if pdu_type != 0:
                continue
            try:
                decoded = codec.decode_device_access(network_pdu, device_key)
            except ProtocolError:
                continue
            if (
                decoded.source == source
                and decoded.access_payload.startswith(expected_opcode)
                and (extra_check is None or extra_check(decoded.access_payload))
            ):
                return decoded.access_payload

    async def _async_wait_access_status(
        self, transport: ble.MeshTransport, codec: MeshNetworkCodec, source: int, expected_opcode: bytes, timeout: float
    ) -> bytes:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProtocolError(f"timeout waiting for Status {expected_opcode.hex()}")
            pdu_type, network_pdu = await asyncio.wait_for(transport.queue.get(), remaining)
            if pdu_type != 0:
                continue
            try:
                decoded = codec.decode_access(network_pdu)
            except ProtocolError:
                continue
            if decoded.source == source and decoded.access_payload.startswith(expected_opcode):
                return decoded.access_payload

    async def _async_allow_all_proxy_traffic(self, transport: ble.MeshTransport, codec: MeshNetworkCodec) -> None:
        seq = await self.network.async_reserve_sequence()
        # Empty reject list == the proxy forwards everything to us.
        await transport.send(2, codec.encode_proxy_configuration(bytes([0x00, 0x01]), seq))

    async def _async_retry_mesh(
        self,
        address: str,
        timeout: float,
        func: Any,
        attempts: int = MESH_RETRY_ATTEMPTS,
        delay: float = MESH_RETRY_DELAY,
    ) -> Any:
        """Run ``func(transport, codec)`` against a (possibly reused) Mesh Proxy connection.

        Connections are pooled per address (see ``_async_get_transport``) so
        consecutive commands/polls against the same lamp reuse an already
        open connection instead of paying the - by far dominant - BLE
        connect/service-discovery cost every time. Bluetooth-proxied
        connections can still fail transiently (weak RSSI, a proxy's
        connection slots briefly full, ...); a single dropped attempt
        shouldn't surface as a user-facing error, so the whole operation is
        retried a few times, forcing a fresh connection on each retry. Each
        attempt reserves fresh sequence numbers, so nothing is ever resent
        under a reused sequence number even if an earlier attempt's
        transmission secretly reached the node.
        """
        lock = self._transport_lock(address)
        async with lock:
            last_exc: ProtocolError | None = None
            for attempt in range(1, attempts + 1):
                try:
                    transport = await self._async_get_transport(address, timeout)
                    transport.drain()
                    codec = self.network.codec()
                    return await func(transport, codec)
                except ProtocolError as exc:
                    last_exc = exc
                    await self._async_close_transport(address)
                    if attempt < attempts:
                        _LOGGER.debug("Mesh operation on %s failed (attempt %d/%d): %s", address, attempt, attempts, exc)
                        await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # -- normal operation ----------------------------------------------

    async def async_set_onoff(self, unicast: int, address: str, on: bool, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> LightState:
        async def _do(transport: ble.MeshTransport, codec: MeshNetworkCodec) -> bytes:
            seq = await self.network.async_reserve_sequence()
            payload = onoff_set_payload(on, seq & 0xFF, unack=False)
            await transport.send(0, codec.encode_access(unicast, payload, seq))
            return await self._async_wait_access_status(transport, codec, unicast, _ONOFF_STATUS, timeout)

        status = await self._async_retry_mesh(address, timeout, _do)
        parsed = parse_onoff_status(status[2:])
        return LightState(onoff=parsed.present)

    async def async_set_lightness(
        self, unicast: int, address: str, lightness: int, timeout: float = DEFAULT_CONNECT_TIMEOUT
    ) -> LightState:
        async def _do(transport: ble.MeshTransport, codec: MeshNetworkCodec) -> bytes:
            seq = await self.network.async_reserve_sequence()
            payload = lightness_set_payload(lightness, seq & 0xFF, unack=False)
            await transport.send(0, codec.encode_access(unicast, payload, seq))
            return await self._async_wait_access_status(transport, codec, unicast, _LIGHTNESS_STATUS, timeout)

        status = await self._async_retry_mesh(address, timeout, _do)
        parsed = parse_lightness_status(status[2:])
        return LightState(lightness=parsed.present, onoff=parsed.present > 0)

    async def async_set_ctl(
        self,
        unicast: int,
        address: str,
        lightness: int,
        temperature: int,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> LightState:
        async def _do(transport: ble.MeshTransport, codec: MeshNetworkCodec) -> bytes:
            seq = await self.network.async_reserve_sequence()
            payload = ctl_set_payload(lightness, temperature, 0, seq & 0xFF, unack=False)
            await transport.send(0, codec.encode_access(unicast, payload, seq))
            return await self._async_wait_access_status(transport, codec, unicast, _CTL_STATUS, timeout)

        status = await self._async_retry_mesh(address, timeout, _do)
        parsed = parse_ctl_status(status[2:])
        return LightState(
            ctl_lightness=parsed.present_lightness, ctl_temperature=parsed.present_temperature, onoff=parsed.present_lightness > 0
        )

    async def async_set_hsl(
        self,
        unicast: int,
        address: str,
        lightness: int,
        hue: int,
        saturation: int,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> LightState:
        async def _do(transport: ble.MeshTransport, codec: MeshNetworkCodec) -> bytes:
            seq = await self.network.async_reserve_sequence()
            payload = hsl_set_payload(lightness, hue, saturation, seq & 0xFF, unack=False)
            await transport.send(0, codec.encode_access(unicast, payload, seq))
            return await self._async_wait_access_status(transport, codec, unicast, _HSL_STATUS, timeout)

        status = await self._async_retry_mesh(address, timeout, _do)
        parsed = parse_hsl_status(status[2:])
        return LightState(hsl_lightness=parsed.lightness, hue=parsed.hue, saturation=parsed.saturation, onoff=parsed.lightness > 0)

    async def async_get_status(
        self, unicast: int, address: str, capabilities: dict[str, bool], timeout: float = DEFAULT_CONNECT_TIMEOUT
    ) -> LightState:
        from .protocol import MESH_GET_OPCODES

        async def _do(transport: ble.MeshTransport, codec: MeshNetworkCodec) -> LightState:
            state = LightState()
            if capabilities.get(CAPABILITY_ONOFF):
                seq = await self.network.async_reserve_sequence()
                await transport.send(0, codec.encode_access(unicast, sig_opcode(MESH_GET_OPCODES["onoff"]), seq))
                status = await self._async_wait_access_status(transport, codec, unicast, _ONOFF_STATUS, timeout)
                state.onoff = parse_onoff_status(status[2:]).present
            if capabilities.get(CAPABILITY_LIGHTNESS):
                seq = await self.network.async_reserve_sequence()
                await transport.send(0, codec.encode_access(unicast, sig_opcode(MESH_GET_OPCODES["lightness"]), seq))
                status = await self._async_wait_access_status(transport, codec, unicast, _LIGHTNESS_STATUS, timeout)
                parsed = parse_lightness_status(status[2:])
                state.lightness = parsed.present
                if state.onoff is None:
                    state.onoff = parsed.present > 0
            if capabilities.get(CAPABILITY_CTL):
                seq = await self.network.async_reserve_sequence()
                await transport.send(0, codec.encode_access(unicast, sig_opcode(MESH_GET_OPCODES["ctl"]), seq))
                status = await self._async_wait_access_status(transport, codec, unicast, _CTL_STATUS, timeout)
                parsed = parse_ctl_status(status[2:])
                state.ctl_lightness, state.ctl_temperature = parsed.present_lightness, parsed.present_temperature
            if capabilities.get(CAPABILITY_HSL):
                seq = await self.network.async_reserve_sequence()
                await transport.send(0, codec.encode_access(unicast, sig_opcode(MESH_GET_OPCODES["hsl"]), seq))
                status = await self._async_wait_access_status(transport, codec, unicast, _HSL_STATUS, timeout)
                parsed = parse_hsl_status(status[2:])
                state.hsl_lightness, state.hue, state.saturation = parsed.lightness, parsed.hue, parsed.saturation
            return state

        return await self._async_retry_mesh(address, timeout, _do)

    # -- direct (proprietary) channel --------------------------------------

    async def async_identify(self, address: str, active: bool, duration: int = 10, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        from .protocol import identify_data

        async with ble.DirectClient(self.hass, address, timeout) as client:
            await client.command(0x17, identify_data(active, duration), timeout)

    async def async_factory_reset(self, unicast: int, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        """Global-reset a lamp (proprietary opcode 0xE5) and forget it locally.

        Only removes the node from our mesh store once the lamp has
        confirmed the reset (a non-OK status raises and nothing local
        changes) - the lamp physically erases its NetKey/AppKey/Device Key,
        so it needs to be re-provisioned (by this integration or anything
        else) before it can be used again.
        """
        from .protocol import GLOBAL_RESET_DATA, GLOBAL_RESET_OPCODE

        node = self.network.node_for_unicast(unicast)
        if node is None:
            raise ProtocolError(f"unknown node 0x{unicast:04X}")
        async with ble.DirectClient(self.hass, node["address"], timeout) as client:
            await client.command(GLOBAL_RESET_OPCODE, GLOBAL_RESET_DATA, timeout)
        await self._async_close_transport(node["address"])
        await self.network.async_remove_node(unicast)

    # -- firmware update (Nordic Secure DFU) --------------------------------

    async def async_firmware_update(
        self,
        address: str,
        package: Any,
        expected_version: str,
        expected_hardware: int,
        expected_product_id: int,
        progress_callback: Any = None,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        verify_timeout: float = 90.0,
    ) -> dict[str, Any]:
        """Verify and apply a signed Nordic Secure DFU package (see protocol.load_nordic_firmware_package).

        Only the fully validated Nordic Secure/Buttonless DFU path is used
        here; the still-experimental proprietary GATT firmware-update opcodes
        (0xE1/0xE3) are intentionally not automated, see STEINEL_BLE_TOOL.md.
        """
        from .protocol import (
            advertised_firmware_matches_catalog,
            increment_ble_address,
            parse_steinel_advertisement,
            semantic_firmware_number,
        )

        expected_number = semantic_firmware_number(expected_version)
        if package.hardware != expected_hardware:
            raise ProtocolError(f"firmware package is for hardware {package.hardware}, expected {expected_hardware}")
        if package.firmware_number != expected_number:
            raise ProtocolError(
                f"firmware package reports version value {package.firmware_number}, "
                f"{expected_version} corresponds to {expected_number}"
            )

        info = await ble.async_wait_for_manufacturer_data(self.hass, {address}, STEINEL_COMPANY_ID, timeout)
        manufacturer = info.manufacturer_data.get(STEINEL_COMPANY_ID)
        identity = parse_steinel_advertisement(manufacturer, info.name) if manufacturer else {}
        if identity.get("product_id") != expected_product_id:
            raise ProtocolError(f"device is product id {identity.get('product_id')}, expected {expected_product_id}")
        if identity.get("hardware") != expected_hardware:
            raise ProtocolError(f"device is hardware {identity.get('hardware')}, expected {expected_hardware}")
        if advertised_firmware_matches_catalog(identity.get("firmware", ""), expected_version):
            return {"status": "already_up_to_date", **identity}

        await ble.async_enter_nordic_bootloader(self.hass, address, timeout)
        bootloader_device = await ble.async_wait_for_service(
            self.hass, {address, increment_ble_address(address)}, NORDIC_DFU_SERVICE, timeout
        )

        client = await ble.async_connect(self.hass, bootloader_device.address, "steinel-dfu", max(20.0, timeout))
        try:
            dfu = ble.NordicDfuClient(client, timeout=max(20.0, timeout))
            dfu.on_progress = progress_callback
            await dfu.start()
            await dfu.command(b"\x02\x00\x00")  # disable packet-receipt notifications; per-object CRC stays on
            await dfu.transfer_command_object(package.init_packet)
            await dfu.transfer_data_objects(package.image)
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

        updated_info = await ble.async_wait_for_manufacturer_data(self.hass, {address}, STEINEL_COMPANY_ID, verify_timeout)
        updated_manufacturer = updated_info.manufacturer_data.get(STEINEL_COMPANY_ID)
        if updated_manufacturer is None:
            raise ProtocolError("the lamp restarted but is not sending verifiable STEINEL manufacturer data")
        updated = parse_steinel_advertisement(updated_manufacturer, updated_info.name)
        if not advertised_firmware_matches_catalog(updated.get("firmware", ""), expected_version):
            raise ProtocolError(
                f"update finished, but advertising reports firmware {updated.get('firmware')} instead of {expected_version}"
            )
        return {"status": "updated", **updated}


class SteinelLightCoordinator(DataUpdateCoordinator[LightState]):
    """Polls one lamp's Mesh model state over a short-lived GATT connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub: SteinelMeshHub,
        unicast: int,
        address: str,
        name: str,
        capabilities: dict[str, bool],
        update_interval: int = DEFAULT_UPDATE_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"steinel_ble-{name}",
            update_interval=timedelta(seconds=update_interval),
        )
        self.hub = hub
        self.unicast = unicast
        self.address = address
        self.capabilities = capabilities

    async def _async_update_data(self) -> LightState:
        try:
            return await self.hub.async_get_status(self.unicast, self.address, self.capabilities)
        except ProtocolError as exc:
            raise UpdateFailed(str(exc)) from exc

    async def async_apply(self, state: LightState) -> None:
        """Push a state obtained from a command response into the coordinator."""
        merged = self.data or LightState()
        for attr in ("onoff", "lightness", "ctl_lightness", "ctl_temperature", "hsl_lightness", "hue", "saturation"):
            value = getattr(state, attr)
            if value is not None:
                setattr(merged, attr, value)
        merged.available = True
        merged.last_seen = time.monotonic()
        self.async_set_updated_data(merged)
