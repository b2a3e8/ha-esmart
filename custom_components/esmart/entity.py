"""Base classes and platform setup shared by every eSMART entity."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DOMAIN,
    signal_connectivity,
    signal_new_node,
    signal_node_removed,
    signal_node_update,
)
from .coordinator import ESmartConfigEntry, ESmartHub, ESmartItem
from .protocol import ESmartDoor, ESmartMeter, ESmartNode, ESmartScene

type EntityFactory = Callable[[ESmartHub, ESmartItem], list[Entity]]


@callback
def async_setup_platform_entities(
    hass: HomeAssistant,
    entry: ESmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: EntityFactory,
) -> None:
    """Add entities for every known item now and for each one discovered later.

    ``factory`` returns the platform's entities for an item (often none). The
    hub announces each item once, and listing the known ones and subscribing
    happen in the same callback, so nothing is added twice.
    """
    hub = entry.runtime_data

    @callback
    def _add(item: ESmartItem) -> None:
        if entities := factory(hub, item):
            async_add_entities(entities)

    for item in list(hub.items()):
        _add(item)
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_node(entry.entry_id), _add)
    )


class ESmartEntity(Entity):
    """Common wiring: device info, availability and dispatcher subscriptions."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: ESmartHub, unique_id: str, name: str | None) -> None:
        self.hub = hub
        self._attr_unique_id = unique_id
        # Entities with a translated name leave this unset.
        if name is not None:
            self._attr_name = name
        # An entity derived from a node keeps following that node's updates.
        self._update_id = unique_id

    @property
    def available(self) -> bool:
        return self.hub.connected

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates, removal and connectivity changes."""
        entry_id = self.hub.entry_id
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_node_update(entry_id, self._update_id),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_node_removed(entry_id, self._update_id),
                self._handle_removed,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_connectivity(entry_id), self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        # @callback marks this to run on the event loop. Without it, Home
        # Assistant runs dispatcher targets in the executor pool, and
        # async_write_ha_state() then raises a thread-safety error.
        self.async_write_ha_state()

    @callback
    def _handle_removed(self) -> None:
        """The wall unit dropped our node: remove the entity for good."""
        if self.registry_entry is not None:
            # Removing the registry entry also removes the entity.
            er.async_get(self.hass).async_remove(self.entity_id)
        else:
            self.hass.async_create_task(self.async_remove(force_remove=True))


class ESmartNodeEntity(ESmartEntity):
    """Base for node-backed entities: each node is its own device in its room."""

    def __init__(self, hub: ESmartHub, node: ESmartNode) -> None:
        super().__init__(hub, node.unique_id, None)
        self.node = node
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, node.unique_id)},
            name=node.name,
            manufacturer="eSMART",
            model=f"eSMART {node.kind.title()}",
            suggested_area=node.room_name,
            via_device_id=hub.controller_device_id,
        )


class ESmartDoorEntity(ESmartEntity):
    """Base for the entrance-door button, attached to the controller device."""

    def __init__(self, hub: ESmartHub, door: ESmartDoor) -> None:
        super().__init__(hub, door.unique_id, door.name)
        self.door = door
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.entry_id)})


class ESmartSceneEntity(ESmartEntity):
    """Base for wall-unit scenes, attached to the controller device."""

    def __init__(self, hub: ESmartHub, scene: ESmartScene) -> None:
        super().__init__(hub, scene.unique_id, scene.name)
        self.scene = scene
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.entry_id)})

    @property
    def available(self) -> bool:
        return super().available and self.scene.active


class ESmartMeterEntity(ESmartEntity):
    """Base for apartment meters, attached to the controller device."""

    def __init__(self, hub: ESmartHub, meter: ESmartMeter, name: str | None) -> None:
        super().__init__(hub, meter.unique_id, name)
        self.meter = meter
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.entry_id)})
