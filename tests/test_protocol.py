"""Tests for the pure wire-format helpers."""

from __future__ import annotations

import pytest

from custom_components.esmart.const import (
    KIND_CLIMATE,
    KIND_COVER,
    KIND_LIGHT,
    PROTOCOL_VERSION,
)
from custom_components.esmart.protocol import (
    ESmartNode,
    SetpointLimits,
    build_door_pulse,
    build_get,
    build_operation,
    build_scene_activation,
    classify_node,
    device_jid,
    installation_id,
    is_awning,
    parse_meter_readings,
    parse_node_data,
    parse_setpoint_limits,
)

from .conftest import INSTALLATION, ROOM


@pytest.mark.parametrize(
    ("type_id", "type_name", "kind"),
    [
        (2, "Lampe", KIND_LIGHT),
        (3, "Lampe Dim.", KIND_LIGHT),
        # Dimmers must not depend on the localised type name.
        (3, "Variateur", KIND_LIGHT),
        (4, "Store", KIND_COVER),
        (6, "Heating", KIND_CLIMATE),
        (None, "Lamp", KIND_LIGHT),
        (None, "Shutter", KIND_COVER),
        (11, "Ventilation", None),
        (None, None, None),
    ],
)
def test_classify_node(
    type_id: int | None, type_name: str | None, kind: str | None
) -> None:
    assert classify_node(type_id, type_name) == kind


@pytest.mark.parametrize(
    ("name", "awning"),
    [
        ("Markise 1", True),
        ("Terrace awning", True),
        ("Store banne", True),
        ("Store 1", False),
    ],
)
def test_is_awning(name: str, awning: bool) -> None:
    assert is_awning(name) is awning


def test_installation_ids() -> None:
    assert installation_id(ROOM) == INSTALLATION
    assert device_jid(ROOM) == f"{INSTALLATION}@myesmart.net"


def test_parse_node_data() -> None:
    assert parse_node_data(
        {
            "onoff": "on",
            "dimming": "60%",
            "position": 512,
            "orientation": 100.0,
            "deviceOnOff": "off",
            "setpoint": 21,
            "temperature": 20.5,
            "power": 12,
        }
    ) == {
        "onoff": True,
        "dimming": 60,
        "position": 512,
        "orientation": 100,
        "device_onoff": False,
        "setpoint": 21.0,
        "temperature": 20.5,
        "power": 12,
    }


def test_parse_node_data_ignores_garbage() -> None:
    assert (
        parse_node_data(
            {"onoff": "maybe", "dimming": "lots", "position": "1", "setpoint": None}
        )
        == {}
    )
    assert parse_node_data({"dimming": "150%"}) == {"dimming": 100}


def test_parse_meter_readings() -> None:
    assert parse_meter_readings({"id": 3, "power": 0, "name": "x"}) == {"power": 0}
    assert parse_meter_readings({"id": 3, "flow_rate": 4}) == {"flow_rate": 4}
    assert parse_meter_readings({"id": 3, "power": "1"}) == {}


def _node(**kwargs: object) -> ESmartNode:
    return ESmartNode(room_jid=ROOM, node_id=1, name="n", room_name="r", **kwargs)


def test_apply_state_tracks_capabilities_and_brightness() -> None:
    node = _node(kind=KIND_LIGHT)
    assert node.apply_state({"dimming": "40%"})
    assert node.dimmable
    assert node.last_dimming == 40
    assert node.apply_state({"dimming": "0%"})
    assert node.last_dimming == 40  # zero is "off", not a brightness
    assert not node.apply_state({"dimming": "0%"})


def test_apply_state_ignores_cover_power() -> None:
    node = _node(kind=KIND_COVER)
    assert node.apply_state({"orientation": 10, "power": 5})
    assert node.tiltable
    assert node.power is None


@pytest.mark.parametrize(
    ("temp", "snapped"),
    [(21.0, 21.0), (21.4, 21.0), (21.5, 22.0), (10.0, 18.0), (30.0, 24.0)],
)
def test_setpoint_snap(temp: float, snapped: float) -> None:
    assert SetpointLimits().snap(temp) == snapped


def test_setpoint_snap_half_steps() -> None:
    assert SetpointLimits(16.0, 26.0, 0.5).snap(21.3) == 21.5


def test_parse_setpoint_limits() -> None:
    assert parse_setpoint_limits(
        {"setpoint_min_temp": 16, "setpoint_max_temp": 26, "setpoint_increment": 0.5}
    ) == SetpointLimits(16.0, 26.0, 0.5)
    assert parse_setpoint_limits({"setpoint_min_temp": 16}) is None
    assert (
        parse_setpoint_limits(
            {"setpoint_min_temp": 26, "setpoint_max_temp": 16, "setpoint_increment": 1}
        )
        is None
    )
    assert (
        parse_setpoint_limits(
            {
                "setpoint_min_temp": 16,
                "setpoint_max_temp": 26,
                "setpoint_increment": True,
            }
        )
        is None
    )


def test_message_builders() -> None:
    get = build_get(from_jid="me@x", msg_type="structure", body={})
    assert get["headers"]["method"] == "GET"
    assert get["headers"]["type"] == "structure"
    assert get["headers"]["to"] == "master"
    assert get["headers"]["version"] == PROTOCOL_VERSION
    assert get["headers"]["timestamp"].endswith("+0000")

    op = build_operation(from_jid="me@x", items=[{"id": 1}, {"id": 2}])
    assert op["headers"]["method"] == "CMD"
    assert op["headers"]["size"] == "2"
    assert op["body"] == [{"id": 1}, {"id": 2}]

    assert build_door_pulse(from_jid="me@x", door_id=0)["body"] == {
        "id": 0,
        "action": "pulse",
    }
    assert build_scene_activation(from_jid="me@x", num=2, version="v")["body"] == {
        "num": 2,
        "version": "v",
    }
