"""Diagnosedaten inklusive der letzten Push-Rohnachrichten (geschwärzt)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import PMRingIntercomConfigEntry, fcm_patch


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
        "push_aktiv": hub.push_connected,
        "push_gestartet_laut_ring_doorbell": hub.listener_started,
        "push_fehler": hub.last_push_error,
        "push_fehler_um": hub.last_push_error_at.isoformat()
        if hub.last_push_error_at
        else None,
        "push_neustarts": hub.watchdog_restarts,
        "push_neuregistrierungen": hub.reregistrations,
        "push_stille_ausfaelle": hub.silent_failures,
        "push_ring_anmeldung_um": hub.last_resubscribe.isoformat() if hub.last_resubscribe else None,
        "push_ring_anmeldung_ok": hub.last_resubscribe_ok,
        "fcm_gehaertet": hub.fcm_hardened,
        "fcm_verworfen": dict(fcm_patch.STATISTIK),
        "push_nachrichten": hub.push_count,
        "klingeln": hub.ding_count,
        "letztes_klingeln": hub.last_ding.as_event_data() if hub.last_ding else None,
        "letzte_abfrage": hub.last_poll.isoformat() if hub.last_poll else None,
        "abfragefehler": hub.last_poll_error,
        "letzte_push_nachrichten": list(hub.recent_pushes),
        "audio_signalisierung": hub.audio.last_log,
    }
