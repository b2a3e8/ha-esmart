"""The eSMART integration."""

from __future__ import annotations

import asyncio
from functools import partial
import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .client import CannotConnect, InvalidAuth, async_remove_account, construct_off_loop
from .const import CONF_PASSWORD, CONF_USERNAME, DOMAIN, ISSUE_ROOM_ACCESS
from .coordinator import ESmartConfigEntry, ESmartHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.LIGHT,
    Platform.SCENE,
    Platform.SENSOR,
]

# Deleting the account on removal is best effort; don't hold the UI up for long.
REMOVE_ACCOUNT_TIMEOUT_SECONDS = 45.0


async def async_setup_entry(hass: HomeAssistant, entry: ESmartConfigEntry) -> bool:
    """Set up eSMART from a config entry."""
    # ESmartClient's constructor builds an SSL context (reads the system CA
    # store) — blocking I/O — so build the hub off the event loop.
    hub = await hass.async_add_executor_job(
        construct_off_loop, partial(ESmartHub, hass, entry)
    )
    try:
        await hub.async_start()
    except InvalidAuth as err:
        await hub.async_stop()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except (CannotConnect, TimeoutError, OSError) as err:
        # Stop the client, or it would keep reconnecting in the background
        # next to the one Home Assistant creates on its next retry.
        await hub.async_stop()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    except BaseException:
        await hub.async_stop()
        raise

    entry.runtime_data = hub

    # Register the controller "bridge" device that every node hangs off of.
    controller = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="eSMART",
        name="eSMART Controller",
        model="eSMART Home",
    )
    hub.controller_device_id = controller.id

    async def _async_stop(_event: Event) -> None:
        # Close the session cleanly so the server doesn't keep a stale one.
        await hub.async_stop()

    # A plain listener: a one-time one can't be removed after it fired.
    entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ESmartConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ESmartConfigEntry) -> None:
    """Delete this remote's account from the eSMART server.

    Best effort: the integration is removed either way. The wall unit may keep
    listing the remote until it is deleted there.
    """
    for issue_id in [
        issue_id
        for (domain, issue_id) in ir.async_get(hass).issues
        if domain == DOMAIN and issue_id.startswith(ISSUE_ROOM_ACCESS)
    ]:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    try:
        async with asyncio.timeout(REMOVE_ACCOUNT_TIMEOUT_SECONDS):
            await async_remove_account(
                entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
            )
    except Exception as err:  # noqa: BLE001 - removal must never fail on this
        _LOGGER.warning(
            "Could not delete this remote's account from the eSMART server (%s). "
            "You can delete the remote on the wall unit",
            err,
        )


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ESmartConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device the wall unit no longer reports."""
    return not (device.identifiers & entry.runtime_data.current_device_identifiers())
