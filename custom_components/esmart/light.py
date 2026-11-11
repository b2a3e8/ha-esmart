"""Light platform for eSMART lamp nodes."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_LIGHT
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
    """Set up lights as they are discovered."""

    def _factory(hub: ESmartHub, item: ESmartItem) -> list[Entity]:
        if isinstance(item, ESmartNode) and item.kind == KIND_LIGHT:
            return [ESmartLight(hub, item)]
        return []

    async_setup_platform_entities(hass, entry, async_add_entities, _factory)


def _to_percent(brightness: int) -> int:
    return round(brightness / 255 * 100)


class ESmartLight(ESmartNodeEntity, LightEntity):
    """An eSMART lamp, dimmable when the wall unit reports a dimming value."""

    _attr_name = None  # the node's main feature, named after its device

    def __init__(self, hub: ESmartHub, node: ESmartNode) -> None:
        super().__init__(hub, node)
        mode = ColorMode.BRIGHTNESS if node.dimmable else ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    @property
    def is_on(self) -> bool | None:
        if self.node.dimmable and self.node.dimming is not None:
            return self.node.dimming > 0
        return self.node.onoff

    @property
    def brightness(self) -> int | None:
        if not self.node.dimmable or self.node.dimming is None:
            return None
        return round(self.node.dimming / 100 * 255)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not self.node.dimmable:
            self.hub.set_onoff(self.node, True)
            self.node.onoff = True
            self.async_write_ha_state()  # optimistic; reconciled on telemetry
            return
        if (raw := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            percent = _to_percent(raw)
        else:
            # Back to the last brightness instead of always full.
            percent = self.node.dimming or self.node.last_dimming or 100
        percent = max(1, percent)
        self.hub.set_dimming(self.node, percent)
        self.node.dimming = percent
        self.node.last_dimming = percent
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.node.dimmable:
            self.hub.set_dimming(self.node, 0)
            self.node.dimming = 0
        else:
            self.hub.set_onoff(self.node, False)
            self.node.onoff = False
        self.async_write_ha_state()
