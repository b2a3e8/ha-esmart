"""Climate platform for eSMART heating nodes."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import KIND_CLIMATE
from .coordinator import ESmartConfigEntry, ESmartHub, ESmartItem
from .entity import ESmartNodeEntity, async_setup_platform_entities
from .protocol import ESmartNode

# Commands are queued and coalesced by the client; don't serialize them here.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ESmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up thermostats as they are discovered."""

    def _factory(hub: ESmartHub, item: ESmartItem) -> list[Entity]:
        if isinstance(item, ESmartNode) and item.kind == KIND_CLIMATE:
            return [ESmartClimate(hub, item)]
        return []

    async_setup_platform_entities(hass, entry, async_add_entities, _factory)


class ESmartClimate(ESmartNodeEntity, ClimateEntity, RestoreEntity):
    """An eSMART heating valve, exposed as a heat/off thermostat."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]  # noqa: RUF012 - HA convention
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hub: ESmartHub, node: ESmartNode) -> None:
        super().__init__(hub, node, node.name)
        # The wall unit reports whether the valve is heating, but not whether
        # heating is switched on, so the mode is tracked here and restored
        # across restarts. Telemetry wins if it ever carries "onoff".
        self._hvac_mode = HVACMode.HEAT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state in (
            HVACMode.OFF,
            HVACMode.HEAT,
        ):
            self._hvac_mode = HVACMode(last.state)

    # The range comes from the wall unit's config, which can arrive after the
    # entity is created; the hub refreshes us when it does.
    @property
    def min_temp(self) -> float:
        return self.hub.setpoint_limits(self.node.room_jid).min_temp

    @property
    def max_temp(self) -> float:
        return self.hub.setpoint_limits(self.node.room_jid).max_temp

    @property
    def target_temperature_step(self) -> float:
        return self.hub.setpoint_limits(self.node.room_jid).step

    @property
    def current_temperature(self) -> float | None:
        return self.node.temperature

    @property
    def target_temperature(self) -> float | None:
        return self.node.setpoint

    @property
    def hvac_mode(self) -> HVACMode:
        if self.node.onoff is not None:
            return HVACMode.HEAT if self.node.onoff else HVACMode.OFF
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self.node.device_onoff is None:
            return None
        return HVACAction.HEATING if self.node.device_onoff else HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            # Show the snapped value that was sent, not e.g. 21.5 the wall
            # unit can't take; reconciled on telemetry.
            self.node.setpoint = self.hub.set_setpoint(self.node, float(temp))
            self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        on = hvac_mode != HVACMode.OFF
        self.hub.set_onoff(self.node, on)
        self._hvac_mode = hvac_mode
        if self.node.onoff is not None:
            self.node.onoff = on
        self.async_write_ha_state()
