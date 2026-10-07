"""Intercom-Audio: Gespräch mit der Ring Intercom über WebRTC (experimentell).

Ohne Kamera-Entität. Die Integration stellt eigene WebSocket-Befehle bereit:

``pm_ring_intercom/audio/start``      Abonnement. Nimmt das SDP-Angebot des
                                      Browsers (mit Mikrofonspur) entgegen und
                                      liefert Sitzung, Antwort, ICE-Kandidaten
                                      und Fehler. Abbestellen beendet das Gespräch.
``pm_ring_intercom/audio/candidate``  ICE-Kandidat des Browsers an Ring geben.

Der Ton selbst läuft danach direkt zwischen Browser und Ring.
"""

from __future__ import annotations

import logging
from typing import Any
import uuid

from ring_doorbell.exceptions import RingError
from ring_doorbell.webrtcstream import RingWebRtcMessage
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_STATUS
from .stream import IntercomWebRtcStream

_LOGGER = logging.getLogger(__name__)


class AudioSessions:
    """Offene Gespräche einer Intercom."""

    def __init__(self, hass: HomeAssistant, hub: Any) -> None:
        """Initialisieren."""
        self.hass = hass
        self.hub = hub
        self.streams: dict[str, IntercomWebRtcStream] = {}
        self.last_log: list[str] = []

    @property
    def active(self) -> int:
        """Anzahl offener Gespräche."""
        return len(self.streams)

    @callback
    def _changed(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.hub.entry.entry_id))

    async def async_start(
        self, offer_sdp: str, send: Any
    ) -> str:
        """Gespräch aufbauen und Sitzungs-ID zurückgeben."""
        session_id = uuid.uuid4().hex

        def _on_message(message: RingWebRtcMessage) -> None:
            if message.error_code:
                _LOGGER.warning(
                    "Ring hat das Gespräch beendet oder abgelehnt: %s %s",
                    message.error_code,
                    message.error_message,
                )
                send({"type": "error", "code": str(message.error_code),
                      "message": message.error_message or ""})
            elif message.answer:
                send({"type": "answer", "answer": message.answer})
            elif message.candidate:
                send({"type": "candidate", "candidate": {
                    "candidate": message.candidate,
                    "sdpMLineIndex": message.sdp_m_line_index or 0,
                }})

        async def _on_close() -> None:
            send({"type": "closed"})
            self.drop(session_id)

        stream = IntercomWebRtcStream(
            self.hub.ring,
            self.hub.intercom.id,
            video_enabled="m=video" in offer_sdp,
            keep_alive_timeout=None,
            on_message_callback=_on_message,
            on_close_callback=_on_close,
        )
        self.streams[session_id] = stream
        self.last_log = stream.log
        self._changed()
        send({"type": "session", "session_id": session_id})
        try:
            await stream.generate(offer_sdp)
        except RingError:
            self.drop(session_id)
            raise
        return session_id

    async def async_candidate(self, session_id: str, candidate: str, index: int) -> None:
        """ICE-Kandidaten weitergeben."""
        if stream := self.streams.get(session_id):
            await stream.on_ice_candidate(candidate, index)

    @callback
    def close(self, session_id: str) -> None:
        """Gespräch beenden."""
        if stream := self.streams.get(session_id):
            stream.sync_close()
        self.drop(session_id)

    @callback
    def drop(self, session_id: str) -> None:
        """Gespräch aus der Liste nehmen."""
        if self.streams.pop(session_id, None) is not None:
            self._changed()

    async def async_close_all(self) -> None:
        """Alle Gespräche schließen."""
        for stream in list(self.streams.values()):
            await stream.close()
        self.streams.clear()


def _get_sessions(hass: HomeAssistant, entry_id: str | None) -> AudioSessions | None:
    entries = [
        e
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if getattr(e, "runtime_data", None)
    ]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    if len(entries) != 1:
        return None
    return entries[0].runtime_data.hub.audio


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/audio/start",
        vol.Required("offer"): str,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_audio_start(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Gespräch starten (Abonnement)."""
    sessions = _get_sessions(hass, msg.get("entry_id"))
    if sessions is None:
        connection.send_error(msg["id"], "not_found", "Keine eindeutige Intercom gefunden")
        return

    msg_id = msg["id"]
    session_id: str | None = None

    @callback
    def _send(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, payload))

    @callback
    def _unsubscribe() -> None:
        if session_id:
            sessions.close(session_id)

    connection.subscriptions[msg_id] = _unsubscribe
    connection.send_result(msg_id)
    try:
        session_id = await sessions.async_start(msg["offer"], _send)
    except RingError as err:
        _send({"type": "error", "code": "ring", "message": str(err)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/audio/candidate",
        vol.Required("session_id"): str,
        vol.Required("candidate"): str,
        vol.Optional("sdp_m_line_index", default=0): int,
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_audio_candidate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """ICE-Kandidaten des Browsers entgegennehmen."""
    sessions = _get_sessions(hass, msg.get("entry_id"))
    if sessions is None:
        connection.send_error(msg["id"], "not_found", "Keine eindeutige Intercom gefunden")
        return
    await sessions.async_candidate(
        msg["session_id"], msg["candidate"], msg["sdp_m_line_index"]
    )
    connection.send_result(msg["id"])


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """WebSocket-Befehle registrieren."""
    websocket_api.async_register_command(hass, ws_audio_start)
    websocket_api.async_register_command(hass, ws_audio_candidate)
