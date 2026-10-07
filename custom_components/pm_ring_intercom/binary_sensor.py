"""Binärsensoren: Klingelt (für Panel-Overlays) und Push-Verbindung."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import PMRingIntercomConfigEntry
from .const import SIGNAL_DING, SIGNAL_PUSH, SIGNAL_STATUS
from .entity import IntercomEntity
from .hub import DingInfo, IntercomHub

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PMRingIntercomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Entitäten anlegen."""
    hub = entry.runtime_data.hub
    async_add_entities([IntercomRingingSensor(hub), IntercomPushConnected(hub)])


class IntercomRingingSensor(IntercomEntity, BinarySensorEntity):
    """Ist nach dem Klingeln für eine einstellbare Dauer eingeschaltet."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_is_on = False

    def __init__(self, hub: IntercomHub) -> None:
        """Initialisieren."""
        super().__init__(hub, "klingelt")
        self._cancel_off: CALLBACK_TYPE | None = None
        self._attrs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Signal abonnieren."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DING.format(self.hub.entry.entry_id), self._handle_ding
            )
        )
        self.async_on_remove(self._cancel)

    @callback
    def _cancel(self) -> None:
        if self._cancel_off:
            self._cancel_off()
            self._cancel_off = None

    @callback
    def _handle_ding(self, ding: DingInfo) -> None:
        self._cancel()
        self._attr_is_on = True
        self._attrs = ding.as_event_data()
        self.async_write_ha_state()
        self._cancel_off = async_call_later(
            self.hass, self.hub.ring_duration, self._turn_off
        )

    @callback
    def _turn_off(self, _now: datetime) -> None:
        self._cancel_off = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Angaben zum letzten Klingeln."""
        return self._attrs


class IntercomPushConnected(IntercomEntity, BinarySensorEntity):
    """Zeigt, ob der Push-Empfang von Ring aktiv ist."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: IntercomHub) -> None:
        """Initialisieren."""
        super().__init__(hub, "push_verbindung")

    async def async_added_to_hass(self) -> None:
        """Signale abonnieren."""
        await super().async_added_to_hass()
        for signal in (SIGNAL_STATUS, SIGNAL_PUSH):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal.format(self.hub.entry.entry_id),
                    self.async_write_ha_state,
                )
            )

    @property
    def is_on(self) -> bool:
        """Push-Empfang aktiv."""
        return self.hub.listener_started

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Zähler und Abfragestatus."""
        hub = self.hub
        return {
            "push_nachrichten": hub.push_count,
            "klingeln_per_push": hub.ding_count.get("push", 0),
            "klingeln_nur_per_abfrage": hub.ding_count.get("abfrage", 0),
            "abfrageintervall_s": hub.poll_interval,
            "letzte_abfrage": hub.last_poll.isoformat() if hub.last_poll else None,
            "abfragefehler": hub.last_poll_error,
        }
