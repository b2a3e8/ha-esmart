"""Tests for the entities created from the wall unit's messages."""

from __future__ import annotations

from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
)
from homeassistant.components.light import ATTR_BRIGHTNESS
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    ATTR_TEMPERATURE,
    STATE_CLOSED,
    STATE_OFF,
    STATE_ON,
    STATE_OPEN,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.esmart.const import DOMAIN

from .conftest import ROOM, FakeClient, load_fixture, setup_integration


def eid(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, f"no {platform} entity for {unique_id}"
    return entity_id


def device(hass: HomeAssistant, identifier: str) -> dr.DeviceEntry | None:
    """The eSMART device with this identifier."""
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), client_entry_id(hass)
    )


def node_device(hass: HomeAssistant, node_id: int) -> dr.DeviceEntry | None:
    return device(hass, f"{ROOM}_{node_id}")


def state(hass: HomeAssistant, platform: str, unique_id: str) -> State:
    entity_state = hass.states.get(eid(hass, platform, unique_id))
    assert entity_state is not None
    return entity_state


async def call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data):
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
    )


@pytest.fixture
async def client(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> FakeClient:
    return await setup_integration(hass, config_entry)


async def test_entities_are_created(hass: HomeAssistant, client: FakeClient) -> None:
    registry = er.async_get(hass)
    platforms = sorted(entry.domain for entry in registry.entities.values())
    assert platforms == sorted(
        ["light"] * 3
        + ["cover"] * 2
        + ["climate"] * 2
        + ["button"]
        + ["scene"] * 2
        # node power: 3 lights + 2 heatings; meters: 2 power, 1 flow, 1 temp
        + ["sensor"] * 9
    )
    # The unsupported ventilation node and the inactive scene are skipped.
    assert registry.async_get_entity_id("scene", DOMAIN, f"{ROOM}_scene_3") is None

    light = node_device(hass, 9)
    assert light is not None
    assert light.name == "Ceiling light"
    assert light.model == "eSMART Light"
    controller = device(hass, client_entry_id(hass))
    assert controller is not None
    assert light.via_device_id == controller.id
    area = ar.async_get(hass).async_get_area(light.area_id)
    assert area is not None
    assert area.name == "Living room"


def client_entry_id(hass: HomeAssistant) -> str:
    return hass.config_entries.async_entries(DOMAIN)[0].entry_id


async def test_light_states(hass: HomeAssistant, client: FakeClient) -> None:
    assert state(hass, "light", f"{ROOM}_9").state == STATE_ON
    assert state(hass, "light", f"{ROOM}_13").state == STATE_OFF
    dimmer = state(hass, "light", f"{ROOM}_42")
    assert dimmer.state == STATE_ON
    assert dimmer.attributes[ATTR_BRIGHTNESS] == 153


async def test_light_commands(hass: HomeAssistant, client: FakeClient) -> None:
    light = eid(hass, "light", f"{ROOM}_9")
    dimmer = eid(hass, "light", f"{ROOM}_42")

    await call(hass, "light", "turn_off", light)
    await call(hass, "light", "turn_on", dimmer, brightness=255)
    await call(hass, "light", "turn_off", dimmer)
    assert hass.states.get(dimmer).state == STATE_OFF
    # Turning on without a brightness restores the last one.
    await call(hass, "light", "turn_on", dimmer)
    assert client.commands == [
        ("onoff", 9, False),
        ("dimming", 42, 100),
        ("dimming", 42, 0),
        ("dimming", 42, 100),
    ]
    assert hass.states.get(light).state == STATE_OFF


async def test_cover_states(hass: HomeAssistant, client: FakeClient) -> None:
    blind = state(hass, "cover", f"{ROOM}_49")
    assert blind.state == STATE_CLOSED
    assert blind.attributes[ATTR_CURRENT_POSITION] == 0
    assert blind.attributes[ATTR_CURRENT_TILT_POSITION] == 50
    assert blind.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.SHUTTER

    awning = state(hass, "cover", f"{ROOM}_57")
    assert awning.state == STATE_OPEN
    assert awning.attributes[ATTR_DEVICE_CLASS] == CoverDeviceClass.AWNING
    assert ATTR_CURRENT_TILT_POSITION not in awning.attributes


async def test_cover_commands(hass: HomeAssistant, client: FakeClient) -> None:
    blind = eid(hass, "cover", f"{ROOM}_49")
    await call(hass, "cover", "open_cover", blind)
    await call(hass, "cover", "close_cover", blind)
    await call(hass, "cover", "set_cover_position", blind, **{ATTR_POSITION: 25})
    await call(hass, "cover", "stop_cover", blind)
    await call(
        hass, "cover", "set_cover_tilt_position", blind, **{ATTR_TILT_POSITION: 25}
    )
    await call(hass, "cover", "open_cover_tilt", blind)
    await call(hass, "cover", "close_cover_tilt", blind)
    assert client.commands == [
        # Full travel takes the slats to the matching end.
        ("position", 49, 0, 0),
        ("position", 49, 1024, 1024),
        ("position", 49, 768, 512),
        ("stop", 49),
        ("orientation", 49, 256, 1024),
        ("orientation", 49, 1024, 1024),
        ("orientation", 49, 0, 1024),
    ]


async def test_climate_states(hass: HomeAssistant, client: FakeClient) -> None:
    living = state(hass, "climate", f"{ROOM}_26")
    assert living.state == HVACMode.HEAT
    assert living.attributes[ATTR_TEMPERATURE] == 21
    assert living.attributes["current_temperature"] == 20.5
    assert living.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert living.attributes["min_temp"] == 18
    assert living.attributes["max_temp"] == 24
    assert living.attributes["target_temp_step"] == 1
    bedroom = state(hass, "climate", f"{ROOM}_25")
    assert bedroom.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE


async def test_climate_commands(hass: HomeAssistant, client: FakeClient) -> None:
    climate = eid(hass, "climate", f"{ROOM}_26")
    await call(hass, "climate", "set_temperature", climate, temperature=21.4)
    assert hass.states.get(climate).attributes[ATTR_TEMPERATURE] == 21
    await call(hass, "climate", "set_hvac_mode", climate, **{ATTR_HVAC_MODE: "off"})
    current = hass.states.get(climate)
    assert current.state == HVACMode.OFF
    assert current.attributes[ATTR_HVAC_ACTION] == HVACAction.OFF
    assert client.commands == [("setpoint", 26, 21.0), ("onoff", 26, False)]


async def test_climate_follows_the_wall_units_limits(
    hass: HomeAssistant, client: FakeClient
) -> None:
    client.send(
        "config",
        {"setpoint_min_temp": 16, "setpoint_max_temp": 26, "setpoint_increment": 0.5},
    )
    await hass.async_block_till_done()
    climate = state(hass, "climate", f"{ROOM}_26")
    assert climate.attributes["min_temp"] == 16
    assert climate.attributes["target_temp_step"] == 0.5


async def test_climate_mode_survives_restart(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    # Home Assistant puts area and device name into new entity IDs.
    entity_id = "climate.living_room_living_room_heating"
    mock_restore_cache(hass, [State(entity_id, HVACMode.OFF)])
    await setup_integration(hass, config_entry)
    assert eid(hass, "climate", f"{ROOM}_26") == entity_id
    assert hass.states.get(entity_id).state == HVACMode.OFF


async def test_sensors(hass: HomeAssistant, client: FakeClient) -> None:
    power = state(hass, "sensor", f"{ROOM}_9_power")
    assert power.state == "12"
    assert power.attributes[ATTR_FRIENDLY_NAME] == "Ceiling light Power"

    flow = state(hass, "sensor", f"{ROOM}_meter_3_flow_rate")
    assert flow.state == "4"
    assert (
        flow.attributes[ATTR_FRIENDLY_NAME] == "eSMART Controller Hot water flow rate"
    )

    unnamed = state(hass, "sensor", f"{ROOM}_meter_8_temperature")
    assert unnamed.state == "21.5"
    assert unnamed.attributes[ATTR_FRIENDLY_NAME] == "eSMART Controller Temperature 8"

    client.send("data", {"meters": [{"id": 0, "power": 700, "name": "Total energy"}]})
    await hass.async_block_till_done()
    assert state(hass, "sensor", f"{ROOM}_meter_0_power").state == "700"


async def test_door_and_scene(hass: HomeAssistant, client: FakeClient) -> None:
    await call(hass, "button", "press", eid(hass, "button", f"{ROOM}_door_0"))
    await call(hass, "scene", "turn_on", eid(hass, "scene", f"{ROOM}_scene_2"))
    assert client.commands == [
        ("door", 0),
        ("scene", 2, "2018-02-06 09:28:24+0000"),
    ]


async def test_scene_deactivated_on_the_wall_unit(
    hass: HomeAssistant, client: FakeClient
) -> None:
    scenes = load_fixture("scenes.json")
    scenes["scenes"][1]["active"] = False
    client.send("scene", scenes)
    await hass.async_block_till_done()
    assert state(hass, "scene", f"{ROOM}_scene_2").state == STATE_UNAVAILABLE


async def test_disconnect_makes_entities_unavailable(
    hass: HomeAssistant, client: FakeClient
) -> None:
    light = eid(hass, "light", f"{ROOM}_9")
    client.drop()
    await hass.async_block_till_done()
    assert hass.states.get(light).state == STATE_UNAVAILABLE


async def test_command_while_disconnected_raises(
    hass: HomeAssistant, client: FakeClient
) -> None:
    """Regression: commands used to vanish with only a log line."""
    light = eid(hass, "light", f"{ROOM}_9")
    client.session_ready = False  # dropped, but not noticed by the hub yet
    with pytest.raises(HomeAssistantError, match="Not connected"):
        await call(hass, "light", "turn_off", light)
    # The optimistic state is not applied.
    assert hass.states.get(light).state == STATE_ON


async def test_telemetry_before_structure(
    hass: HomeAssistant, fake_client: type[FakeClient], config_entry: MockConfigEntry
) -> None:
    client = await setup_integration(hass, config_entry, replay=False)
    client.send("data", load_fixture("data.json"))
    client.send("structure", load_fixture("structure.json"))
    await hass.async_block_till_done()
    assert state(hass, "light", f"{ROOM}_9").state == STATE_ON


async def test_removed_nodes_are_cleaned_up(
    hass: HomeAssistant, client: FakeClient
) -> None:
    """Regression: devices deleted on the wall unit used to stay forever."""
    structure = load_fixture("structure.json")
    rooms = structure["apartment"]["floors"][0]["rooms"]
    del rooms[1]  # the bedroom
    client.send("structure", structure)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for platform, uid in (
        ("light", f"{ROOM}_13"),
        ("climate", f"{ROOM}_25"),
        ("sensor", f"{ROOM}_13_power"),
    ):
        assert registry.async_get_entity_id(platform, DOMAIN, uid) is None
        assert hass.states.async_entity_ids(platform)  # the others remain
    assert node_device(hass, 13) is None
    assert node_device(hass, 25) is None
    assert node_device(hass, 9) is not None

    # A node that comes back is added again.
    client.send("structure", load_fixture("structure.json"))
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("light", DOMAIN, f"{ROOM}_13") is not None


async def test_empty_structure_removes_nothing(
    hass: HomeAssistant, client: FakeClient
) -> None:
    structure = load_fixture("structure.json")
    structure["apartment"]["floors"] = []
    client.send("structure", structure)
    client.send("structure", {"no": "apartment"})
    await hass.async_block_till_done()
    assert eid(hass, "light", f"{ROOM}_13")


async def test_removed_doors_and_scenes_are_cleaned_up(
    hass: HomeAssistant, client: FakeClient
) -> None:
    scenes = load_fixture("scenes.json")
    del scenes["scenes"][0]
    client.send("scene", scenes)
    client.send("doors", {"doors": []})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("scene", DOMAIN, f"{ROOM}_scene_1") is None
    assert registry.async_get_entity_id("button", DOMAIN, f"{ROOM}_door_0") is None
    assert eid(hass, "scene", f"{ROOM}_scene_2")


async def test_full_config_scene_reply_removes_nothing(
    hass: HomeAssistant, client: FakeClient
) -> None:
    client.send("scene", {"fullConfig": True, "scenes": []})
    await hass.async_block_till_done()
    assert eid(hass, "scene", f"{ROOM}_scene_1")
