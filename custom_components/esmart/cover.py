"""Cover platform for eSMART shutter, blind and awning nodes."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import KIND_COVER, POSITION_MAX
from .coordinator import ESmartConfigEntry, ESmartHub, ESmartItem
from .entity import ESmartNodeEntity, async_setup_platform_entities
from .protocol import ESmartNode, is_awning

_LOGGER = logging.getLogger(__name__)

# Commands are queued and coalesced by the client; don't serialize them here.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ESmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up covers as they are discovered."""

    def _factory(hub: ESmartHub, item: ESmartItem) -> list[Entity]:
        if isinstance(item, ESmartNode) and item.kind == KIND_COVER:
            return [ESmartCover(hub, item)]
        return []

    async_setup_platform_entities(hass, entry, async_add_entities, _factory)


def ticks_to_percent(ticks: int | None) -> int | None:
    """0 ticks (open) -> 100 %, max ticks (closed) -> 0 %."""
    if ticks is None:
        return None
    return round(100 - (max(0, min(POSITION_MAX, ticks)) / POSITION_MAX * 100))


def percent_to_ticks(percent: int) -> int:
    """The inverse of :func:`ticks_to_percent`."""
    return round((100 - max(0, min(100, percent))) / 100 * POSITION_MAX)


class ESmartCover(ESmartNodeEntity, CoverEntity):
    """An eSMART shutter, with optional slat tilt on venetian blinds."""

    _attr_name = None  # the node's main feature, named after its device

    def __init__(self, hub: ESmartHub, node: ESmartNode) -> None:
        super().__init__(hub, node)
        # The wall unit reports awnings and blinds alike; only the name differs.
        self._attr_device_class = (
            CoverDeviceClass.AWNING
            if is_awning(node.name)
            else CoverDeviceClass.SHUTTER
        )
        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        if node.tiltable:
            features |= (
                CoverEntityFeature.SET_TILT_POSITION
                | CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
            )
        self._attr_supported_features = features

    @property
    def current_cover_position(self) -> int | None:
        return ticks_to_percent(self.node.position)

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        if position is None:
            return None
        return position == 0

    @property
    def current_cover_tilt_position(self) -> int | None:
        if not self.node.tiltable or self.node.orientation is None:
            return None
        return round(self.node.orientation / POSITION_MAX * 100)

    def _log_command(self, command: str, target: int | None = None) -> None:
        _LOGGER.debug(
            "COVER cmd %s on %s (id %s): target=%s, reported position=%s "
            "orientation=%s",
            command,
            self.node.name,
            self.node.node_id,
            target,
            self.node.position,
            self.node.orientation,
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        self._log_command("open", 0)
        self.hub.set_position(self.node, 0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        self._log_command("close", POSITION_MAX)
        self.hub.set_position(self.node, POSITION_MAX)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        ticks = percent_to_ticks(kwargs[ATTR_POSITION])
        self._log_command(f"set_position {kwargs[ATTR_POSITION]}%", ticks)
        self.hub.set_position(self.node, ticks)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        self._log_command("stop")
        self.hub.stop_node(self.node)

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        ticks = round(kwargs[ATTR_TILT_POSITION] / 100 * POSITION_MAX)
        self._log_command(f"set_tilt {kwargs[ATTR_TILT_POSITION]}%", ticks)
        self.hub.set_orientation(self.node, ticks)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        self._log_command("open_tilt", POSITION_MAX)
        self.hub.set_orientation(self.node, POSITION_MAX)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        self._log_command("close_tilt", 0)
        self.hub.set_orientation(self.node, 0)
