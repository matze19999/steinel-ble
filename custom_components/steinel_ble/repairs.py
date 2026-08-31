"""Repair flows for recoverable STEINEL setup failures."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


class ResetRepairFlow(RepairsFlow):
    """Guide the user through the short power-on reset window."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Wait until the lamp has just been power-cycled, then retry."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_missing")
        if user_input is not None:
            ir.async_delete_issue(self.hass, DOMAIN, f"reset_{self._entry_id}")
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self._entry_id),
                f"steinel_ble repair reload {self._entry_id}",
            )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            description_placeholders={"name": entry.title},
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    _data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the repair flow belonging to an issue."""
    if issue_id.startswith("reset_"):
        return ResetRepairFlow(issue_id.removeprefix("reset_"))
    return ConfirmRepairFlow()
