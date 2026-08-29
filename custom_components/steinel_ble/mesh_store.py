"""Persistent Bluetooth Mesh network state for one STEINEL mesh hub.

Mirrors the CLI's file-backed ``MeshConfig`` (net key, app key, IV index,
monotonic sequence counter, provisioner source address, per-node device
keys) but backed by Home Assistant's ``Store`` helper instead of a raw JSON
file, and with an async, lock-guarded sequence reservation so several
entities can safely share one provisioner identity concurrently.

The sequence number is persisted *before* it is used on the radio, exactly
like the CLI tool, so an interrupted write never causes a sequence reuse
(which nodes would otherwise silently drop as a replay).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEFAULT_TTL, DOMAIN
from .protocol import MeshNetworkCodec, ProtocolError

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_network"


class MeshNetwork:
    """Loads, mutates and persists the shared mesh network state.

    This integration only ever supports a single STEINEL mesh hub per Home
    Assistant instance (see the single-instance guard in ``config_flow.py``),
    so the store uses one fixed key rather than being keyed by config entry
    id. That also lets the config flow provision real devices - reserving
    real sequence numbers against the real, persisted network identity -
    while the wizard is still running, before the config entry exists.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self.net_key: bytes = b""
        self.app_key: bytes = b""
        self.iv_index: int = 0
        self.sequence: int = 0
        self.source: int = 0x0001
        self.ttl: int = DEFAULT_TTL
        self.net_key_index: int = 0
        self.app_key_index: int = 0
        self.nodes: dict[str, dict[str, Any]] = {}
        self._loaded = False

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def async_load(self) -> None:
        """Load existing state, or create a fresh random network on first run."""
        data = await self._store.async_load()
        if data is None:
            self.net_key = secrets.token_bytes(16)
            self.app_key = secrets.token_bytes(16)
            self.iv_index = 0
            self.sequence = 0
            self.source = 0x0001
            self.ttl = DEFAULT_TTL
            self.net_key_index = 0
            self.app_key_index = 0
            self.nodes = {}
            self._loaded = True
            await self.async_save()
            _LOGGER.debug("Created a new STEINEL mesh network")
            return
        self.net_key = bytes.fromhex(data["net_key"])
        self.app_key = bytes.fromhex(data["app_key"])
        self.iv_index = int(data["iv_index"])
        self.sequence = int(data["sequence"])
        self.source = int(data["source"])
        self.ttl = int(data.get("ttl", DEFAULT_TTL))
        self.net_key_index = int(data.get("net_key_index", 0))
        self.app_key_index = int(data.get("app_key_index", 0))
        self.nodes = dict(data.get("nodes", {}))
        self._loaded = True

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "net_key": self.net_key.hex(),
                "app_key": self.app_key.hex(),
                "iv_index": self.iv_index,
                "sequence": self.sequence,
                "source": self.source,
                "ttl": self.ttl,
                "net_key_index": self.net_key_index,
                "app_key_index": self.app_key_index,
                "nodes": self.nodes,
            }
        )

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def codec(self) -> MeshNetworkCodec:
        if not self._loaded:
            raise RuntimeError("MeshNetwork.async_load() was not awaited yet")
        return MeshNetworkCodec(self.net_key, self.app_key, self.source, self.iv_index, self.ttl)

    async def async_reserve_sequence(self, count: int = 1) -> int:
        """Reserve ``count`` consecutive sequence numbers and persist immediately."""
        if count < 1:
            raise ValueError("count must be at least 1")
        async with self._lock:
            if self.sequence + count - 1 > 0xFFFFFF:
                raise ProtocolError("24-bit sequence number space exhausted; a key refresh is required")
            first = self.sequence
            self.sequence += count
            await self.async_save()
            return first

    def node_for_unicast(self, unicast: int) -> dict[str, Any] | None:
        return self.nodes.get(f"0x{unicast:04X}")

    def node_for_address(self, address: str) -> tuple[int, dict[str, Any]] | None:
        wanted = address.upper()
        for key, node in self.nodes.items():
            if str(node.get("address", "")).upper() == wanted:
                return int(key, 16), node
        return None

    async def async_upsert_node(self, unicast: int, node: dict[str, Any]) -> None:
        async with self._lock:
            self.nodes[f"0x{unicast:04X}"] = node
            await self.async_save()

    async def async_remove_node(self, unicast: int) -> None:
        async with self._lock:
            self.nodes.pop(f"0x{unicast:04X}", None)
            await self.async_save()
