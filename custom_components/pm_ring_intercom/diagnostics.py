"""Diagnosedaten inklusive der letzten Push-Rohnachrichten (geschwärzt)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import PMRingIntercomConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PMRingIntercomConfigEntry
) -> dict[str, Any]:
    """Diagnose für den Eintrag."""
    hub = entry.runtime_data.hub
    return {
        "intercom": {
            "id": hub.intercom.id,
            "name": hub.intercom.name,
            "kind": hub.intercom.kind,
        },
        "optionen": dict(entry.options),
        "push_aktiv": hub.listener_started,
        "push_fehler": hub.last_push_error,
        "push_nachrichten": hub.push_count,
        "klingeln": hub.ding_count,
        "letztes_klingeln": hub.last_ding.as_event_data() if hub.last_ding else None,
        "letzte_abfrage": hub.last_poll.isoformat() if hub.last_poll else None,
        "abfragefehler": hub.last_poll_error,
        "letzte_push_nachrichten": list(hub.recent_pushes),
        "audio_signalisierung": hub.audio.last_log,
    }
