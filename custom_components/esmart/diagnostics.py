"""Diagnostics support for eSMART."""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD, CONF_USERNAME
from .coordinator import ESmartConfigEntry

# Keys that identify the account, the installation or the tenant.
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "room_jid",
    "jid",
    "tenant_name",
    "zip_code",
    "meteo_zip_code",
    "location",
    "address",
}


def redact_structure(body: Any) -> Any:
    """Redact a raw structure while keeping what is needed to add device support.

    Room and node names stay: they are needed to match a report to the setup.
    The apartment's own ID and name are its MAC address, and floor names can be
    a building or apartment number, so those go.
    """
    body = copy.deepcopy(body)
    apartment = body.get("apartment") if isinstance(body, dict) else None
    if isinstance(apartment, dict):
        for key in ("id", "name"):
            if key in apartment:
                apartment[key] = REDACTED
        for floor in apartment.get("floors") or []:
            if isinstance(floor, dict) and "name" in floor:
                floor["name"] = REDACTED
    return async_redact_data(body, TO_REDACT)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ESmartConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub = entry.runtime_data
    client = hub.client
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "connected": hub.connected,
        "session_ready": client.session_ready,
        "joined_rooms": len(client.rooms),
        "setpoint_limits": [
            asdict(hub.setpoint_limits(room_jid))
            for room_jid in sorted({node.room_jid for node in hub.nodes.values()})
        ],
        "nodes": [
            async_redact_data(asdict(node), TO_REDACT) for node in hub.nodes.values()
        ],
        "doors": [
            async_redact_data(asdict(door), TO_REDACT) for door in hub.doors.values()
        ],
        "scenes": [
            async_redact_data(asdict(scene), TO_REDACT) for scene in hub.scenes.values()
        ],
        "meters": [
            async_redact_data(asdict(meter), TO_REDACT) for meter in hub.meters.values()
        ],
        # The raw structure shows nodes this integration doesn't support yet.
        "structure": [redact_structure(body) for body in hub.raw_structure.values()],
    }
