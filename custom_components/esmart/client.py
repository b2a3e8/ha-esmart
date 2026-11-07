"""Asyncio XMPP client for the eSMART cloud, built on slixmpp.

The eSMART wall unit speaks a JSON-over-XMPP protocol: every installation is a
Multi-User Chat room (``<device>@conference.myesmart.net``) that the unit and
all remotes join. State arrives as ``INFO`` groupchat messages; we send
``CMD`` groupchat messages to actuate nodes.

This module owns the connection only — parsing structure into entities lives in
the hub. It has no Home Assistant dependencies: it exposes a small set of
high-level coroutines plus callbacks for connection state and inbound messages.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from dataclasses import dataclass
from functools import partial
import json
import logging
import secrets
import string
from typing import Any
from xml.etree import ElementTree as ET

import slixmpp
from slixmpp.exceptions import IqError, IqTimeout, PresenceError

from . import protocol
from .const import (
    CAPS_NODE,
    CAPS_VER,
    POSITION_MAX,
    XMPP_DOMAIN,
    XMPP_HOST,
    XMPP_PORT,
)

_LOGGER = logging.getLogger(__name__)

SHARED_ROOM = f"shared@conference.{XMPP_DOMAIN}"
SASL_NAMESPACE = "urn:ietf:params:xml:ns:xmpp-sasl"

# How long to wait for a connection (TCP, TLS, login and session) to come up.
CONNECT_TIMEOUT_SECONDS = 30.0
# How long a clean disconnect may take before the socket is closed hard.
DISCONNECT_TIMEOUT_SECONDS = 5.0
# How long the wall unit has to accept a pairing code.
PAIRING_TIMEOUT_SECONDS = 45.0
# How long to stay in the shared room after announcing ourselves.
PAIRING_ANNOUNCE_SECONDS = 1.0

# Reconnect backoff after an unexpected disconnect.
RECONNECT_INITIAL_DELAY_SECONDS = 5.0
RECONNECT_MAX_DELAY_SECONDS = 300.0

# Actuating commands issued within this window are merged into a single bus
# message. Short enough to feel instant, long enough to capture the burst of
# calls Home Assistant makes when a scene or group toggles many entities.
OPERATION_COALESCE_SECONDS = 0.02

# Active health check. A silently dropped TCP link (no RST) is otherwise only
# noticed minutes later, so we ping the server periodically; no answer within
# the timeout means the link is dead and we reconnect. ping() to our own host
# treats an error reply as alive, so servers without ping won't false-alarm.
WATCHDOG_INTERVAL_SECONDS = 120.0
WATCHDOG_TIMEOUT_SECONDS = 10.0

# A device-room join can fail for a while: right after pairing the wall unit may
# not have added this remote to the member list yet (407), and after a restart
# our previous session can still hold the nickname (409). Retry with growing
# delays; this restarts on every reconnect.
ROOM_JOIN_TIMEOUT_SECONDS = 10.0
ROOM_JOIN_RETRY_DELAYS: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0, 300.0)

# SASL failure conditions that mean the credentials themselves are bad, as
# opposed to a temporary server problem.
INVALID_AUTH_CONDITIONS = frozenset(
    {"not-authorized", "account-disabled", "credentials-expired", "invalid-mechanism"}
)

# Callback signatures.
MessageCallback = Callable[[str, str, str, Any], None]  # (method, type, room, body)
ConnectionCallback = Callable[[bool], None]
RoomCallback = Callable[[str], None]
AuthFailedCallback = Callable[[], None]


class ESmartError(Exception):
    """Base class for eSMART client errors."""


class CannotConnect(ESmartError):
    """The eSMART servers could not be reached or the session didn't start."""


class InvalidAuth(ESmartError):
    """The eSMART server rejected our credentials."""


class NotConnected(ESmartError):
    """A command was issued while the connection is down."""


class PairingTimeout(ESmartError):
    """The wall unit did not respond to a pairing attempt."""


@dataclass(frozen=True)
class RoomJoinResult:
    """Outcome of one attempt to join a device room."""

    joined: bool
    condition: str | None = None


def generate_username() -> str:
    """Generate a remote username in the ``i-<13>-<3>`` shape the unit expects."""
    alphabet = string.ascii_lowercase + string.digits
    mid = "".join(secrets.choice(alphabet) for _ in range(13))
    tail = f"{secrets.randbelow(1000):03d}"
    return f"i-{mid}-{tail}"


def generate_password() -> str:
    """Generate a 20-character alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def device_room_for(jid: Any) -> str | None:
    """Return the MUC room JID for an eSMART device, or ``None``.

    Accepts a device bare JID (``x@myesmart.net``), an already-resolved room JID
    (``x@conference.myesmart.net``), or a slixmpp ``JID``. Anything else — a whole
    stanza, an empty value — yields ``None`` so a stray object can never be turned
    into a bogus room we then try to join.
    """
    bare = getattr(jid, "bare", None) or str(jid)
    if bare.endswith(f"@conference.{XMPP_DOMAIN}"):
        return bare
    if bare.endswith(f"@{XMPP_DOMAIN}"):
        return bare.replace(f"@{XMPP_DOMAIN}", f"@conference.{XMPP_DOMAIN}")
    return None


def construct_off_loop[T](factory: Callable[[], T]) -> T:
    """Build a slixmpp-backed object in a worker thread.

    slixmpp's ``XMLStream.__init__`` both performs blocking SSL setup (which must
    stay off Home Assistant's event loop) and constructs an ``asyncio.Future()``
    (which needs a *current* event loop, raising in a bare worker thread). Run
    ``factory`` here with a throwaway loop so the future can bind. The object
    is then bound to a closed loop: call :func:`bind_to_running_loop` before
    using it.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return factory()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def bind_to_running_loop(xmpp: slixmpp.ClientXMPP) -> None:
    """Re-pin a client built by :func:`construct_off_loop` to the running loop.

    Without this, slixmpp schedules its connection on the closed throwaway loop
    and ``connect()`` raises ``RuntimeError: Event loop is closed``.
    """
    loop = asyncio.get_running_loop()
    if xmpp.loop is loop:
        return
    xmpp.loop = loop
    xmpp.disconnected = loop.create_future()


async def _async_close(xmpp: slixmpp.ClientXMPP) -> None:
    """Stop a client for good: cancel connection attempts and close the stream.

    slixmpp retries a failed connection by itself, forever, so a client that is
    merely dropped keeps reconnecting in the background. Cancelling the attempt
    stops that; then the stream is closed cleanly, or hard after a timeout.
    """
    xmpp.cancel_connection_attempt()
    if xmpp.transport is None:
        return
    try:
        await asyncio.wait_for(
            xmpp.disconnect(wait=2.0), timeout=DISCONNECT_TIMEOUT_SECONDS
        )
    except (TimeoutError, OSError, RuntimeError) as err:
        _LOGGER.debug("Clean disconnect failed (%s); closing the socket", err)
        xmpp.abort()


def _use_starttls_only(xmpp: slixmpp.ClientXMPP) -> None:
    """Connect with STARTTLS on the standard port, as the official apps do.

    With an explicit host slixmpp tries direct TLS first; on the STARTTLS port
    that handshake fails and fires ``connection_failed``, aborting the attempt.
    """
    xmpp.enable_direct_tls = False
    xmpp.enable_starttls = True
    xmpp.enable_plaintext = False


class _ReuseSSLContext:
    """Mixin: reuse the construction-time SSL context on every TLS handshake.

    slixmpp's default ``get_ssl_context`` calls ``set_default_verify_paths()`` on
    each handshake — blocking filesystem I/O on the event loop. The context built
    at construction already trusts the system CA store, so hand it back untouched.
    Mixed into every slixmpp client we create (long-lived and registration).
    """

    ssl_context: Any

    def get_ssl_context(self) -> Any:
        if self.ssl_context is None:
            return super().get_ssl_context()  # type: ignore[misc]
        return self.ssl_context


class _RegisterXMPP(_ReuseSSLContext, slixmpp.ClientXMPP):
    """Short-lived client used only for in-band account registration."""


async def async_register_account(username: str, password: str) -> None:
    """Register a brand-new XMPP account via in-band registration (XEP-0077).

    Returns once the account exists and we have logged in as it once. Raises
    :class:`CannotConnect` or :class:`TimeoutError` on failure.
    """
    jid = f"{username}@{XMPP_DOMAIN}"
    _LOGGER.debug("Registering new eSMART account %s", jid)
    # Constructing a slixmpp client builds an SSL context (blocking I/O); build
    # it off the event loop, then bind it to this loop.
    loop = asyncio.get_running_loop()
    xmpp = await loop.run_in_executor(
        None, construct_off_loop, partial(_RegisterXMPP, jid, password)
    )
    bind_to_running_loop(xmpp)
    _use_starttls_only(xmpp)
    xmpp.register_plugin("xep_0030")
    xmpp.register_plugin("xep_0077")
    xmpp.plugin["xep_0077"].force_registration = True

    done: asyncio.Future[None] = loop.create_future()

    def _fail(error: Exception) -> None:
        if not done.done():
            done.set_exception(error)

    async def _on_register(_iq: Any) -> None:
        resp = xmpp.Iq()
        resp["type"] = "set"
        resp["register"]["username"] = xmpp.boundjid.user
        resp["register"]["password"] = password
        try:
            await resp.send()
            _LOGGER.debug("Submitted registration for %s", jid)
        except (IqError, IqTimeout) as err:
            _LOGGER.warning("eSMART refused the account registration: %s", err)
            _fail(CannotConnect(f"Registration was refused: {err}"))

    def _on_session_start(_event: Any) -> None:
        if not done.done():
            done.set_result(None)

    xmpp.add_event_handler("register", _on_register)
    xmpp.add_event_handler("session_start", _on_session_start)
    xmpp.add_event_handler(
        "failed_all_auth",
        lambda _event: _fail(CannotConnect("Registration was refused by the server")),
    )
    xmpp.add_event_handler(
        "connection_failed",
        lambda event: _fail(CannotConnect(f"Could not reach eSMART: {event}")),
    )

    xmpp.connect(XMPP_HOST, XMPP_PORT)
    try:
        await asyncio.wait_for(done, timeout=CONNECT_TIMEOUT_SECONDS)
        _LOGGER.debug("Registered eSMART account %s", jid)
    finally:
        await _async_close(xmpp)


class ESmartClient(_ReuseSSLContext, slixmpp.ClientXMPP):
    """Long-lived eSMART connection with reconnect and command helpers."""

    def __init__(
        self,
        username: str,
        password: str,
        nickname: str,
        *,
        on_message: MessageCallback,
        on_connection: ConnectionCallback,
        on_auth_failed: AuthFailedCallback | None = None,
        on_room_joined: RoomCallback | None = None,
        on_room_denied: RoomCallback | None = None,
        join_rooms: bool = True,
    ) -> None:
        super().__init__(f"{username}@{XMPP_DOMAIN}", password)
        self._nickname = nickname
        self._on_message = on_message
        self._on_connection = on_connection
        self._on_auth_failed = on_auth_failed
        self._on_room_joined = on_room_joined
        self._on_room_denied = on_room_denied
        # A client used only for housekeeping (e.g. deleting the account) skips
        # room joins and the watchdog.
        self._join_rooms = join_rooms
        self._rooms: set[str] = set()
        self._paired = asyncio.Event()
        self._pairing = False
        # Device JIDs that confirmed pairing, in case the roster lags behind.
        self._paired_devices: set[str] = set()
        # Per-room command coalescing: room -> {node_id -> merged operation}.
        self._pending_ops: dict[str, dict[int, dict[str, Any]]] = {}
        self._flush_handles: dict[str, asyncio.TimerHandle] = {}
        # Retry of device-room joins that fail (membership, nickname conflict).
        self._room_retries: dict[str, int] = {}
        self._room_retry_handles: dict[str, asyncio.TimerHandle] = {}
        # Nickname per room; switched to a unique one if ours is taken.
        self._room_nicks: dict[str, str] = {}
        # Set at the end of _on_session_start, so async_connect can wait for the
        # session to be *fully* ready (presence sent, roster fetched) instead of
        # racing against the asynchronous session_start handler.
        self._session_ready = asyncio.Event()
        self._connect_error: asyncio.Future[None] | None = None
        # The SASL condition of the last failed login, to tell bad credentials
        # apart from a temporary server problem.
        self._last_auth_condition: str | None = None
        # Reconnect state. slixmpp's own retry only covers the TCP connect, so
        # we drive reconnects ourselves: on an unexpected disconnect we retry
        # with backoff until the session is back, unless we are shutting down,
        # the credentials were rejected, or a caller is connecting explicitly.
        self._closing = False
        self._connecting = False
        self._auth_failed = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

        _use_starttls_only(self)
        # Auto-accept and reciprocate the subscription the wall unit sends us.
        self.auto_authorize = True
        self.auto_subscribe = True

        self.register_plugin("xep_0030")  # Service discovery
        self.register_plugin("xep_0045")  # Multi-User Chat
        self.register_plugin("xep_0077")  # In-band registration (account removal)
        self.register_plugin("xep_0199")  # XMPP Ping (keepalive)

        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("connection_failed", self._on_connection_failed)
        self.add_event_handler("failed_auth", self._on_failed_auth)
        self.add_event_handler("failed_all_auth", self._on_failed_all_auth)
        self.add_event_handler("groupchat_message", self._on_groupchat_message)
        self.add_event_handler("groupchat_invite", self._on_muc_invite)
        self.add_event_handler("groupchat_direct_invite", self._on_muc_invite)
        # The wall unit confirms pairing by subscribing to our presence and by
        # adding us to its roster. slixmpp auto-authorizes the roster side, but
        # still emits these raw events, so we listen to both.
        self.add_event_handler("presence_subscribe", self._on_subscribe)
        self.add_event_handler("roster_update", self._on_roster_update)

        # Raw stanza tracing for protocol diagnostics, active only when this
        # integration's logger is at DEBUG. Pure passthroughs that never alter,
        # drop, or raise — so they cannot disturb the stream.
        self.add_filter("in", self._trace_inbound)
        self.add_filter("out", self._trace_outbound)

    # -- diagnostics -------------------------------------------------------

    def _trace_inbound(self, stanza: Any) -> Any:
        try:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("XMPP RECV %s", _stanza_for_log(stanza))
        except Exception:  # noqa: BLE001 - tracing must never disturb the stream
            pass
        return stanza

    def _trace_outbound(self, stanza: Any) -> Any:
        try:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("XMPP SEND %s", _stanza_for_log(stanza))
        except Exception:  # noqa: BLE001 - tracing must never disturb the stream
            pass
        return stanza

    @property
    def bare_jid(self) -> str:
        return self.boundjid.bare

    @property
    def rooms(self) -> set[str]:
        """Device rooms we have joined."""
        return set(self._rooms)

    @property
    def session_ready(self) -> bool:
        """True while a session is up and commands can be sent."""
        return self._session_ready.is_set() and self.is_connected()

    # -- lifecycle ---------------------------------------------------------

    async def async_connect(self) -> None:
        """Connect and wait until the XMPP session is fully established.

        Raises :class:`InvalidAuth` if the credentials are rejected and
        :class:`CannotConnect` (or :class:`TimeoutError`) otherwise. On failure
        nothing keeps running in the background: retrying is up to the caller.
        """
        bind_to_running_loop(self)
        loop = self.loop
        self._session_ready.clear()
        self._connect_error = loop.create_future()
        self._last_auth_condition = None
        self._connecting = True

        _LOGGER.debug(
            "Connecting to eSMART at %s:%s as %s", XMPP_HOST, XMPP_PORT, self.bare_jid
        )
        self.connect(XMPP_HOST, XMPP_PORT)

        ready = asyncio.ensure_future(self._session_ready.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready, self._connect_error},
                timeout=CONNECT_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._connect_error in done:
                # Raises the error set by the failure handlers.
                self._connect_error.result()
            if ready not in done:
                raise TimeoutError(
                    f"Timed out connecting to eSMART ({XMPP_HOST}:{XMPP_PORT})"
                )
        except BaseException:
            # Stop slixmpp's own retries and drop any half-open stream, so a
            # failed attempt can never log in later behind the caller's back.
            self.cancel_connection_attempt()
            if self.transport is not None:
                self.abort()
            raise
        finally:
            self._connecting = False
            ready.cancel()
            if self._connect_error is not None and not self._connect_error.done():
                self._connect_error.cancel()
            self._connect_error = None
        self._auth_failed = False
        _LOGGER.debug("Connected to eSMART as %s", self.bare_jid)

    async def async_disconnect(self) -> None:
        """Tear the connection down for good and wait until it is closed."""
        _LOGGER.debug("Disconnecting from eSMART (%s)", self.bare_jid)
        self._closing = True
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._stop_watchdog()
        for handle in (
            *self._flush_handles.values(),
            *self._room_retry_handles.values(),
        ):
            handle.cancel()
        self._flush_handles.clear()
        self._pending_ops.clear()
        self._room_retry_handles.clear()
        self._room_retries.clear()
        for task in list(self._tasks):
            task.cancel()
        self._session_ready.clear()
        await _async_close(self)

    async def async_unregister(self) -> None:
        """Delete this account from the eSMART server (XEP-0077).

        Needs a live session. The server ends the stream afterwards.
        """
        await self.plugin["xep_0077"].cancel_registration(
            timeout=ROOM_JOIN_TIMEOUT_SECONDS
        )
        _LOGGER.debug("Deleted eSMART account %s", self.bare_jid)

    def _call_on_loop(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run a callback in its own event-loop iteration.

        slixmpp runs stanza handlers inline while it parses the stream. Deferring
        the work keeps parsing fast and keeps a failing handler from breaking it.
        """
        self.loop.call_soon(callback, *args)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run a coroutine as a fire-and-forget task, logging any failure.

        The task is referenced until it finishes (asyncio only keeps weak
        references to tasks) and cancelled on disconnect.
        """
        task = self.loop.create_task(coro)
        self._tasks.add(task)

        def _report(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            if (exc := finished.exception()) is not None:
                _LOGGER.error("Background eSMART task failed: %s", exc)

        task.add_done_callback(_report)

    async def _on_session_start(self, _event: Any) -> None:
        """Send initial presence, then (re)join every known device room."""
        self.send_presence()
        self._send_caps_presence()
        try:
            await self.get_roster()
        except (IqError, IqTimeout) as err:
            _LOGGER.warning("Roster fetch failed on session start: %s", err)
        self._on_connection(True)

        rooms = self._device_rooms_from_roster()
        _LOGGER.debug(
            "eSMART session ready; %d device room(s) in roster: %s",
            len(rooms),
            ", ".join(rooms) or "<none yet>",
        )
        # Signal async_connect that the session is fully up before we go on to
        # (re)join rooms, so callers can rely on presence already being sent.
        self._session_ready.set()
        if not self._join_rooms:
            return
        # A fresh session gets a fresh set of join retries.
        for handle in self._room_retry_handles.values():
            handle.cancel()
        self._room_retry_handles.clear()
        self._room_retries.clear()
        self._rooms.clear()
        for room in rooms:
            await self.async_join_room(room)
        self._start_watchdog()

    def _on_disconnected(self, reason: Any) -> None:
        self._call_on_loop(self._handle_disconnected, reason)

    def _handle_disconnected(self, reason: Any) -> None:
        self._session_ready.clear()
        self._on_connection(False)
        self._stop_watchdog()
        if self._closing:
            _LOGGER.debug("eSMART disconnected (shutting down): %s", reason)
            return
        if self._connecting:
            # async_connect is waiting on this attempt and reports the failure.
            _LOGGER.debug("eSMART disconnected while connecting: %s", reason)
            return
        if self._auth_failed:
            _LOGGER.debug("eSMART disconnected after rejecting our login")
            return
        _LOGGER.warning(
            "Connection to eSMART lost (%s); reconnecting", reason or "disconnected"
        )
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Start the reconnect loop unless one is already running."""
        if self._closing or self._auth_failed:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self.loop.create_task(self._async_reconnect_loop())

    async def _async_reconnect_loop(self) -> None:
        """Retry connecting with capped exponential backoff until it sticks."""
        delay = RECONNECT_INITIAL_DELAY_SECONDS
        attempt = 0
        while not self._closing:
            await asyncio.sleep(delay)
            if self._closing:
                return
            attempt += 1
            _LOGGER.debug("Reconnecting to eSMART (attempt %d)", attempt)
            try:
                await self.async_connect()
            except asyncio.CancelledError:
                raise
            except InvalidAuth:
                _LOGGER.error(
                    "eSMART rejected this remote's credentials. The remote was "
                    "probably deleted on the wall unit; pair again to fix this"
                )
                self._auth_failed = True
                if self._on_auth_failed is not None:
                    self._on_auth_failed()
                return
            except Exception as err:  # noqa: BLE001 - keep retrying on any failure
                delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)
                _LOGGER.debug(
                    "eSMART reconnect failed (%s); retrying in %.0fs", err, delay
                )
            else:
                _LOGGER.info("Reconnected to eSMART")
                return

    # -- health watchdog ---------------------------------------------------

    def _start_watchdog(self) -> None:
        self._stop_watchdog()
        self._watchdog_task = self.loop.create_task(self._async_watchdog())

    def _stop_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def _async_watchdog(self) -> None:
        """Ping the server periodically; reconnect if it stops answering."""
        ping = self.plugin["xep_0199"]
        while not self._closing:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            if self._closing or not self.is_connected():
                return
            try:
                await ping.ping(timeout=WATCHDOG_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except IqTimeout:
                _LOGGER.warning("eSMART did not answer a health ping; reconnecting")
                self.disconnect(0.0)
                return
            except Exception as err:  # noqa: BLE001 - any reply means the link is up
                _LOGGER.debug("eSMART health ping returned %s (link alive)", err)

    def _on_connection_failed(self, reason: Any) -> None:
        self._call_on_loop(self._handle_connection_failed, reason)

    def _handle_connection_failed(self, reason: Any) -> None:
        _LOGGER.debug("eSMART connection attempt failed: %s", reason)
        self._fail_connect(CannotConnect(f"eSMART connection failed: {reason}"))
        self._on_connection(False)

    def _on_failed_auth(self, stanza: Any) -> None:
        # Fired once per SASL mechanism tried; failed_all_auth follows if none
        # works. Remember why, to tell bad credentials from a server problem.
        with contextlib.suppress(KeyError, TypeError):
            self._last_auth_condition = str(stanza["condition"]) or None

    def _on_failed_all_auth(self, _event: Any) -> None:
        self._call_on_loop(self._handle_failed_all_auth)

    def _handle_failed_all_auth(self) -> None:
        condition = self._last_auth_condition
        if condition is None or condition in INVALID_AUTH_CONDITIONS:
            _LOGGER.debug("eSMART rejected our login (%s)", condition)
            self._auth_failed = True
            self._fail_connect(InvalidAuth("eSMART rejected our credentials"))
        else:
            _LOGGER.debug("eSMART login failed temporarily (%s)", condition)
            self._fail_connect(CannotConnect(f"eSMART login failed: {condition}"))
        self._on_connection(False)

    def _fail_connect(self, error: Exception) -> None:
        if self._connect_error is not None and not self._connect_error.done():
            self._connect_error.set_exception(error)

    # -- pairing -----------------------------------------------------------

    async def async_pair(
        self, code: str, pairing_timeout: float = PAIRING_TIMEOUT_SECONDS
    ) -> list[str]:
        """Pair with the wall unit using the on-screen code.

        Announces ourselves in the shared MUC, then waits for the unit to
        subscribe to us (it does so once it accepts the code). Returns the
        discovered device room JIDs.
        """
        self._paired.clear()

        # Already paired? The unit will be in our roster already.
        rooms = self._device_rooms_from_roster()
        if rooms:
            _LOGGER.debug("Already paired; device room(s): %s", ", ".join(rooms))
            return rooms

        nick = self._nickname
        _LOGGER.debug("Pairing with eSMART as '%s'", nick)
        self._pairing = True
        try:
            self._send_caps_presence()
            await self._join_muc_and_wait(SHARED_ROOM, nick)
            _LOGGER.debug("Joined shared pairing room %s", SHARED_ROOM)

            payload = json.dumps(
                {
                    "jid": self.bare_jid,
                    "name": self._nickname,
                    "code": int(code),
                }
            )
            self.send_message(mto=SHARED_ROOM, mbody=payload, mtype="groupchat")
            _LOGGER.debug("Sent pairing announcement")

            await asyncio.sleep(PAIRING_ANNOUNCE_SECONDS)
            self.plugin["xep_0045"].leave_muc(SHARED_ROOM, nick)

            _LOGGER.debug(
                "Waiting up to %.0fs for the wall unit to accept pairing",
                pairing_timeout,
            )
            try:
                await asyncio.wait_for(self._paired.wait(), timeout=pairing_timeout)
            except TimeoutError as err:
                _LOGGER.warning(
                    "Pairing timed out. Confirm the wall unit is showing a pairing "
                    "code and that '%s' is not already listed as a remote",
                    nick,
                )
                raise PairingTimeout from err
        finally:
            self._pairing = False

        rooms = sorted(
            set(self._device_rooms_from_roster())
            | {room for jid in self._paired_devices if (room := device_room_for(jid))}
        )
        _LOGGER.debug(
            "Pairing succeeded; device room(s): %s", ", ".join(rooms) or "pending"
        )
        return rooms

    def _on_subscribe(self, presence: Any) -> None:
        """A device subscribed to us: pairing succeeded, so join its room."""
        sender = presence["from"].bare
        if not sender.endswith(f"@{XMPP_DOMAIN}") or sender == self.bare_jid:
            return
        self._call_on_loop(self._handle_subscribe, sender)

    def _handle_subscribe(self, sender: str) -> None:
        # async_join_room normalises the JID and ignores anything that isn't a
        # real device/room, so a stray sender can never become a bogus join.
        _LOGGER.debug("Wall unit %s subscribed to us; pairing accepted", sender)
        # Explicitly accept and reciprocate the subscription. This mutual
        # presence subscription is what tells the wall unit to finish pairing
        # and add us to its room's member list; relying on slixmpp's
        # auto-handling alone has not been enough.
        self.send_presence(pto=sender, ptype="subscribed")
        self.send_presence(pto=sender, ptype="subscribe")
        self._paired_devices.add(sender)
        self._paired.set()
        if self._join_rooms:
            self._spawn(self.async_join_room(sender))

    def _on_roster_update(self, _event: Any) -> None:
        """A roster push containing a device JID also confirms pairing."""
        if not self._pairing or self._paired.is_set():
            return
        self._call_on_loop(self._handle_roster_paired)

    def _handle_roster_paired(self) -> None:
        if self._paired.is_set():
            return
        rooms = self._device_rooms_from_roster()
        if rooms:
            _LOGGER.debug("Device appeared in roster; pairing accepted: %s", rooms)
            self._paired.set()
            if self._join_rooms:
                for room in rooms:
                    self._spawn(self.async_join_room(room))

    async def _on_muc_invite(self, event: Any) -> None:
        """Join a room the wall unit invites us into (belt-and-braces)."""
        room = ""
        try:
            if isinstance(event, dict):
                room = str(event.get("from") or event.get("room") or "")
            else:  # a Message stanza
                room = str(event["groupchat_invite"]["jid"] or event["from"])
        except KeyError, TypeError:
            return
        room = room.split("/")[0]
        if room and device_room_for(room) is not None:
            _LOGGER.debug("Received room invite from wall unit: %s", room)
            self._paired.set()
            if self._join_rooms:
                await self.async_join_room(room)

    def _device_rooms_from_roster(self) -> list[str]:
        rooms: list[str] = []
        for raw_jid in self.client_roster:
            jid = str(raw_jid)
            if (
                jid.endswith(f"@{XMPP_DOMAIN}")
                and jid != self.bare_jid
                and (room := device_room_for(jid)) is not None
            ):
                rooms.append(room)
        return rooms

    # -- rooms -------------------------------------------------------------

    async def _join_muc_and_wait(self, room_jid: str, nick: str) -> RoomJoinResult:
        """Join a MUC and wait until the server seats us in the room.

        Sending a groupchat message before the join is acknowledged makes the
        server drop it, so we wait for our own presence reflection.
        """
        muc = self.plugin["xep_0045"]
        try:
            await muc.join_muc_wait(
                room_jid, nick, maxstanzas=0, timeout=ROOM_JOIN_TIMEOUT_SECONDS
            )
        except PresenceError as err:
            # The room said no: 407 (registration-required) until the wall unit
            # has made us a member, 409 (conflict) if the nickname is taken.
            # We are not in the room, so report it and let the caller retry.
            condition = getattr(err, "condition", None) or "unknown"
            _LOGGER.debug("eSMART refused our join to %s: %s", room_jid, condition)
            return RoomJoinResult(joined=False, condition=condition)
        except TimeoutError:
            # The eSMART server seats us in the room (we receive our own
            # presence with status 110 and a steady stream of INFO/data), but it
            # doesn't always send the reflection join_muc_wait blocks on, so it
            # times out spuriously. Proceed: bailing here would stop us from
            # ever requesting the structure, leaving the room with no entities.
            _LOGGER.debug(
                "No join confirmation for %s; proceeding (the server seats us "
                "without always reflecting our presence)",
                room_jid,
            )
        # join_muc_wait already sent our join presence. Re-advertise it with the
        # caps the wall unit expects (slixmpp's own join omits them).
        self._send_caps_presence(to=f"{room_jid}/{nick}")
        return RoomJoinResult(joined=True)

    def _room_nick(self, room: str) -> str:
        return self._room_nicks.get(room, self._nickname)

    async def async_join_room(self, room_jid: Any) -> None:
        """Join a device MUC room and request its current state.

        ``room_jid`` may be a device JID, a room JID, or a slixmpp ``JID``;
        anything that isn't a real eSMART room is ignored rather than joined.
        """
        room = device_room_for(room_jid)
        if room is None:
            _LOGGER.debug("Ignoring join request for non-room target: %r", room_jid)
            return
        result = await self._join_muc_and_wait(room, self._room_nick(room))
        if not result.joined:
            if result.condition == "conflict":
                # Someone (often a stale session of ours) holds the nickname.
                # Membership is per account, so a unique nickname is fine.
                self._room_nicks[room] = f"{self._nickname} ({self.boundjid.user[-3:]})"
            self._schedule_room_retry(room, result.condition)
            return
        self._room_retries.pop(room, None)
        if room not in self._rooms:
            self._rooms.add(room)
            _LOGGER.debug("Joined eSMART room %s", room)
        if self._on_room_joined is not None:
            self._on_room_joined(room)
        await self.async_request_initial(room)

    def _schedule_room_retry(self, room: str, condition: str | None) -> None:
        """Retry a failed device-room join with growing delays."""
        if room in self._rooms or self._closing:
            return
        if room in self._room_retry_handles:
            return  # a retry is already pending for this room
        attempt = self._room_retries.get(room, 0)
        if attempt >= len(ROOM_JOIN_RETRY_DELAYS):
            _LOGGER.warning(
                "Could not join eSMART room %s after %d attempts (%s). If the wall "
                "unit doesn't list this remote, pair again",
                room,
                attempt + 1,
                condition,
            )
            if condition == "registration-required" and self._on_room_denied:
                self._on_room_denied(room)
            return
        delay = ROOM_JOIN_RETRY_DELAYS[attempt]
        self._room_retries[room] = attempt + 1
        _LOGGER.debug(
            "Retrying join of %s in %.0fs (%s; attempt %d/%d)",
            room,
            delay,
            condition,
            attempt + 1,
            len(ROOM_JOIN_RETRY_DELAYS),
        )

        def _fire() -> None:
            self._room_retry_handles.pop(room, None)
            self._spawn(self.async_join_room(room))

        self._room_retry_handles[room] = self.loop.call_later(delay, _fire)

    async def async_request_initial(self, room_jid: str) -> None:
        """Pull capabilities, structure, data, doors and scenes for a room."""
        _LOGGER.debug("Requesting initial state for room %s", room_jid)
        for msg_type, body in (
            ("capabilities", {}),
            ("config", {"data": "tablet"}),
            ("data", {"summary": "data"}),
            ("structure", {}),
            ("doors", {}),
            ("scene", {"fullConfig": False}),
        ):
            self._send_json(
                room_jid,
                protocol.build_get(
                    from_jid=self.bare_jid, msg_type=msg_type, body=body
                ),
            )

    # -- commands ----------------------------------------------------------
    #
    # Actuating commands are coalesced: items queued within a short window are
    # merged (per node) and sent as a single bus message, so toggling a whole
    # room of lights actuates them together instead of one at a time. Each
    # raises NotConnected when the session is down, so the caller can tell the
    # user instead of the command vanishing.

    def _ensure_connected(self) -> None:
        if not self.session_ready:
            raise NotConnected("Not connected to eSMART")

    def set_onoff(self, room_jid: str, node_id: int, on: bool) -> None:
        self._queue_operation(room_jid, {"id": node_id, "onoff": "on" if on else "off"})

    def set_dimming(self, room_jid: str, node_id: int, percent: int) -> None:
        pct = max(0, min(100, percent))
        self._queue_operation(room_jid, {"id": node_id, "dimming": f"{pct}%"})

    def set_position(
        self, room_jid: str, node_id: int, position: int, orientation: int | None = None
    ) -> None:
        item: dict[str, Any] = {"id": node_id, "position": _clamp_ticks(position)}
        if orientation is not None:
            item["orientation"] = _clamp_ticks(orientation)
        self._queue_operation(room_jid, item)

    def set_orientation(
        self, room_jid: str, node_id: int, orientation: int, position: int | None = None
    ) -> None:
        item: dict[str, Any] = {"id": node_id, "orientation": _clamp_ticks(orientation)}
        if position is not None:
            item["position"] = _clamp_ticks(position)
        self._queue_operation(room_jid, item)

    def set_setpoint(self, room_jid: str, node_id: int, temp: float) -> None:
        # The hub has already snapped this to the wall unit's range and step.
        self._queue_operation(room_jid, {"id": node_id, "setpoint": temp})

    def stop_node(self, room_jid: str, node_id: int) -> None:
        # Stop is time-sensitive; flush any pending items for this room with it
        # so the stop is not delayed behind the coalescing window.
        self._queue_operation(room_jid, {"id": node_id, "stop": True})
        self._flush_operations(room_jid)

    def door_pulse(self, room_jid: str, door_id: int) -> None:
        self._ensure_connected()
        self._send_json(
            room_jid,
            protocol.build_door_pulse(from_jid=self.bare_jid, door_id=door_id),
        )

    def activate_scene(self, room_jid: str, num: int, version: str) -> None:
        self._ensure_connected()
        self._send_json(
            room_jid,
            protocol.build_scene_activation(
                from_jid=self.bare_jid, num=num, version=version
            ),
        )

    def _queue_operation(self, room_jid: str, item: dict[str, Any]) -> None:
        """Buffer a node operation, merging by id, and schedule a flush."""
        self._ensure_connected()
        node_id = item["id"]
        room_ops = self._pending_ops.setdefault(room_jid, {})
        if (merged := room_ops.get(node_id)) is None:
            room_ops[node_id] = dict(item)
        else:
            merged.update(item)
        if room_jid not in self._flush_handles:
            self._flush_handles[room_jid] = self.loop.call_later(
                OPERATION_COALESCE_SECONDS, self._flush_operations, room_jid
            )

    def _flush_operations(self, room_jid: str) -> None:
        """Send all buffered operations for a room as one bus message."""
        if (handle := self._flush_handles.pop(room_jid, None)) is not None:
            handle.cancel()
        room_ops = self._pending_ops.pop(room_jid, None)
        if not room_ops:
            return
        self._send_json(
            room_jid,
            protocol.build_operation(
                from_jid=self.bare_jid, items=list(room_ops.values())
            ),
        )

    # -- inbound -----------------------------------------------------------

    def _on_groupchat_message(self, msg: Any) -> None:
        room = str(msg["from"]).split("/")[0]
        body = msg.get("body")
        if not body:
            return
        try:
            parsed = json.loads(body)
        except ValueError, TypeError:
            _LOGGER.debug("Ignoring non-JSON message from %s: %s", room, body)
            return
        if not isinstance(parsed, dict):
            return
        headers = parsed.get("headers")
        if not isinstance(headers, dict):
            return
        method = headers.get("method")
        msg_type = headers.get("type")
        if not method or not msg_type:
            return
        self._call_on_loop(
            self._dispatch_message,
            method,
            msg_type,
            room,
            headers.get("from"),
            parsed.get("body"),
        )

    def _dispatch_message(
        self, method: str, msg_type: str, room: str, sender: Any, body: Any
    ) -> None:
        # The sender tells our own commands apart from the wall tablet's and the
        # app's, and the unit itself reports as "master".
        _LOGGER.debug(
            "RECV %s/%s from %s (sender %s): %s", method, msg_type, room, sender, body
        )
        try:
            self._on_message(method, msg_type, room, body)
        except Exception:
            _LOGGER.exception(
                "Error handling %s/%s message from %s: %s",
                method,
                msg_type,
                room,
                body,
            )

    # -- low level ---------------------------------------------------------

    def _send_json(self, room_jid: str, payload: dict[str, Any]) -> None:
        headers = payload.get("headers", {})
        _LOGGER.debug(
            "SEND %s/%s to %s: %s",
            headers.get("method"),
            headers.get("type"),
            room_jid,
            payload.get("body"),
        )
        if not self.is_connected():
            # Only reachable when the link dropped inside the coalescing window.
            _LOGGER.warning(
                "Connection to eSMART dropped; %s/%s command was not sent",
                headers.get("method"),
                headers.get("type"),
            )
            return
        self.send_message(mto=room_jid, mbody=json.dumps(payload), mtype="groupchat")

    def _send_caps_presence(self, to: str | None = None) -> None:
        """Send a presence carrying the entity capabilities the unit expects.

        With no ``to`` this broadcasts to our roster; with a ``room/nick`` target
        it advertises caps inside a MUC.
        """
        presence = self.make_presence(pto=to) if to else self.make_presence()
        caps = ET.Element(
            "{http://jabber.org/protocol/caps}c",
            {"hash": "sha-1", "node": CAPS_NODE, "ver": CAPS_VER},
        )
        presence.appendxml(caps)
        presence.send()


async def async_remove_account(username: str, password: str) -> None:
    """Log in as a remote and delete its account from the eSMART server.

    Best effort, used when the integration is removed or re-paired. Raises on
    failure so the caller can decide how loudly to report it.
    """
    loop = asyncio.get_running_loop()
    client: ESmartClient = await loop.run_in_executor(
        None,
        construct_off_loop,
        partial(
            ESmartClient,
            username=username,
            password=password,
            nickname=username,
            on_message=lambda *_: None,
            on_connection=lambda _connected: None,
            join_rooms=False,
        ),
    )
    try:
        await client.async_connect()
        await client.async_unregister()
    finally:
        await client.async_disconnect()


def _clamp_ticks(value: int) -> int:
    return max(0, min(POSITION_MAX, value))


def _stanza_for_log(stanza: Any) -> Any:
    """Keep SASL out of the trace: those stanzas carry credential material."""
    # Match the XML tag: a stanza built with a stream has its .namespace attribute
    # overwritten with the stream's default ("jabber:client").
    if stanza.xml.tag.startswith(f"{{{SASL_NAMESPACE}}}"):
        return f"<{stanza.name}> (SASL, redacted)"
    return stanza
