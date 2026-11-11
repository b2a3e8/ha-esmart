"""Pure eSMART protocol helpers: the node model, classification and payloads.

This module deliberately has no Home Assistant or XMPP dependencies so the
wire format can be reasoned about (and unit tested) in isolation. The XMPP
client in :mod:`client` wraps these payloads in stanzas, and the hub in
:mod:`coordinator` turns parsed messages into entity state.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
from typing import Any

from .const import (
    AWNING_KEYWORDS,
    DEFAULT_SETPOINT_MAX,
    DEFAULT_SETPOINT_MIN,
    DEFAULT_SETPOINT_STEP,
    KIND_CLIMATE,
    KIND_COVER,
    KIND_DOOR,
    KIND_LIGHT,
    KIND_METER,
    KIND_SCENE,
    METRIC_FLOW_RATE,
    METRIC_POWER,
    METRIC_TEMPERATURE,
    NODE_TYPE_DIMMABLE_LIGHT,
    NODE_TYPE_LIGHT,
    NODE_TYPE_SHUTTER,
    NODE_TYPE_THERMOSTAT,
    PROTOCOL_VERSION,
    XMPP_DOMAIN,
)


def classify_node(type_id: Any, type_name: Any) -> str | None:
    """Map a structure node to one of our entity kinds, or ``None`` to skip."""
    name = str(type_name or "").lower()
    if type_id in (NODE_TYPE_LIGHT, NODE_TYPE_DIMMABLE_LIGHT) or "lamp" in name:
        return KIND_LIGHT
    if type_id == NODE_TYPE_SHUTTER or "store" in name or "shutter" in name:
        return KIND_COVER
    if type_id == NODE_TYPE_THERMOSTAT or "heating" in name:
        return KIND_CLIMATE
    return None


def is_awning(name: str) -> bool:
    """Guess from a cover's name whether it is an awning rather than a blind."""
    lowered = name.lower()
    return any(keyword in lowered for keyword in AWNING_KEYWORDS)


def installation_id(room_jid: str) -> str:
    """The installation ID of a device room (``<id>@conference.<domain>``)."""
    return room_jid.split("@", 1)[0]


def device_jid(room_jid: str) -> str:
    """The wall unit's own JID for a device room."""
    return f"{installation_id(room_jid)}@{XMPP_DOMAIN}"


@dataclass
class ESmartNode:
    """A controllable eSMART node (light, cover or climate) and its state."""

    room_jid: str
    node_id: int
    name: str
    room_name: str
    kind: str

    # Dynamic state, populated from ``data``/``operation`` messages.
    onoff: bool | None = None
    dimming: int | None = None  # 0..100 %
    position: int | None = None  # 0..1024 raw ticks
    orientation: int | None = None  # 0..1024 raw ticks
    setpoint: float | None = None
    temperature: float | None = None
    device_onoff: bool | None = None
    power: int | float | None = None  # W

    # Last non-zero brightness, so turning a dimmer back on restores it.
    last_dimming: int | None = None

    # Whether we have ever seen the corresponding capability in telemetry.
    dimmable: bool = False
    tiltable: bool = False
    # From the structure: whether the blind's motor direction is reversed. Kept
    # for diagnostics; the wall unit appears to handle it itself.
    reverted: bool = False

    @property
    def unique_id(self) -> str:
        return f"{self.room_jid}_{self.node_id}"

    def apply_state(self, data: dict[str, Any]) -> bool:
        """Merge a parsed ``data`` payload. Returns ``True`` if anything changed."""
        changed = False
        for attr, value in parse_node_data(data).items():
            # A blind keeps its last power reading after the motor stops.
            if attr == "power" and self.kind == KIND_COVER:
                continue
            if getattr(self, attr) != value:
                setattr(self, attr, value)
                changed = True
            if attr == "dimming" and value > 0:
                self.last_dimming = value
            if attr == "dimming" and not self.dimmable:
                self.dimmable = True
                changed = True
            if attr == "orientation" and not self.tiltable:
                self.tiltable = True
                changed = True
        return changed


@dataclass(frozen=True)
class SetpointLimits:
    """The heating setpoint range and step the wall unit accepts."""

    min_temp: float = DEFAULT_SETPOINT_MIN
    max_temp: float = DEFAULT_SETPOINT_MAX
    step: float = DEFAULT_SETPOINT_STEP

    def snap(self, temp: float) -> float:
        """Round to the nearest allowed step (halves up) and clamp to the range."""
        steps = math.floor((temp - self.min_temp) / self.step + 0.5)
        snapped = self.min_temp + steps * self.step
        # Rounding to 2 places drops float noise like 21.000000000000004.
        return round(max(self.min_temp, min(self.max_temp, snapped)), 2)


def parse_setpoint_limits(config: dict[str, Any]) -> SetpointLimits | None:
    """Read the setpoint limits from an ``INFO/config`` body.

    Returns ``None`` if the config doesn't carry a usable set, so the caller
    keeps whatever limits it had.
    """
    values = [
        config.get(key)
        for key in ("setpoint_min_temp", "setpoint_max_temp", "setpoint_increment")
    ]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return None
    min_temp, max_temp, step = (float(v) for v in values)
    if step <= 0 or min_temp >= max_temp:
        return None
    return SetpointLimits(min_temp=min_temp, max_temp=max_temp, step=step)


@dataclass
class ESmartDoor:
    """An entrance-door relay that accepts a momentary "pulse" command."""

    room_jid: str
    door_id: int
    name: str
    kind: str = field(default=KIND_DOOR)

    @property
    def unique_id(self) -> str:
        return f"{self.room_jid}_door_{self.door_id}"


@dataclass
class ESmartScene:
    """A scene stored on the wall unit.

    ``active`` means the scene is enabled, not that it is currently running.
    ``version`` has to be sent back when activating the scene.
    """

    room_jid: str
    scene_id: int
    num: int
    name: str
    version: str
    active: bool = True
    kind: str = field(default=KIND_SCENE)

    @property
    def unique_id(self) -> str:
        return f"{self.room_jid}_scene_{self.scene_id}"


@dataclass
class ESmartMeter:
    """A single reading of an apartment meter.

    Meter ids are not unique on their own: hot water reports ``power`` and
    ``flow_rate`` as two entries with the same id.
    """

    room_jid: str
    meter_id: int
    metric: str
    name: str
    value: int | float | None = None
    kind: str = field(default=KIND_METER)

    @property
    def unique_id(self) -> str:
        return f"{self.room_jid}_meter_{self.meter_id}_{self.metric}"


def parse_node_data(data: dict[str, Any]) -> dict[str, Any]:
    """Extract the fields we care about from a node's ``data`` dict."""
    out: dict[str, Any] = {}
    if (onoff := data.get("onoff")) in ("on", "off"):
        out["onoff"] = onoff == "on"
    if isinstance(dimming := data.get("dimming"), str):
        with contextlib.suppress(ValueError):
            out["dimming"] = max(0, min(100, int(dimming.rstrip("%"))))
    if isinstance(pos := data.get("position"), (int, float)):
        out["position"] = int(pos)
    if isinstance(orient := data.get("orientation"), (int, float)):
        out["orientation"] = int(orient)
    if (device_onoff := data.get("deviceOnOff")) in ("on", "off"):
        out["device_onoff"] = device_onoff == "on"
    if isinstance(setpoint := data.get("setpoint"), (int, float)):
        out["setpoint"] = float(setpoint)
    if isinstance(temperature := data.get("temperature"), (int, float)):
        out["temperature"] = float(temperature)
    if isinstance(power := data.get("power"), (int, float)):
        out["power"] = power
    return out


def parse_meter_readings(entry: dict[str, Any]) -> dict[str, int | float]:
    """Extract the readings from one entry of a ``data`` message's meters."""
    return {
        metric: entry[metric]
        for metric in (METRIC_POWER, METRIC_FLOW_RATE, METRIC_TEMPERATURE)
        if isinstance(entry.get(metric), (int, float))
    }


def _timestamp() -> str:
    """Format a timestamp the way the wall unit expects (``... +0000``)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S+0000")


def build_message(
    *, from_jid: str, method: str, msg_type: str, body: Any
) -> dict[str, Any]:
    """Assemble a full eSMART JSON message envelope."""
    size = len(body) if isinstance(body, (list, dict)) else 1
    return {
        "headers": {
            "method": method,
            "type": msg_type,
            "version": PROTOCOL_VERSION,
            "to": "master",
            "from": from_jid,
            "timestamp": _timestamp(),
            "size": str(size),
        },
        "body": body,
    }


def build_get(*, from_jid: str, msg_type: str, body: dict[str, Any]) -> dict[str, Any]:
    """Build a GET request used to pull initial state from a room."""
    return build_message(from_jid=from_jid, method="GET", msg_type=msg_type, body=body)


def build_operation(*, from_jid: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a CMD/operation message carrying one or more node operations."""
    return build_message(
        from_jid=from_jid, method="CMD", msg_type="operation", body=items
    )


def build_door_pulse(*, from_jid: str, door_id: int) -> dict[str, Any]:
    """Build a CMD/door message that pulses an entrance-door relay."""
    return build_message(
        from_jid=from_jid,
        method="CMD",
        msg_type="door",
        body={"id": door_id, "action": "pulse"},
    )


def build_scene_activation(*, from_jid: str, num: int, version: str) -> dict[str, Any]:
    """Build a CMD/scene message that runs a scene."""
    return build_message(
        from_jid=from_jid,
        method="CMD",
        msg_type="scene",
        body={"num": num, "version": version},
    )
