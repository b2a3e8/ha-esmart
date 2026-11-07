"""Tests for the XMPP client, with slixmpp's networking stubbed out."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from functools import partial
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest
from slixmpp.exceptions import PresenceError

from custom_components.esmart import client as client_mod
from custom_components.esmart.client import (
    CannotConnect,
    ESmartClient,
    InvalidAuth,
    NotConnected,
    async_register_account,
    construct_off_loop,
)
from custom_components.esmart.const import XMPP_HOST, XMPP_PORT

from .conftest import PASSWORD, ROOM, USERNAME

DEVICE = ROOM.replace("@conference.", "@")


class Callbacks:
    def __init__(self) -> None:
        self.messages: list[tuple[Any, ...]] = []
        self.connection: list[bool] = []
        self.auth_failed = MagicMock()
        self.room_joined = MagicMock()
        self.room_denied = MagicMock()


@pytest.fixture
def callbacks() -> Callbacks:
    return Callbacks()


@pytest.fixture
async def client(
    hass: HomeAssistant, callbacks: Callbacks
) -> AsyncGenerator[ESmartClient]:
    """A real client, never connected to a network."""
    xmpp: ESmartClient = await hass.async_add_executor_job(
        construct_off_loop,
        partial(
            ESmartClient,
            username=USERNAME,
            password=PASSWORD,
            nickname="Home Assistant",
            on_message=lambda *args: callbacks.messages.append(args),
            on_connection=callbacks.connection.append,
            on_auth_failed=callbacks.auth_failed,
            on_room_joined=callbacks.room_joined,
            on_room_denied=callbacks.room_denied,
        ),
    )
    client_mod.bind_to_running_loop(xmpp)
    yield xmpp
    await xmpp.async_disconnect()


def _make_live(xmpp: ESmartClient) -> list[dict[str, Any]]:
    """Pretend a session is up; returns the JSON payloads that get sent."""
    sent: list[dict[str, Any]] = []
    xmpp._session_ready.set()
    xmpp.is_connected = lambda: True  # type: ignore[method-assign]

    def _send(mto: str, mbody: str, mtype: str) -> None:
        sent.append(json.loads(mbody))

    xmpp.send_message = _send  # type: ignore[method-assign]
    return sent


def _presence_error(xmpp: ESmartClient, condition: str) -> PresenceError:
    presence = xmpp.Presence()
    presence["type"] = "error"
    presence["error"]["condition"] = condition
    return PresenceError(presence)


# -- registration ------------------------------------------------------------


async def test_register_runs_on_the_running_loop(hass: HomeAssistant) -> None:
    """Regression: the registration client was bound to a closed loop.

    connect() then raised "RuntimeError: Event loop is closed", so no new
    installation could be set up.
    """
    seen: dict[str, Any] = {}

    def fake_connect(self: Any, host: str, port: int) -> None:
        seen["loop_ok"] = self.loop is asyncio.get_running_loop()
        seen["address"] = (host, port)
        seen["direct_tls"] = self.enable_direct_tls
        self.event("connection_failed", "refused")

    with (
        patch.object(client_mod._RegisterXMPP, "connect", fake_connect),
        pytest.raises(CannotConnect),
    ):
        await async_register_account(USERNAME, PASSWORD)
    assert seen == {
        "loop_ok": True,
        "address": (XMPP_HOST, XMPP_PORT),
        "direct_tls": False,
    }


# -- connecting --------------------------------------------------------------


async def test_connect_success(client: ESmartClient, callbacks: Callbacks) -> None:
    def fake_connect(host: str, port: int) -> None:
        client._session_ready.set()

    client.connect = fake_connect  # type: ignore[method-assign]
    await client.async_connect()
    assert client.loop is asyncio.get_running_loop()


async def test_connect_failure_stops_background_retries(client: ESmartClient) -> None:
    client.connect = lambda host, port: client.event("connection_failed", "boom")  # type: ignore[method-assign]
    with (
        patch.object(client, "cancel_connection_attempt") as cancel,
        pytest.raises(CannotConnect),
    ):
        await client.async_connect()
    cancel.assert_called()


async def test_connect_timeout(client: ESmartClient) -> None:
    client.connect = lambda host, port: None  # type: ignore[method-assign]
    with (
        patch.object(client_mod, "CONNECT_TIMEOUT_SECONDS", 0.01),
        patch.object(client, "cancel_connection_attempt") as cancel,
        pytest.raises(TimeoutError),
    ):
        await client.async_connect()
    cancel.assert_called()


async def test_connect_rejected_credentials(client: ESmartClient) -> None:
    def fake_connect(host: str, port: int) -> None:
        client.event("failed_auth", {"condition": "not-authorized"})
        client.event("failed_all_auth")

    client.connect = fake_connect  # type: ignore[method-assign]
    with pytest.raises(InvalidAuth):
        await client.async_connect()


async def test_connect_temporary_login_failure(client: ESmartClient) -> None:
    def fake_connect(host: str, port: int) -> None:
        client.event("failed_auth", {"condition": "temporary-auth-failure"})
        client.event("failed_all_auth")

    client.connect = fake_connect  # type: ignore[method-assign]
    with pytest.raises(CannotConnect):
        await client.async_connect()


async def test_reconnect_stops_and_reports_rejected_credentials(
    client: ESmartClient, callbacks: Callbacks
) -> None:
    attempts = AsyncMock(side_effect=InvalidAuth)
    with (
        patch.object(client, "async_connect", attempts),
        patch.object(client_mod, "RECONNECT_INITIAL_DELAY_SECONDS", 0),
    ):
        client._schedule_reconnect()
        await client._reconnect_task
    assert attempts.await_count == 1
    callbacks.auth_failed.assert_called_once()
    # Later disconnects don't start retrying a login that can't work.
    client._handle_disconnected("gone")
    assert client._reconnect_task.done()


async def test_reconnect_retries_until_connected(client: ESmartClient) -> None:
    attempts = AsyncMock(side_effect=[CannotConnect, TimeoutError, None])
    with (
        patch.object(client, "async_connect", attempts),
        patch.object(client_mod, "RECONNECT_INITIAL_DELAY_SECONDS", 0),
    ):
        client._schedule_reconnect()
        await client._reconnect_task
    assert attempts.await_count == 3


async def test_unexpected_disconnect_schedules_reconnect(client: ESmartClient) -> None:
    with patch.object(client, "_schedule_reconnect") as schedule:
        client._handle_disconnected("reset")
        schedule.assert_called_once()
        # Not while shutting down, and not while async_connect handles it.
        schedule.reset_mock()
        client._connecting = True
        client._handle_disconnected("reset")
        client._connecting = False
        client._closing = True
        client._handle_disconnected("reset")
        schedule.assert_not_called()


async def test_disconnect_cancels_pending_connection(client: ESmartClient) -> None:
    with patch.object(client, "cancel_connection_attempt") as cancel:
        await client.async_disconnect()
    cancel.assert_called()


# -- rooms -------------------------------------------------------------------


async def test_join_room_requests_state(
    client: ESmartClient, callbacks: Callbacks
) -> None:
    sent = _make_live(client)
    with (
        patch.object(client.plugin["xep_0045"], "join_muc_wait", AsyncMock()),
        patch.object(client, "_send_caps_presence"),
    ):
        await client.async_join_room(ROOM)
    assert client.rooms == {ROOM}
    callbacks.room_joined.assert_called_once_with(ROOM)
    assert [msg["headers"]["type"] for msg in sent] == [
        "capabilities",
        "config",
        "data",
        "structure",
        "doors",
        "scene",
    ]


async def test_join_room_without_confirmation_proceeds(
    client: ESmartClient, callbacks: Callbacks
) -> None:
    _make_live(client)
    with (
        patch.object(
            client.plugin["xep_0045"],
            "join_muc_wait",
            AsyncMock(side_effect=TimeoutError),
        ),
        patch.object(client, "_send_caps_presence"),
    ):
        await client.async_join_room(ROOM)
    callbacks.room_joined.assert_called_once_with(ROOM)


async def test_join_room_denied_retries_then_reports(
    client: ESmartClient, callbacks: Callbacks
) -> None:
    """Regression: a refused join used to be treated as joined."""
    error = _presence_error(client, "registration-required")
    join = AsyncMock(side_effect=error)
    with (
        patch.object(client.plugin["xep_0045"], "join_muc_wait", join),
        patch.object(client_mod, "ROOM_JOIN_RETRY_DELAYS", (0.0, 0.0)),
    ):
        await client.async_join_room(ROOM)
        for _ in range(20):
            if callbacks.room_denied.called:
                break
            await asyncio.sleep(0.01)
    assert join.await_count == 3
    assert client.rooms == set()
    callbacks.room_joined.assert_not_called()
    callbacks.room_denied.assert_called_once_with(ROOM)


async def test_join_room_nickname_conflict_switches_nickname(
    client: ESmartClient, callbacks: Callbacks
) -> None:
    _make_live(client)
    join = AsyncMock(side_effect=[_presence_error(client, "conflict"), None])
    with (
        patch.object(client.plugin["xep_0045"], "join_muc_wait", join),
        patch.object(client, "_send_caps_presence"),
        patch.object(client_mod, "ROOM_JOIN_RETRY_DELAYS", (0.0,)),
    ):
        await client.async_join_room(ROOM)
        for _ in range(20):
            if callbacks.room_joined.called:
                break
            await asyncio.sleep(0.01)
    first, second = (call.args[1] for call in join.await_args_list)
    assert first == "Home Assistant"
    assert second == "Home Assistant (123)"
    callbacks.room_joined.assert_called_once_with(ROOM)


async def test_join_ignores_non_rooms(client: ESmartClient) -> None:
    join = AsyncMock()
    with patch.object(client.plugin["xep_0045"], "join_muc_wait", join):
        await client.async_join_room("someone@example.com")
    join.assert_not_called()


# -- commands ----------------------------------------------------------------


async def test_commands_raise_when_not_connected(client: ESmartClient) -> None:
    with pytest.raises(NotConnected):
        client.set_onoff(ROOM, 9, True)
    with pytest.raises(NotConnected):
        client.door_pulse(ROOM, 0)
    with pytest.raises(NotConnected):
        client.activate_scene(ROOM, 1, "v")


async def test_operations_are_coalesced(client: ESmartClient) -> None:
    sent = _make_live(client)
    with patch.object(client_mod, "OPERATION_COALESCE_SECONDS", 0.0):
        client.set_onoff(ROOM, 9, True)
        client.set_dimming(ROOM, 42, 150)
        client.set_position(ROOM, 49, 2000, orientation=-5)
        client.set_orientation(ROOM, 49, 512)
        await asyncio.sleep(0.01)
    assert len(sent) == 1
    assert sent[0]["body"] == [
        {"id": 9, "onoff": "on"},
        {"id": 42, "dimming": "100%"},
        {"id": 49, "position": 1024, "orientation": 512},
    ]


async def test_stop_is_sent_immediately(client: ESmartClient) -> None:
    sent = _make_live(client)
    client.set_position(ROOM, 49, 512)
    client.stop_node(ROOM, 49)
    assert sent[0]["body"] == [{"id": 49, "position": 512, "stop": True}]


# -- inbound -----------------------------------------------------------------


async def test_groupchat_messages_are_dispatched(
    client: ESmartClient, callbacks: Callbacks
) -> None:
    def message(body: str) -> dict[str, Any]:
        return {"from": f"{ROOM}/master", "body": body}

    client._on_groupchat_message(
        message(json.dumps({"headers": {"method": "INFO", "type": "data"}, "body": {}}))
    )
    for junk in ("not json", "[1, 2]", json.dumps({"headers": "x"}), ""):
        client._on_groupchat_message(message(junk))
    await asyncio.sleep(0)
    assert callbacks.messages == [("INFO", "data", ROOM, {})]


async def test_handler_errors_dont_escape(
    client: ESmartClient, caplog: pytest.LogCaptureFixture
) -> None:
    client._on_message = MagicMock(side_effect=ValueError("bad"))
    client._dispatch_message("INFO", "data", ROOM, "master", {})
    assert "Error handling INFO/data" in caplog.text


async def test_sasl_is_kept_out_of_the_trace(client: ESmartClient) -> None:
    stanza = MagicMock()
    stanza.xml.tag = "{urn:ietf:params:xml:ns:xmpp-sasl}auth"
    stanza.name = "auth"
    assert client_mod._stanza_for_log(stanza) == "<auth> (SASL, redacted)"


# -- session, pairing and housekeeping ---------------------------------------


async def test_session_start_joins_rooms_from_roster(
    client: ESmartClient, callbacks: Callbacks
) -> None:
    with (
        patch.object(client, "get_roster", AsyncMock()),
        patch.object(client, "send_presence"),
        patch.object(client, "_send_caps_presence"),
        patch.object(client, "async_join_room", AsyncMock()) as join,
        patch.object(
            client, "client_roster", [f"{USERNAME}@myesmart.net", DEVICE, "x@y.z"]
        ),
    ):
        await client._on_session_start(None)
    join.assert_awaited_once_with(ROOM)
    assert callbacks.connection == [True]
    assert client._session_ready.is_set()
    assert client._watchdog_task is not None


async def test_pairing_is_accepted_by_subscription(client: ESmartClient) -> None:
    sent = _make_live(client)
    with (
        patch.object(
            client,
            "_join_muc_and_wait",
            AsyncMock(return_value=client_mod.RoomJoinResult(joined=True)),
        ),
        patch.object(client, "_send_caps_presence"),
        patch.object(client, "send_presence") as presence,
        patch.object(client.plugin["xep_0045"], "leave_muc"),
        patch.object(client, "async_join_room", AsyncMock()) as join,
        patch.object(client_mod, "PAIRING_ANNOUNCE_SECONDS", 0),
    ):
        pairing = asyncio.ensure_future(client.async_pair("6366"))
        for _ in range(5):
            await asyncio.sleep(0)
        client._handle_subscribe(DEVICE)
        rooms = await pairing
        await asyncio.gather(*client._tasks)
    assert rooms == [ROOM]
    assert sent == [
        {"jid": f"{USERNAME}@myesmart.net", "name": "Home Assistant", "code": 6366}
    ]
    presence.assert_any_call(pto=DEVICE, ptype="subscribed")
    presence.assert_any_call(pto=DEVICE, ptype="subscribe")
    join.assert_awaited_once_with(DEVICE)


async def test_pairing_times_out(client: ESmartClient) -> None:
    _make_live(client)
    with (
        patch.object(
            client,
            "_join_muc_and_wait",
            AsyncMock(return_value=client_mod.RoomJoinResult(joined=True)),
        ),
        patch.object(client, "_send_caps_presence"),
        patch.object(client.plugin["xep_0045"], "leave_muc"),
        patch.object(client_mod, "PAIRING_ANNOUNCE_SECONDS", 0),
        pytest.raises(client_mod.PairingTimeout),
    ):
        await client.async_pair("6366", pairing_timeout=0.01)


async def test_room_invite_is_joined(client: ESmartClient) -> None:
    with patch.object(client, "async_join_room", AsyncMock()) as join:
        await client._on_muc_invite({"from": f"{ROOM}/master"})
        await client._on_muc_invite({"from": "elsewhere@example.com"})
    join.assert_awaited_once_with(ROOM)


async def test_watchdog_reconnects_when_ping_is_unanswered(
    client: ESmartClient,
) -> None:
    from slixmpp.exceptions import IqTimeout

    client.is_connected = lambda: True  # type: ignore[method-assign]
    with (
        patch.object(client_mod, "WATCHDOG_INTERVAL_SECONDS", 0),
        patch.object(
            client.plugin["xep_0199"],
            "ping",
            AsyncMock(side_effect=IqTimeout(client.Iq())),
        ),
        patch.object(client, "disconnect") as disconnect,
    ):
        await client._async_watchdog()
    disconnect.assert_called_once_with(0.0)


async def test_disconnect_closes_the_stream(client: ESmartClient) -> None:
    client.transport = MagicMock()
    closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    closed.set_result(None)
    with patch.object(client, "disconnect", return_value=closed) as disconnect:
        await client.async_disconnect()
    disconnect.assert_called_once()
    client.transport = None


async def test_disconnect_aborts_a_hung_stream(client: ESmartClient) -> None:
    client.transport = MagicMock()
    hung: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    with (
        patch.object(client_mod, "DISCONNECT_TIMEOUT_SECONDS", 0.01),
        patch.object(client, "disconnect", return_value=hung),
        patch.object(client, "abort") as abort,
    ):
        await client.async_disconnect()
    abort.assert_called_once()
    client.transport = None


async def test_remove_account(hass: HomeAssistant) -> None:
    with (
        patch.object(ESmartClient, "async_connect", AsyncMock()) as connect,
        patch.object(ESmartClient, "async_unregister", AsyncMock()) as unregister,
        patch.object(ESmartClient, "async_disconnect", AsyncMock()) as disconnect,
    ):
        await client_mod.async_remove_account(USERNAME, PASSWORD)
    connect.assert_awaited_once()
    unregister.assert_awaited_once()
    disconnect.assert_awaited_once()
