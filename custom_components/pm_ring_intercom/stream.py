"""WebRTC-Gegensprechen mit der Ring Intercom (experimentell).

Ring stellt Live-Sitzungen über einen Signalisierungs-Websocket bereit
(Methode ``live_view``). ring_doorbell nutzt ihn nur für Kameras und fordert
dabei immer Video an. Die Intercom hat keine Kamera. Diese Klasse fordert
deshalb nur Audio an und protokolliert jede Signalisierungsnachricht, damit
sichtbar wird, ob und wie Ring eine Intercom-Sitzung annimmt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from typing import Any

from ring_doorbell.const import (
    APP_API_URI,
    RTC_STREAMING_TICKET_ENDPOINT,
    RTC_STREAMING_WEB_SOCKET_ENDPOINT,
)
from ring_doorbell.exceptions import RingError
from ring_doorbell.webrtcstream import RingWebRtcStream
from websockets.asyncio.client import connect

_LOGGER = logging.getLogger(__name__)


class IntercomWebRtcStream(RingWebRtcStream):
    """Live-Sitzung mit wählbaren Stream-Optionen und Mitschnitt."""

    def __init__(self, *args: Any, video_enabled: bool = False, **kwargs: Any) -> None:
        """Initialisieren."""
        super().__init__(*args, **kwargs)
        self._video_enabled = video_enabled
        self.log: list[str] = []

    def _remember(self, direction: str, payload: Any) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        # SDP und ICE kürzen, damit der Mitschnitt lesbar bleibt.
        self.log.append(f"{direction} {text[:400]}")
        del self.log[:-40]
        _LOGGER.debug("Signalisierung %s %s", direction, text)

    async def _generate(self, sdp_offer: str) -> None:
        try:
            req = await self._ring.async_query(
                RTC_STREAMING_TICKET_ENDPOINT,
                method="POST",
                base_uri=APP_API_URI,
            )
            ticket = req.json()["ticket"]
            ws_uri = RTC_STREAMING_WEB_SOCKET_ENDPOINT.format(uuid.uuid4(), ticket)
            if not self.ssl_context:
                loop = asyncio.get_running_loop()
                self.ssl_context = await loop.run_in_executor(
                    None, ssl.create_default_context
                )
            self.websocket = await connect(
                ws_uri,
                user_agent_header="android:com.ringapp",
                ssl=self.ssl_context,
            )
            self.dialog_id = str(uuid.uuid4())
            offer_msg = {
                "method": "live_view",
                "dialog_id": self.dialog_id,
                "body": {
                    "doorbot_id": self.device_api_id,
                    "stream_options": {
                        "audio_enabled": True,
                        "video_enabled": self._video_enabled,
                    },
                    "sdp": sdp_offer,
                    "type": "offer",
                },
            }
            self.read_task = asyncio.create_task(self.reader())
            self._remember(">>", {"method": "live_view", "doorbot_id": self.device_api_id})
            await self.websocket.send(json.dumps(offer_msg))
            self._offered_event.set()
        except Exception as ex:
            raise RingError("Live-Sitzung mit der Intercom fehlgeschlagen", ex) from ex

    async def handle_message(self, message_str: str) -> None:
        """Nachricht mitschneiden und an die Basisklasse geben."""
        self._remember("<<", message_str)
        try:
            await super().handle_message(message_str)
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning(
                "Unerwartete Signalisierungsnachricht von Ring: %s", message_str[:400]
            )
