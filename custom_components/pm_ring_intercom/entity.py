"""Gemeinsame Basis der Entitäten."""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import IntercomHub


class IntercomEntity(Entity):
    """Entität am Gerät der Intercom.

    Über die MAC-Adresse hängen die Entitäten am selben Gerät wie die der
    offiziellen Ring-Integration.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: IntercomHub, key: str) -> None:
        """Initialisieren."""
        self.hub = hub
        intercom = hub.intercom
        self._attr_unique_id = f"{intercom.id}-{key}"
        self._attr_translation_key = key
        connections: set[tuple[str, str]] = set()
        raw_mac = str(getattr(intercom, "device_id", "") or "")
        if re.fullmatch(r"[0-9a-fA-F]{12}", raw_mac.replace(":", "")):
            connections.add((CONNECTION_NETWORK_MAC, format_mac(raw_mac)))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(intercom.id))},
            connections=connections,
            manufacturer="Ring",
            model="Intercom",
            name=intercom.name,
        )
