"""Consistency checks for the integration's metadata files."""

from __future__ import annotations

import json
from pathlib import Path

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "esmart"


def _load(name: str) -> dict:
    return json.loads((INTEGRATION / name).read_text(encoding="utf-8"))


def test_english_translation_matches_strings() -> None:
    """Custom integrations load translations/en.json, not strings.json."""
    assert _load("translations/en.json") == _load("strings.json")


def test_entity_translation_keys_have_icons_or_names() -> None:
    strings = _load("strings.json")["entity"]
    icons = _load("icons.json")["entity"]
    for platform, keys in icons.items():
        for key in keys:
            assert key in strings[platform], f"{platform}.{key} has no translation"
