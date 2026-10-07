"""Knopf: Haustür öffnen über die Ring Intercom."""

from __future__ import annotations

from ring_doorbell.exceptions import AuthenticationError, RingError

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PMRingIntercomConfigEntry
from .entity import IntercomEntity
from .hub import IntercomHub

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PMRingIntercomConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Entität anlegen."""
    async_add_entities([IntercomOpenButton(entry.runtime_data.hub)])


class IntercomOpenButton(IntercomEntity, ButtonEntity):
    """Öffnet die Haustür (Türöffner der Siedle-Anlage über die Intercom)."""

    _attr_icon = "mdi:door-open"

    def __init__(self, hub: IntercomHub) -> None:
        """Initialisieren."""
        super().__init__(hub, "tuer_oeffnen")

    async def async_press(self) -> None:
        """Tür öffnen."""
        try:
            ok = await self.hub.intercom.async_open_door()
        except AuthenticationError as err:
            self.hub.entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Ring-Anmeldung abgelaufen") from err
        except (RingError, TimeoutError) as err:
            raise HomeAssistantError(f"Haustür nicht geöffnet: {err}") from err
        if not ok:
            raise HomeAssistantError("Ring hat das Öffnen der Haustür abgelehnt")
