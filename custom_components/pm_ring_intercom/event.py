"""Ereignis-Entität: Klingeln (live)."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PMRingIntercomConfigEntry
from .const import SIGNAL_DING
from .entity import IntercomEntity
from .hub import DingInfo

PARALLEL_UPDATES = 0

# Entspricht DoorbellEventType.RING neuerer HA-Versionen.
EVENT_TYPE_RING = "ring"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PMRingIntercomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Entitäten anlegen."""
    async_add_entities([IntercomDingEvent(entry.runtime_data.hub)])


class IntercomDingEvent(IntercomEntity, EventEntity):
    """Klingeln, ausgelöst durch Push oder Abfrage."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_TYPE_RING]

    def __init__(self, hub) -> None:  # noqa: ANN001
        """Initialisieren."""
        super().__init__(hub, "klingeln_live")

    async def async_added_to_hass(self) -> None:
        """Signal abonnieren."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DING.format(self.hub.entry.entry_id),
                self._handle_ding,
            )
        )

    @callback
    def _handle_ding(self, ding: DingInfo) -> None:
        self._trigger_event(EVENT_TYPE_RING, ding.as_event_data())
        self.async_write_ha_state()
