"""Sensor platform for eSMART meters and per-node power readings."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_COVER, METRIC_FLOW_RATE, METRIC_POWER, METRIC_TEMPERATURE
from .coordinator import ESmartConfigEntry, ESmartHub, ESmartItem
from .entity import ESmartMeterEntity, ESmartNodeEntity, async_setup_platform_entities
from .protocol import ESmartMeter, ESmartNode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ESmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors as meters and nodes are discovered."""

    def _factory(hub: ESmartHub, item: ESmartItem) -> list[Entity]:
        if isinstance(item, ESmartMeter):
            return [ESmartMeterSensor(hub, item)]
        # Blinds keep their last power reading after the motor stops, so their
        # power isn't worth a sensor.
        if isinstance(item, ESmartNode) and item.kind != KIND_COVER:
            return [ESmartNodePowerSensor(hub, item)]
        return []

    async_setup_platform_entities(hass, entry, async_add_entities, _factory)


class ESmartMeterSensor(ESmartMeterEntity, SensorEntity):
    """One reading of an apartment meter.

    ``power`` is instantaneous watts on every meter type: the total meter is the
    sum of the electricity, heating, hot water and cooling meters. The Wh and dL
    in the structure describe cumulative counters, which ``data`` doesn't carry.
    The unit of ``flow_rate`` isn't stated anywhere, so it stays a bare number.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: ESmartHub, meter: ESmartMeter) -> None:
        if meter.name and meter.metric == METRIC_FLOW_RATE:
            # Keeps it apart from the same meter's power reading.
            name = None
            self._attr_translation_key = "meter_flow_rate"
            self._attr_translation_placeholders = {"name": meter.name}
        elif meter.name:
            name = meter.name
        else:
            name = None
            self._attr_translation_key = f"meter_{meter.metric}_unnamed"
            self._attr_translation_placeholders = {"id": str(meter.meter_id)}
        super().__init__(hub, meter, name)
        if meter.metric == METRIC_POWER:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
        elif meter.metric == METRIC_TEMPERATURE:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    @property
    def native_value(self) -> int | float | None:
        return self.meter.value


class ESmartNodePowerSensor(ESmartNodeEntity, SensorEntity):
    """Power drawn by a lamp or heating node."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "node_power"

    def __init__(self, hub: ESmartHub, node: ESmartNode) -> None:
        super().__init__(hub, node)
        self._attr_unique_id = f"{node.unique_id}_power"
        self._attr_translation_placeholders = {"name": node.name}

    @property
    def native_value(self) -> int | float | None:
        return self.node.power
