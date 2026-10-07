"""Diagnose-Sensor: letzte Push-Meldung von Ring."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PMRingIntercomConfigEntry
from .const import SIGNAL_PUSH, SIGNAL_STATUS
from .entity import IntercomEntity
from .hub import IntercomHub

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PMRingIntercomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Entitäten anlegen."""
    hub = entry.runtime_data.hub
    async_add_entities([IntercomLastPushSensor(hub), IntercomAudioSensor(hub)])


class IntercomLastPushSensor(IntercomEntity, SensorEntity):
    """Kategorie der zuletzt empfangenen Push-Nachricht (alle Ring-Geräte)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: IntercomHub) -> None:
        """Initialisieren."""
        super().__init__(hub, "letzte_push_meldung")

    async def async_added_to_hass(self) -> None:
        """Signal abonnieren."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_PUSH.format(self.hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> str | None:
        """Kategorie der letzten Nachricht."""
        push = self.hub.last_push
        if push is None:
            return None
        return (push.category or push.kind)[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Auswertung der letzten Nachricht, ohne Rohdaten."""
        push = self.hub.last_push
        if push is None:
            return {}
        return {
            "art": push.kind,
            "format": push.fmt,
            "geraet_id": push.device_id,
            "geraet_name": push.device_name,
            "geraet_art": push.device_kind,
            "subtype": push.subtype,
            "ding_id": push.ding_id,
            "empfangen": self.hub.last_push_received.isoformat()
            if self.hub.last_push_received
            else None,
        }


class IntercomAudioSensor(IntercomEntity, SensorEntity):
    """Zustand des Intercom-Audios: bereit oder Gespräch aktiv."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["bereit", "gespraech"]
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, hub: IntercomHub) -> None:
        """Initialisieren."""
        super().__init__(hub, "intercom_audio")

    async def async_added_to_hass(self) -> None:
        """Signal abonnieren."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STATUS.format(self.hub.entry.entry_id),
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> str:
        """Gesprächszustand."""
        return "gespraech" if self.hub.audio.active else "bereit"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Anzahl offener Gespräche."""
        return {"aktive_gespraeche": self.hub.audio.active}
