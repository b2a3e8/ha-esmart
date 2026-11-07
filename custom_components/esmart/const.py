"""Constants for the eSMART integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "esmart"

# --- XMPP transport -------------------------------------------------------
XMPP_HOST: Final = "xmpp.myesmart.net"
XMPP_PORT: Final = 5222
XMPP_DOMAIN: Final = "myesmart.net"

# Entity capabilities (XEP-0115) advertised in presence, in the form the wall
# unit expects from a remote.
CAPS_NODE: Final = "https://github.com/robbiehanson/XMPPFramework"
CAPS_VER: Final = "VyOFcFX6+YNmKssVXSBKGFP0BS4="

# Protocol version reported in command headers.
PROTOCOL_VERSION: Final = "1.19.0"

# --- Config entry keys ----------------------------------------------------
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_NICKNAME: Final = "nickname"
CONF_PAIRING_CODE: Final = "pairing_code"

DEFAULT_NICKNAME: Final = "Home Assistant"

# --- Node classification --------------------------------------------------
# Node "type_id" values observed on the bus. Names are matched as a fallback
# because some firmwares localise them (e.g. "Lampe", "Store").
NODE_TYPE_LIGHT: Final = 2
NODE_TYPE_DIMMABLE_LIGHT: Final = 3
NODE_TYPE_SHUTTER: Final = 4
NODE_TYPE_THERMOSTAT: Final = 6

KIND_LIGHT: Final = "light"
KIND_COVER: Final = "cover"
KIND_CLIMATE: Final = "climate"
KIND_DOOR: Final = "door"
KIND_SCENE: Final = "scene"
KIND_METER: Final = "meter"

# Readings a meter entry can carry. One meter id can report several of them, as
# separate entries.
METRIC_POWER: Final = "power"
METRIC_FLOW_RATE: Final = "flow_rate"
METRIC_TEMPERATURE: Final = "temperature"

# Shutters move on a 0..1024 tick scale; 0 is fully open, 1024 fully closed.
POSITION_MAX: Final = 1024

# Thermostat bounds, used until the wall unit's config reports its own
# (setpoint_min_temp / setpoint_max_temp / setpoint_increment). These are the
# values observed on a stock installation.
DEFAULT_SETPOINT_MIN: Final = 18.0
DEFAULT_SETPOINT_MAX: Final = 24.0
DEFAULT_SETPOINT_STEP: Final = 1.0


# Keywords in a cover's name that mark it as an awning. eSMART reports awnings
# with the same type as blinds ("Store"), so the name is the only hint.
AWNING_KEYWORDS: Final = ("markise", "awning", "banne", "tenda")

# Prefix of the XMPP usernames the integration generates. If pairing finishes
# before the roster lists the wall unit, the entry uses the username as unique
# ID; it is migrated to the installation ID once a device room is joined.
USERNAME_PREFIX: Final = "i-"

# Repair issue raised when the wall unit doesn't let this remote into its room.
ISSUE_ROOM_ACCESS: Final = "room_access_denied"


def signal_new_node(entry_id: str) -> str:
    """Dispatcher signal fired when a new node is discovered."""
    return f"{DOMAIN}_{entry_id}_new_node"


def signal_node_update(entry_id: str, unique_id: str) -> str:
    """Dispatcher signal fired when a node's state changes."""
    return f"{DOMAIN}_{entry_id}_update_{unique_id}"


def signal_node_removed(entry_id: str, unique_id: str) -> str:
    """Dispatcher signal fired when a node disappears from the wall unit."""
    return f"{DOMAIN}_{entry_id}_removed_{unique_id}"


def signal_connectivity(entry_id: str) -> str:
    """Dispatcher signal fired when the XMPP connection comes up or down."""
    return f"{DOMAIN}_{entry_id}_connectivity"
