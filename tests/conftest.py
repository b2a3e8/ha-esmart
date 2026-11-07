"""Shared fixtures for the eSMART tests."""

from __future__ import annotations

from collections.abc import Generator
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.esmart.client import NotConnected
from custom_components.esmart.const import (
    CONF_NICKNAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"

INSTALLATION = "00-11-22-33-44-55-01"
ROOM = f"{INSTALLATION}@conference.myesmart.net"
USERNAME = "i-abcdefghijklm-123"
PASSWORD = "secretsecretsecret00"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/esmart."""


class FakeClient:
    """Stands in for ESmartClient: records commands, replays messages."""

    instances: list[FakeClient] = []
    connect_error: BaseException | None = None

    def __init__(
        self,
        username: str,
        password: str,
        nickname: str,
        *,
        on_message: Any,
        on_connection: Any,
        on_auth_failed: Any = None,
        on_room_joined: Any = None,
        on_room_denied: Any = None,
        join_rooms: bool = True,
    ) -> None:
        self.username = username
        self.password = password
        self.nickname = nickname
        self.on_message = on_message
        self.on_connection = on_connection
        self.on_auth_failed = on_auth_failed
        self.on_room_joined = on_room_joined
        self.on_room_denied = on_room_denied
        self.session_ready = False
        self.rooms: set[str] = set()
        self.commands: list[tuple[Any, ...]] = []
        self.disconnected = False
        FakeClient.instances.append(self)

    async def async_connect(self) -> None:
        if FakeClient.connect_error is not None:
            raise FakeClient.connect_error
        self.session_ready = True
        self.on_connection(True)

    async def async_disconnect(self) -> None:
        self.disconnected = True
        self.session_ready = False

    # -- test helpers ------------------------------------------------------

    def join(self, room: str = ROOM) -> None:
        self.rooms.add(room)
        if self.on_room_joined is not None:
            self.on_room_joined(room)

    def send(self, msg_type: str, body: Any, room: str = ROOM) -> None:
        self.on_message("INFO", msg_type, room, body)

    def send_all(self, room: str = ROOM) -> None:
        """Replay a full initial state, like the wall unit after a join."""
        self.send("config", load_fixture("config.json"), room)
        self.send("structure", load_fixture("structure.json"), room)
        self.send("data", load_fixture("data.json"), room)
        self.send("doors", load_fixture("doors.json"), room)
        self.send("scene", load_fixture("scenes.json"), room)

    def drop(self) -> None:
        self.session_ready = False
        self.on_connection(False)

    # -- commands ----------------------------------------------------------

    def _record(self, *command: Any) -> None:
        if not self.session_ready:
            raise NotConnected("Not connected to eSMART")
        self.commands.append(command)

    def set_onoff(self, room: str, node_id: int, on: bool) -> None:
        self._record("onoff", node_id, on)

    def set_dimming(self, room: str, node_id: int, percent: int) -> None:
        self._record("dimming", node_id, percent)

    def set_position(
        self, room: str, node_id: int, position: int, orientation: int | None = None
    ) -> None:
        self._record("position", node_id, position, orientation)

    def set_orientation(
        self, room: str, node_id: int, orientation: int, position: int | None = None
    ) -> None:
        self._record("orientation", node_id, orientation, position)

    def set_setpoint(self, room: str, node_id: int, temp: float) -> None:
        self._record("setpoint", node_id, temp)

    def stop_node(self, room: str, node_id: int) -> None:
        self._record("stop", node_id)

    def door_pulse(self, room: str, door_id: int) -> None:
        self._record("door", door_id)

    def activate_scene(self, room: str, num: int, version: str) -> None:
        self._record("scene", num, version)


@pytest.fixture
def fake_client() -> Generator[type[FakeClient]]:
    """Replace the XMPP client the hub creates."""
    FakeClient.instances = []
    FakeClient.connect_error = None
    with patch("custom_components.esmart.coordinator.ESmartClient", FakeClient):
        yield FakeClient


@pytest.fixture
def mock_remove_account() -> Generator[Any]:
    with patch(
        "custom_components.esmart.async_remove_account", autospec=True
    ) as remove:
        yield remove


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="eSMART",
        unique_id=INSTALLATION,
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_NICKNAME: "Home Assistant",
        },
    )


async def setup_integration(
    hass: HomeAssistant, entry: MockConfigEntry, *, replay: bool = True
) -> FakeClient:
    """Set up the entry and, by default, replay a full initial state."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = FakeClient.instances[-1]
    if replay:
        client.join()
        client.send_all()
        await hass.async_block_till_done()
    return client
