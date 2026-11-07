"""The eSMART hub: bridges the XMPP client to Home Assistant entities."""

from __future__ import annotations

from collections.abc import Iterator
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .client import ESmartClient, NotConnected
from .const import (
    CONF_NICKNAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_NICKNAME,
    DOMAIN,
    ISSUE_ROOM_ACCESS,
    KIND_CLIMATE,
    KIND_COVER,
    NODE_TYPE_DIMMABLE_LIGHT,
    POSITION_MAX,
    USERNAME_PREFIX,
    signal_connectivity,
    signal_new_node,
    signal_node_removed,
    signal_node_update,
)
from .protocol import (
    ESmartDoor,
    ESmartMeter,
    ESmartNode,
    ESmartScene,
    SetpointLimits,
    classify_node,
    installation_id,
    parse_meter_readings,
    parse_setpoint_limits,
)

_LOGGER = logging.getLogger(__name__)

type ESmartConfigEntry = ConfigEntry[ESmartHub]
type ESmartItem = ESmartNode | ESmartDoor | ESmartScene | ESmartMeter


class ESmartHub:
    """Owns the connection and the live view of every discovered device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.connected = False
        # Device registry id of the controller, set once it has been registered.
        self.controller_device_id: str | None = None
        self.nodes: dict[str, ESmartNode] = {}
        self.doors: dict[str, ESmartDoor] = {}
        self.scenes: dict[str, ESmartScene] = {}
        self.meters: dict[str, ESmartMeter] = {}
        # Last raw structure per room, for diagnostics.
        self.raw_structure: dict[str, Any] = {}
        # Heating setpoint range per installation room, from its INFO/config.
        self._setpoint_limits: dict[str, SetpointLimits] = {}
        # Telemetry that arrived before its node was discovered in the structure.
        self._pending_state: dict[str, dict[str, Any]] = {}
        # Last raw telemetry per cover, so only changes reach the debug log.
        self._cover_telemetry: dict[str, dict[str, Any]] = {}

        data = entry.data
        self._username: str = data[CONF_USERNAME]
        self._client = ESmartClient(
            username=data[CONF_USERNAME],
            password=data[CONF_PASSWORD],
            nickname=data.get(CONF_NICKNAME) or DEFAULT_NICKNAME,
            on_message=self._handle_message,
            on_connection=self._handle_connection,
            on_auth_failed=self._handle_auth_failed,
            on_room_joined=self._handle_room_joined,
            on_room_denied=self._handle_room_denied,
        )

    @property
    def client(self) -> ESmartClient:
        return self._client

    def items(self) -> Iterator[ESmartItem]:
        """Every discovered node, door, scene and meter."""
        yield from self.nodes.values()
        yield from self.doors.values()
        yield from self.scenes.values()
        yield from self.meters.values()

    def setpoint_limits(self, room_jid: str) -> SetpointLimits:
        """The setpoint range for a room, or the defaults until its config arrives."""
        return self._setpoint_limits.get(room_jid, SetpointLimits())

    async def async_start(self) -> None:
        """Connect to the eSMART cloud."""
        await self._client.async_connect()

    async def async_stop(self) -> None:
        """Disconnect cleanly."""
        await self._client.async_disconnect()

    # -- connection events ---------------------------------------------------

    @callback
    def _handle_connection(self, connected: bool) -> None:
        if connected == self.connected:
            return
        self.connected = connected
        _LOGGER.debug("eSMART connection %s", "established" if connected else "lost")
        async_dispatcher_send(self.hass, signal_connectivity(self.entry_id))

    @callback
    def _handle_auth_failed(self) -> None:
        """The wall unit no longer accepts this remote: ask the user to re-pair."""
        if self.entry.data.get(CONF_USERNAME) != self._username:
            # The entry was re-paired and this account deleted on purpose; the
            # entry is reloading with the new one.
            return
        self.entry.async_start_reauth(self.hass)

    @callback
    def _handle_room_joined(self, room_jid: str) -> None:
        installation = installation_id(room_jid)
        ir.async_delete_issue(self.hass, DOMAIN, _room_issue_id(installation))
        self._migrate_unique_id(installation)

    @callback
    def _handle_room_denied(self, room_jid: str) -> None:
        installation = installation_id(room_jid)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _room_issue_id(installation),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_ROOM_ACCESS,
            translation_placeholders={"title": self.entry.title},
        )

    def _migrate_unique_id(self, installation: str) -> None:
        """Move an entry from a username-based unique ID to the installation.

        If pairing finished before the roster listed the wall unit, the entry
        got the generated username as unique ID, which can't tell the same
        installation being added twice.
        """
        unique_id = self.entry.unique_id
        if unique_id == installation or (
            unique_id is not None and not unique_id.startswith(USERNAME_PREFIX)
        ):
            return
        for other in self.hass.config_entries.async_entries(DOMAIN):
            if other.entry_id != self.entry_id and other.unique_id == installation:
                _LOGGER.warning(
                    "This eSMART installation is set up twice (%s and %s); "
                    "remove one of them",
                    self.entry.title,
                    other.title,
                )
                return
        _LOGGER.debug(
            "Migrating unique ID of %s to the installation ID", self.entry.title
        )
        self.hass.config_entries.async_update_entry(self.entry, unique_id=installation)

    # -- inbound routing ---------------------------------------------------

    @callback
    def _handle_message(
        self, method: str, msg_type: str, room_jid: str, body: Any
    ) -> None:
        if method != "INFO" or body is None:
            return
        if msg_type == "structure":
            self._handle_structure(room_jid, body)
        elif msg_type == "doors":
            self._handle_doors(room_jid, body)
        elif msg_type == "scene":
            self._handle_scene(room_jid, body)
        elif msg_type == "data":
            self._handle_data(room_jid, body)
        elif msg_type == "operation":
            self._handle_operation(room_jid, body)
        elif msg_type == "config":
            self._handle_config(room_jid, body)
        else:
            _LOGGER.debug("Ignoring INFO/%s from %s", msg_type, room_jid)

    def _handle_structure(self, room_jid: str, body: Any) -> None:
        """Discover light/cover/climate nodes from the apartment structure."""
        apartment = body.get("apartment") if isinstance(body, dict) else None
        if not isinstance(apartment, dict):
            _LOGGER.warning(
                "The wall unit sent a structure without an apartment; no devices "
                "were created from it"
            )
            _LOGGER.debug("Structure payload from %s: %s", room_jid, body)
            return
        self.raw_structure[room_jid] = body
        before = len(self.nodes)
        present: set[str] = set()
        for floor in _dicts(apartment.get("floors")):
            for room in _dicts(floor.get("rooms")):
                room_id = room.get("id") if isinstance(room.get("id"), int) else None
                room_name = str(room.get("name") or f"Room {room_id or ''}").strip()
                for node in _dicts(room.get("nodes")):
                    if uid := self._register_node(room_jid, room_name, room_id, node):
                        present.add(uid)
        _LOGGER.debug(
            "Parsed structure from %s: %d node(s) known (%d new)",
            room_jid,
            len(self.nodes),
            len(self.nodes) - before,
        )
        if not present:
            # An empty structure is far more likely a glitch than every device
            # being deleted at once, so don't wipe anything on it.
            _LOGGER.debug("Structure from %s listed no supported nodes", room_jid)
            return
        stale = [
            uid
            for uid, node in self.nodes.items()
            if node.room_jid == room_jid and uid not in present
        ]
        for uid in stale:
            node = self.nodes.pop(uid)
            _LOGGER.info("Removing %s, which the wall unit no longer lists", node.name)
            self._remove_entities(uid)
        if stale:
            self._remove_empty_devices()

    def _register_node(
        self, room_jid: str, room_name: str, room_id: int | None, node: dict[str, Any]
    ) -> str | None:
        """Add or update one structure node; returns its unique ID if supported."""
        kind = classify_node(node.get("type_id"), node.get("type_name"))
        node_id = node.get("id")
        if kind is None or not isinstance(node_id, int):
            _LOGGER.debug(
                "Skipping unsupported node in %s: id=%s type_id=%s type_name=%s",
                room_name,
                node_id,
                node.get("type_id"),
                node.get("type_name"),
            )
            return None

        name = str(node.get("name") or f"{kind.title()} {node_id}").strip()
        unique_id = f"{room_jid}_{node_id}"

        if (existing := self.nodes.get(unique_id)) is not None:
            existing.name = name
            existing.room_name = room_name
            existing.room_id = room_id
            return unique_id

        new_node = ESmartNode(
            room_jid=room_jid,
            node_id=node_id,
            name=name,
            room_name=room_name,
            room_id=room_id,
            kind=kind,
            # Entities fix their features when created, which can be before the
            # first telemetry, so take what the structure declares up front.
            dimmable=node.get("type_id") == NODE_TYPE_DIMMABLE_LIGHT,
            tiltable=node.get("has_orientation") is True,
            reverted=node.get("reverted") is True,
        )
        if new_node.reverted:
            _LOGGER.info(
                "Blind %s is marked as reversed on the wall unit. This hasn't been "
                "tested yet; please report whether it moves the right way",
                name,
            )
        # Replay any telemetry that arrived before this node was discovered.
        if (pending := self._pending_state.pop(unique_id, None)) is not None:
            new_node.apply_state(pending)

        self.nodes[unique_id] = new_node
        _LOGGER.debug("Discovered %s node %s (%s): %s", kind, name, unique_id, node)
        async_dispatcher_send(self.hass, signal_new_node(self.entry_id), new_node)
        return unique_id

    def _handle_config(self, room_jid: str, body: Any) -> None:
        """Pick up the heating setpoint range the wall unit allows."""
        limits = parse_setpoint_limits(body) if isinstance(body, dict) else None
        if limits is None:
            _LOGGER.debug("No usable setpoint limits in config from %s", room_jid)
            return
        if self._setpoint_limits.get(room_jid) == limits:
            return
        self._setpoint_limits[room_jid] = limits
        _LOGGER.debug("Setpoint limits for %s: %s", room_jid, limits)
        # Thermostats read their range from the hub, so refresh the ones here.
        for node in self.nodes.values():
            if node.kind == KIND_CLIMATE and node.room_jid == room_jid:
                async_dispatcher_send(
                    self.hass, signal_node_update(self.entry_id, node.unique_id)
                )

    def _handle_doors(self, room_jid: str, body: Any) -> None:
        doors = body.get("doors") if isinstance(body, dict) else None
        if not isinstance(doors, list):
            return
        listed: set[str] = set()
        for door in _dicts(doors):
            door_id = door.get("id")
            if not isinstance(door_id, int):
                continue
            unique_id = f"{room_jid}_door_{door_id}"
            listed.add(unique_id)
            name = str(door.get("name") or f"Door {door_id}").strip()
            if (existing := self.doors.get(unique_id)) is not None:
                existing.name = name
                continue
            new_door = ESmartDoor(room_jid=room_jid, door_id=door_id, name=name)
            self.doors[unique_id] = new_door
            _LOGGER.debug("Discovered door %s (%s)", new_door.name, unique_id)
            async_dispatcher_send(self.hass, signal_new_node(self.entry_id), new_door)
        # The list is complete, so a door missing from it was removed.
        for unique_id in [
            uid
            for uid, door in self.doors.items()
            if door.room_jid == room_jid and uid not in listed
        ]:
            _LOGGER.info("Removing door %s", self.doors.pop(unique_id).name)
            self._remove_entities(unique_id)

    def _handle_scene(self, room_jid: str, body: Any) -> None:
        """Sync scenes with the wall unit's list.

        Replies go to the whole room, so this also sees what other remotes asked
        for. We only ask for the summary, which lists every scene; a fullConfig
        reply has a different purpose, so it must not remove scenes.
        """
        scenes = body.get("scenes") if isinstance(body, dict) else None
        if not isinstance(scenes, list) or body.get("fullConfig"):
            return
        listed: set[str] = set()
        for scene in _dicts(scenes):
            scene_id = scene.get("id")
            num = scene.get("num")
            version = scene.get("version")
            if (
                not isinstance(scene_id, int)
                or not isinstance(num, int)
                or not isinstance(version, str)
            ):
                _LOGGER.debug("Skipping malformed scene from %s: %s", room_jid, scene)
                continue
            unique_id = f"{room_jid}_scene_{scene_id}"
            listed.add(unique_id)
            active = scene.get("active") is True
            name = str(scene.get("name") or f"Scene {num}").strip()

            if (existing := self.scenes.get(unique_id)) is not None:
                # Editing a scene changes its version, and the wall unit expects
                # the current one in CMD/scene.
                existing.num = num
                existing.name = name
                existing.version = version
                self._set_scene_active(existing, active)
            elif active:
                new_scene = ESmartScene(
                    room_jid=room_jid,
                    scene_id=scene_id,
                    num=num,
                    name=name,
                    version=version,
                )
                self.scenes[unique_id] = new_scene
                _LOGGER.debug("Discovered scene %s (%s): %s", name, unique_id, scene)
                async_dispatcher_send(
                    self.hass, signal_new_node(self.entry_id), new_scene
                )
        # A scene deleted on the wall unit just drops out of the list.
        for unique_id in [
            uid
            for uid, known in self.scenes.items()
            if known.room_jid == room_jid and uid not in listed
        ]:
            _LOGGER.info("Removing scene %s", self.scenes.pop(unique_id).name)
            self._remove_entities(unique_id)

    def _set_scene_active(self, scene: ESmartScene, active: bool) -> None:
        if scene.active != active:
            scene.active = active
            async_dispatcher_send(
                self.hass, signal_node_update(self.entry_id, scene.unique_id)
            )

    def _handle_data(self, room_jid: str, body: Any) -> None:
        if not isinstance(body, dict):
            return
        self._handle_meters(room_jid, body.get("meters"))
        nodes = body.get("nodes")
        updated = 0
        for node in _dicts(nodes):
            node_id = node.get("id")
            data = node.get("data")
            if not isinstance(node_id, int) or not isinstance(data, dict):
                continue
            if self._apply_state(room_jid, node_id, data, "data"):
                updated += 1
        _LOGGER.debug(
            "Data from %s: %d node(s), %d changed",
            room_jid,
            len(nodes) if isinstance(nodes, list) else 0,
            updated,
        )

    def _handle_meters(self, room_jid: str, entries: Any) -> None:
        for entry in _dicts(entries):
            meter_id = entry.get("id")
            if not isinstance(meter_id, int):
                continue
            for metric, value in parse_meter_readings(entry).items():
                self._apply_meter(room_jid, meter_id, metric, value, entry.get("name"))

    def _apply_meter(
        self, room_jid: str, meter_id: int, metric: str, value: float, name: Any
    ) -> None:
        unique_id = f"{room_jid}_meter_{meter_id}_{metric}"
        if (meter := self.meters.get(unique_id)) is not None:
            if meter.value != value:
                meter.value = value
                async_dispatcher_send(
                    self.hass, signal_node_update(self.entry_id, unique_id)
                )
            return

        new_meter = ESmartMeter(
            room_jid=room_jid,
            meter_id=meter_id,
            metric=metric,
            # Empty if the wall unit has no name for it; the sensor falls back.
            name=str(name or "").strip(),
            value=value,
        )
        self.meters[unique_id] = new_meter
        _LOGGER.debug("Discovered meter %s (%s): %s", new_meter.name, unique_id, value)
        async_dispatcher_send(self.hass, signal_new_node(self.entry_id), new_meter)

    def _handle_operation(self, room_jid: str, body: Any) -> None:
        """An operation acknowledgement echoes the new state of a single node."""
        op = body.get("operation") if isinstance(body, dict) else None
        if not isinstance(op, dict):
            op = body if isinstance(body, dict) else None
        if not isinstance(op, dict):
            return
        node_id = op.get("id")
        if isinstance(node_id, int):
            self._apply_state(room_jid, node_id, op, "operation")

    def _apply_state(
        self, room_jid: str, node_id: int, data: dict[str, Any], source: str
    ) -> bool:
        """Merge telemetry into a node. Returns True if its state changed."""
        unique_id = f"{room_jid}_{node_id}"
        node = self.nodes.get(unique_id)
        if node is None:
            # Telemetry can arrive before structure; we will reconcile once the
            # node is registered, so just remember the latest raw values here.
            # apply_state parses them then, and can't parse its own output.
            self._pending_state.setdefault(unique_id, {}).update(data)
            return False
        if node.kind == KIND_COVER:
            self._log_cover_state(node, data, source)
        if node.apply_state(data):
            async_dispatcher_send(
                self.hass, signal_node_update(self.entry_id, unique_id)
            )
            return True
        return False

    def _log_cover_state(
        self, node: ESmartNode, data: dict[str, Any], source: str
    ) -> None:
        """Log a cover's raw telemetry whenever any of it changes.

        This logs the raw payload because "power" is not part of the parsed node
        state. Neither value tracks movement: position jumps to the commanded
        target straight away, and power keeps its last reading after a run.
        """
        if self._cover_telemetry.get(node.unique_id) == data:
            return
        self._cover_telemetry[node.unique_id] = data
        _LOGGER.debug(
            "COVER state %s (id %s) via %s: %s (was position=%s orientation=%s)",
            node.name,
            node.node_id,
            source,
            data,
            node.position,
            node.orientation,
        )

    # -- removal -------------------------------------------------------------

    def _remove_entities(self, unique_id: str) -> None:
        """Tell the entities of a removed node, door or scene to go away."""
        async_dispatcher_send(self.hass, signal_node_removed(self.entry_id, unique_id))

    def _remove_empty_devices(self) -> None:
        """Drop room devices that no longer have any nodes."""
        device_registry = dr.async_get(self.hass)
        current = self.current_device_identifiers()
        for device in dr.async_entries_for_config_entry(device_registry, self.entry_id):
            identifiers = {i for i in device.identifiers if i[0] == DOMAIN}
            if not identifiers or identifiers & current:
                continue
            _LOGGER.debug("Removing empty eSMART device %s", device.name)
            device_registry.async_remove_device(device.id)

    def current_device_identifiers(self) -> set[tuple[str, str]]:
        """Identifiers of every device the hub currently provides entities for."""
        identifiers = {(DOMAIN, self.entry_id)}  # the controller
        identifiers.update(
            (DOMAIN, f"{self.entry_id}_{node.device_id}")
            for node in self.nodes.values()
        )
        return identifiers

    # -- outbound (used by entities) --------------------------------------

    def _command(self, func: Any, *args: Any) -> None:
        """Run a client command, turning a dropped connection into a user error."""
        try:
            func(*args)
        except NotConnected as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            ) from err

    def set_onoff(self, node: ESmartNode, on: bool) -> None:
        self._command(self._client.set_onoff, node.room_jid, node.node_id, on)

    def set_dimming(self, node: ESmartNode, percent: int) -> None:
        self._command(self._client.set_dimming, node.room_jid, node.node_id, percent)

    def set_position(self, node: ESmartNode, position: int) -> None:
        orientation = node.orientation
        # Blinds without slats have no orientation. For the rest, carrying the
        # current orientation to either end of travel appears to leave them short
        # of fully open or closed, so send the slat value for that end instead.
        if orientation is not None and position in (0, POSITION_MAX):
            orientation = position
        self._command(
            self._client.set_position,
            node.room_jid,
            node.node_id,
            position,
            orientation,
        )

    def set_orientation(self, node: ESmartNode, orientation: int) -> None:
        self._command(
            self._client.set_orientation,
            node.room_jid,
            node.node_id,
            orientation,
            node.position,
        )

    def stop_node(self, node: ESmartNode) -> None:
        self._command(self._client.stop_node, node.room_jid, node.node_id)

    def set_setpoint(self, node: ESmartNode, temp: float) -> float:
        """Send a setpoint snapped to what the wall unit accepts; returns it."""
        snapped = self.setpoint_limits(node.room_jid).snap(temp)
        self._command(self._client.set_setpoint, node.room_jid, node.node_id, snapped)
        return snapped

    def door_pulse(self, door: ESmartDoor) -> None:
        self._command(self._client.door_pulse, door.room_jid, door.door_id)

    def activate_scene(self, scene: ESmartScene) -> None:
        self._command(
            self._client.activate_scene, scene.room_jid, scene.num, scene.version
        )


def _room_issue_id(installation: str) -> str:
    return f"{ISSUE_ROOM_ACCESS}_{installation}"


def _dicts(value: Any) -> Iterator[dict[str, Any]]:
    """The dict items of a list from the wire, skipping anything malformed."""
    if isinstance(value, list):
        yield from (item for item in value if isinstance(item, dict))
