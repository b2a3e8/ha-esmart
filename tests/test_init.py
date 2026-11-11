"""Tests for setting up, unloading and removing the integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.esmart import async_remove_config_entry_device
from custom_components.esmart.client import CannotConnect, InvalidAuth
from custom_components.esmart.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    ISSUE_ROOM_ACCESS,
)
from custom_components.esmart.diagnostics import (
    async_get_config_entry_diagnostics,
    redact_structure,
)

from .conftest import (
    INSTALLATION,
    PASSWORD,
    ROOM,
    USERNAME,
    FakeClient,
    load_fixture,
    setup_integration,
)


def _reauth_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


async def test_setup_and_unload(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    client = await setup_integration(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert client.username == USERNAME
    assert client.nickname == "Home Assistant"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert client.disconnected


@pytest.mark.parametrize("error", [CannotConnect("down"), TimeoutError(), OSError()])
async def test_setup_retries_when_unreachable(
    hass: HomeAssistant,
    fake_client: type[FakeClient],
    config_entry: MockConfigEntry,
    error: Exception,
) -> None:
    """Regression: a failed setup left its client reconnecting in the background."""
    fake_client.connect_error = error
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_client.instances[-1].disconnected


async def test_setup_with_rejected_credentials_starts_reauth(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    fake_client.connect_error = InvalidAuth("nope")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_client.instances[-1].disconnected
    assert len(_reauth_flows(hass)) == 1


async def test_rejected_credentials_at_runtime_start_reauth(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    """Regression: a deleted remote used to retry its login forever."""
    client = await setup_integration(hass, config_entry)
    client.on_auth_failed()
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1


async def test_replaced_credentials_dont_start_reauth(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    """The old account is deleted on purpose after re-pairing."""
    client = await setup_integration(hass, config_entry)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_USERNAME: "i-new", CONF_PASSWORD: "x"},
    )
    client.on_auth_failed()
    await hass.async_block_till_done()
    assert not _reauth_flows(hass)


async def test_home_assistant_stop_disconnects(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    client = await setup_integration(hass, config_entry)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert client.disconnected


async def test_remove_entry_deletes_account(
    hass: HomeAssistant,
    fake_client: type[FakeClient],
    config_entry: MockConfigEntry,
    mock_remove_account: Any,
) -> None:
    await setup_integration(hass, config_entry)
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    mock_remove_account.assert_awaited_once_with(USERNAME, PASSWORD)


async def test_remove_entry_survives_account_errors(
    hass: HomeAssistant,
    fake_client: type[FakeClient],
    config_entry: MockConfigEntry,
    mock_remove_account: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_remove_account.side_effect = CannotConnect("down")
    await setup_integration(hass, config_entry)
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    assert "Could not delete this remote's account" in caplog.text


async def test_username_unique_id_is_migrated(
    hass: HomeAssistant, fake_client: type[FakeClient]
) -> None:
    """Pairing before the roster listed the wall unit leaves the username as ID."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    await setup_integration(hass, entry)
    assert entry.unique_id == INSTALLATION


async def test_username_unique_id_kept_on_duplicate(
    hass: HomeAssistant,
    fake_client: type[FakeClient],
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_integration(hass, config_entry)
    provisional = MockConfigEntry(
        domain=DOMAIN,
        unique_id="i-zyxwvutsrqpon-999",
        data={CONF_USERNAME: "i-zyxwvutsrqpon-999", CONF_PASSWORD: PASSWORD},
    )
    await setup_integration(hass, provisional)
    assert provisional.unique_id == "i-zyxwvutsrqpon-999"
    assert "set up twice" in caplog.text


async def test_room_access_issue(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    client = await setup_integration(hass, config_entry, replay=False)
    issues = ir.async_get(hass)
    issue_id = f"{ISSUE_ROOM_ACCESS}_{INSTALLATION}"

    client.on_room_denied(ROOM)
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    client.join()
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_remove_device(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    await setup_integration(hass, config_entry)
    devices = dr.async_get(hass)
    current = next(
        device
        for device in dr.async_entries_for_config_entry(devices, config_entry.entry_id)
        if device.name == "Ceiling light"
    )
    stale = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"{ROOM}_9999")},
        name="Old light",
    )
    assert not await async_remove_config_entry_device(hass, config_entry, current)
    assert await async_remove_config_entry_device(hass, config_entry, stale)


async def test_diagnostics_are_redacted(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    await setup_integration(hass, config_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    dumped = repr(diagnostics)
    for secret in (
        USERNAME,
        PASSWORD,
        INSTALLATION,
        "00:11:22:33:44:55",
        "Jane Example",
        "B1-F2-03",
    ):
        assert secret not in dumped
    assert diagnostics["connected"] is True
    assert len(diagnostics["nodes"]) == 7
    # Unsupported nodes show up in the raw structure, for support requests.
    assert "Ventilation" in dumped


def test_redact_structure_keeps_room_names() -> None:
    redacted = redact_structure(load_fixture("structure.json"))
    rooms = redacted["apartment"]["floors"][0]["rooms"]
    assert [room["name"] for room in rooms] == ["Living room", "Bedroom"]
    assert redacted["apartment"]["tenant_name"] == "**REDACTED**"
