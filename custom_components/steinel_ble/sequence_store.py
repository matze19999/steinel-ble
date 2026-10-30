"""Reserve Mesh sequence numbers on disk before any packet uses them."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
_BLOCK = 4096
_RECOVERY_GAP = 65536
_LIMIT = 0xFFFFFF  # Preserve the original allocator's exclusive upper limit.


class SequenceStore:
    """One allocator per config entry; unused reservations are lost on restart.

    The journal contains no keys. Keep it with the HA configuration/backups.
    An old backup or deleted journal cannot guarantee replay-safe recovery.
    """

    def __init__(self, hass, entry_id: str, saved_sequence: int) -> None:
        self._hass = hass
        self._path = Path(
            hass.config.path(".storage", f"steinel_ble.sequence.{entry_id}.json")
        )
        self._floor = saved_sequence
        self._next: int | None = None
        self._end = 0
        self._lock = asyncio.Lock()
        self._failed = False

    async def async_next(self) -> int:
        """Do not release a number until its reservation has been flushed."""
        async with self._lock:
            if self._failed:
                raise ConnectionError(
                    "Mesh sequence journal failed; "
                    "reload integration after fixing storage"
                )
            if self._next is None or self._next >= self._end:
                task = asyncio.ensure_future(
                    self._hass.async_add_executor_job(self._reserve)
                )
                try:
                    start, end, migrated = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # The executor keeps running: wait before releasing the lock.
                    self._failed = True
                    with suppress(Exception):
                        await task
                    raise
                except Exception as err:
                    self._failed = True
                    raise ConnectionError(
                        "Cannot safely reserve Mesh sequence numbers"
                    ) from err
                self._next, self._end = start, end
                if migrated:
                    _LOGGER.warning(
                        "Mesh sequence recovery: stored=%s, start=%s, "
                        "reserved_until=%s; "
                        "one-time recovery gap applied "
                        "(not proof of the timeout cause)",
                        self._floor,
                        start,
                        end,
                    )
                else:
                    _LOGGER.debug(
                        "Mesh sequence reservation: start=%s, end=%s", start, end
                    )
            value = self._next
            self._next += 1
            return value

    def _reserve(self) -> tuple[int, int, bool]:
        """Atomic replacement plus file/directory fsync on HA's Linux host."""
        migrated = False
        if self._next is None:
            if type(self._floor) is not int or not 0 <= self._floor <= _LIMIT:
                raise ValueError("Invalid stored Mesh sequence")
            try:
                with self._path.open(encoding="utf-8") as stream:
                    data = json.load(stream)
            except FileNotFoundError:
                start = self._floor + _RECOVERY_GAP
                migrated = True
            else:
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("Invalid Mesh sequence journal version")
                end = data.get("reserved_until")
                if type(end) is not int or not 0 <= end <= _LIMIT:
                    raise ValueError("Invalid Mesh sequence journal boundary")
                start = max(self._floor, end)
        else:
            start = self._next
        if start >= _LIMIT:
            raise ValueError(
                "Bluetooth Mesh sequence space exhausted; IV Update is not implemented"
            )
        end = min(start + _BLOCK, _LIMIT)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self._path.name + ".", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "reserved_until": end}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path)
            if os.name == "posix":
                directory = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return start, end, migrated
