"""Tests for pairing, re-pairing and their error handling."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.esmart.client import CannotConnect, PairingTimeout
from custom_components.esmart.const import (
    CONF_NICKNAME,
    CONF_PAIRING_CODE,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

from .conftest import INSTALLATION, PASSWORD, ROOM, USERNAME, FakeClient

OTHER_ROOM = "00-11-22-33-44-55-99@conference.myesmart.net"


class FlowClient:
    """Stands in for the client the flow pairs with."""

    instances: list[FlowClient] = []
    rooms: list[str] = [ROOM]
    pair_error: BaseException | None = None
    connect_error: BaseException | None = None

    def __init__(self, username: str, password: str, nickname: str, **_: Any) -> None:
        self.username = username
        self.nickname = nickname
        self.session_ready = False
        self.paired_code: str | None = None
        self.unregistered = False
        self.disconnected = False
        FlowClient.instances.append(self)

    async def async_connect(self) -> None:
        if FlowClient.connect_error is not None:
            raise FlowClient.connect_error
        self.session_ready = True

    async def async_pair(self, code: str) -> list[str]:
        self.paired_code = code
        if FlowClient.pair_error is not None:
            raise FlowClient.pair_error
        return list(FlowClient.rooms)

    async def async_unregister(self) -> None:
        self.unregistered = True

    async def async_disconnect(self) -> None:
        self.disconnected = True
        self.session_ready = False


@pytest.fixture
def flow_client() -> Generator[type[FlowClient]]:
    FlowClient.instances = []
    FlowClient.rooms = [ROOM]
    FlowClient.pair_error = None
    FlowClient.connect_error = None
    with patch("custom_components.esmart.config_flow.ESmartClient", FlowClient):
        yield FlowClient


@pytest.fixture
def register() -> Generator[AsyncMock]:
    with patch(
        "custom_components.esmart.config_flow.async_register_account", AsyncMock()
    ) as mock:
        yield mock


@pytest.fixture
def remove_account() -> Generator[AsyncMock]:
    with patch(
        "custom_components.esmart.config_flow.async_remove_account", AsyncMock()
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def no_real_setup(fake_client: type[FakeClient]) -> None:
    """Entries created by the flow set up against the fake hub client."""


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_pair_new_installation(
    hass: HomeAssistant, flow_client: type[FlowClient], register: AsyncMock
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: " 6366 ", CONF_NICKNAME: "Home"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == INSTALLATION
    client = flow_client.instances[0]
    assert client.paired_code == "6366"
    assert client.disconnected
    assert not client.unregistered
    assert entry.data == {
        CONF_USERNAME: client.username,
        CONF_PASSWORD: register.await_args.args[1],
        CONF_NICKNAME: "Home",
    }
    assert client.username.startswith("i-")


async def test_invalid_code_is_rejected_before_connecting(
    hass: HomeAssistant, flow_client: type[FlowClient], register: AsyncMock
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "12a4"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PAIRING_CODE: "invalid_code"}
    register.assert_not_awaited()


@pytest.mark.parametrize(
    ("where", "error", "reason"),
    [
        ("register", CannotConnect("down"), "cannot_connect"),
        ("register", TimeoutError(), "cannot_connect"),
        ("connect", OSError(), "cannot_connect"),
        ("pair", PairingTimeout(), "pairing_timeout"),
        ("pair", RuntimeError("boom"), "unknown"),
    ],
)
async def test_pairing_errors(
    hass: HomeAssistant,
    flow_client: type[FlowClient],
    register: AsyncMock,
    where: str,
    error: Exception,
    reason: str,
) -> None:
    if where == "register":
        register.side_effect = error
    elif where == "connect":
        flow_client.connect_error = error
    else:
        flow_client.pair_error = error
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    # Recovers once the problem is gone.
    register.side_effect = None
    flow_client.connect_error = flow_client.pair_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_failed_pairing_deletes_the_new_account(
    hass: HomeAssistant, flow_client: type[FlowClient], register: AsyncMock
) -> None:
    """Regression: each failed attempt left an orphan account on the server."""
    flow_client.pair_error = PairingTimeout()
    result = await _start(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "1234"}
    )
    client = flow_client.instances[0]
    assert client.unregistered
    assert client.disconnected


async def test_same_installation_twice_is_refused(
    hass: HomeAssistant,
    flow_client: type[FlowClient],
    register: AsyncMock,
    remove_account: AsyncMock,
    config_entry: MockConfigEntry,
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    remove_account.assert_awaited_once()


async def test_roster_not_ready_falls_back_to_username(
    hass: HomeAssistant, flow_client: type[FlowClient], register: AsyncMock
) -> None:
    flow_client.rooms = []
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == flow_client.instances[0].username


async def _repair(
    hass: HomeAssistant, entry: MockConfigEntry, step: str
) -> dict[str, Any]:
    if step == "reauth_confirm":
        result = await entry.start_reauth_flow(hass)
    else:
        result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "4321"}
    )


@pytest.mark.parametrize("step", ["reauth_confirm", "reconfigure"])
async def test_pair_again(
    hass: HomeAssistant,
    flow_client: type[FlowClient],
    register: AsyncMock,
    remove_account: AsyncMock,
    config_entry: MockConfigEntry,
    step: str,
) -> None:
    config_entry.add_to_hass(hass)
    result = await _repair(hass, config_entry, step)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == (
        "reauth_successful" if step == "reauth_confirm" else "reconfigure_successful"
    )
    new_client = flow_client.instances[0]
    assert config_entry.data[CONF_USERNAME] == new_client.username
    assert config_entry.data[CONF_NICKNAME] == "Home Assistant"
    # The replaced account is deleted.
    remove_account.assert_awaited_once_with(USERNAME, PASSWORD)


@pytest.mark.parametrize("step", ["reauth_confirm", "reconfigure"])
async def test_pair_again_with_another_installation_is_refused(
    hass: HomeAssistant,
    flow_client: type[FlowClient],
    register: AsyncMock,
    remove_account: AsyncMock,
    config_entry: MockConfigEntry,
    step: str,
) -> None:
    flow_client.rooms = [OTHER_ROOM]
    config_entry.add_to_hass(hass)
    result = await _repair(hass, config_entry, step)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_installation"
    assert config_entry.data[CONF_USERNAME] == USERNAME
    # The account made for the attempt is deleted again; the old one stays.
    remove_account.assert_awaited_once_with(
        flow_client.instances[0].username, register.await_args.args[1]
    )


async def test_reauth_migrates_username_entry(
    hass: HomeAssistant,
    flow_client: type[FlowClient],
    register: AsyncMock,
    remove_account: AsyncMock,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )
    entry.add_to_hass(hass)
    result = await _repair(hass, entry, "reauth_confirm")
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == INSTALLATION


async def test_reconfigure_keeps_errors_on_form(
    hass: HomeAssistant,
    flow_client: type[FlowClient],
    register: AsyncMock,
    config_entry: MockConfigEntry,
) -> None:
    flow_client.pair_error = PairingTimeout()
    config_entry.add_to_hass(hass)
    result = await _repair(hass, config_entry, "reconfigure")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "pairing_timeout"}
