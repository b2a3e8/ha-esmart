"""Button platform for eSMART entrance-door relays."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ESmartConfigEntry, ESmartHub, ESmartItem
from .entity import ESmartDoorEntity, async_setup_platform_entities
from .protocol import ESmartDoor

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ESmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up door buttons as they are discovered."""

    def _factory(hub: ESmartHub, item: ESmartItem) -> list[Entity]:
        if isinstance(item, ESmartDoor):
            return [ESmartDoorButton(hub, item)]
        return []

    async_setup_platform_entities(hass, entry, async_add_entities, _factory)


class ESmartDoorButton(ESmartDoorEntity, ButtonEntity):
    """Pulses an entrance-door relay, mirroring the native app's button."""

    _attr_translation_key = "door"

    async def async_press(self) -> None:
        self.hub.door_pulse(self.door)
