"""Scene platform for scenes stored on the eSMART wall unit."""

from __future__ import annotations

from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ESmartConfigEntry, ESmartHub, ESmartItem
from .entity import ESmartSceneEntity, async_setup_platform_entities
from .protocol import ESmartScene

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ESmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up scenes as they are discovered."""

    def _factory(hub: ESmartHub, item: ESmartItem) -> list[Entity]:
        if isinstance(item, ESmartScene):
            return [ESmartWallUnitScene(hub, item)]
        return []

    async_setup_platform_entities(hass, entry, async_add_entities, _factory)


class ESmartWallUnitScene(ESmartSceneEntity, Scene):
    """Runs a scene on the wall unit, which then actuates its nodes itself."""

    async def async_activate(self, **kwargs: Any) -> None:
        self.hub.activate_scene(self.scene)
