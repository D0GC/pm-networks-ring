"""Einrichtung, Push, Abfrage und Audio mit nachgebildetem Ring.

Die Testumgebung läuft auf Englisch, daher englische Entitäts-IDs.
"""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.pm_ring_intercom.const import DOMAIN, EVENT_DING

INTERCOM_ID = 123456


def _intercom() -> MagicMock:
    dev = MagicMock()
    dev.id = INTERCOM_ID
    dev.device_api_id = INTERCOM_ID
    dev.name = "Haustür"
    dev.kind = "intercom_handset_audio"
    dev.device_id = "649a63d2e852"
    dev.async_history = AsyncMock(return_value=[])
    dev.async_open_door = AsyncMock(return_value=True)
    return dev


def _push(category: str, ding_id: str = "1") -> dict:
    return {
        "data": {
            "android_config": json.dumps({"category": category}),
            "data": json.dumps(
                {
                    "device": {"id": INTERCOM_ID, "kind": "intercom_handset_audio"},
                    "event": {
                        "ding": {
                            "id": ding_id,
                            "created_at": dt_util.utcnow().isoformat(),
                            "subtype": "ding",
                        }
                    },
                }
            ),
        }
    }


async def _setup(hass: HomeAssistant, intercom: MagicMock, *, push_error: Exception | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Haustür",
        unique_id=str(INTERCOM_ID),
        data={"token": {"access_token": "x"}, "hardware_id": "hw", "intercom_id": INTERCOM_ID},
    )
    entry.add_to_hass(hass)
    ring = MagicMock()
    ring.async_create_session = AsyncMock()
    ring.async_update_devices = AsyncMock()
    ring.devices.return_value.other = [intercom]

    async def _start(self, timeout=10):
        if push_error:
            raise push_error
        self.started = True
        return True

    with (
        patch("custom_components.pm_ring_intercom.Ring", return_value=ring),
        patch("custom_components.pm_ring_intercom.Auth"),
        patch("custom_components.pm_ring_intercom.hub._RawListener.start", _start),
        patch("custom_components.pm_ring_intercom.hub._RawListener.stop", AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_push_ding_with_unknown_category(hass: HomeAssistant) -> None:
    """Eine von ring_doorbell unbekannte Kategorie löst das Klingeln aus."""
    intercom = _intercom()
    entry = await _setup(hass, intercom)
    events = []
    hass.bus.async_listen(EVENT_DING, events.append)

    hub = entry.runtime_data.hub
    hub.listener._on_notification(_push("com.ring.pn.live-event.ding-call"), "pid")
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["quelle"] == "push"
    assert hass.states.get("binary_sensor.haustur_ringing").state == "on"
    state = hass.states.get("event.haustur_ding_live")
    assert state.attributes["event_type"] == "ring"
    assert hass.states.get("binary_sensor.haustur_push_connection").state == "on"

    # Gleiches Klingeln erneut (z. B. Abfrage) wird nicht doppelt gemeldet.
    hub.listener._on_notification(_push("com.ring.pn.live-event.intercom"), "pid2")
    await hass.async_block_till_done()
    assert len(events) == 1

    # Entsperren ist kein Klingeln.
    hub.listener._on_notification(_push("com.ring.pn.intercom.virtual.unlock", "9"), "pid3")
    await hass.async_block_till_done()
    assert len(events) == 1
    assert hass.states.get("sensor.haustur_last_push_message").state == (
        "com.ring.pn.intercom.virtual.unlock"
    )


async def test_poll_fallback(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Ohne Push meldet die Abfrage das Klingeln."""
    intercom = _intercom()
    await _setup(hass, intercom)
    events = []
    hass.bus.async_listen(EVENT_DING, events.append)

    intercom.async_history.return_value = [
        {"id": 555, "kind": "ding", "created_at": dt_util.utcnow()}
    ]
    freezer.tick(timedelta(seconds=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["quelle"] == "abfrage"

    freezer.tick(timedelta(seconds=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_simulate_service(hass: HomeAssistant) -> None:
    """Der Testdienst löst ein Klingeln aus."""
    await _setup(hass, _intercom())
    events = []
    hass.bus.async_listen(EVENT_DING, events.append)
    await hass.services.async_call(DOMAIN, "klingeln_simulieren", blocking=True)
    await hass.async_block_till_done()
    assert events[0].data["quelle"] == "test"


async def test_audio_websocket(hass: HomeAssistant, hass_ws_client) -> None:
    """Audio-Sitzung: Angebot, Antwort, Kandidat, Beenden."""
    entry = await _setup(hass, _intercom())
    hub = entry.runtime_data.hub
    created = {}

    class FakeStream:
        def __init__(self, ring, device_id, **kwargs):
            created["kwargs"] = kwargs
            created["device_id"] = device_id
            self.log = []
            self.candidates = []
            self.closed = False

        async def generate(self, offer):
            from ring_doorbell.webrtcstream import RingWebRtcMessage

            self.kwargs = created["kwargs"]
            self.kwargs["on_message_callback"](RingWebRtcMessage(answer="v=0 answer"))

        async def on_ice_candidate(self, candidate, index):
            self.candidates.append((candidate, index))

        def sync_close(self):
            self.closed = True

        async def close(self):
            self.closed = True

    client = await hass_ws_client(hass)
    with patch("custom_components.pm_ring_intercom.audio.IntercomWebRtcStream", FakeStream):
        await client.send_json({"id": 1, "type": f"{DOMAIN}/audio/start", "offer": "v=0\r\nm=audio"})
        assert (await client.receive_json())["success"]
        msg = await client.receive_json()
        assert msg["event"]["type"] == "session"
        session_id = msg["event"]["session_id"]
        msg = await client.receive_json()
        assert msg["event"] == {"type": "answer", "answer": "v=0 answer"}

    assert created["device_id"] == INTERCOM_ID
    assert created["kwargs"]["video_enabled"] is False
    await hass.async_block_till_done()
    assert hass.states.get("sensor.haustur_intercom_audio").state == "gespraech"

    stream = hub.audio.streams[session_id]
    await client.send_json(
        {"id": 2, "type": f"{DOMAIN}/audio/candidate", "session_id": session_id,
         "candidate": "candidate:1", "sdp_m_line_index": 0}
    )
    assert (await client.receive_json())["success"]
    assert stream.candidates == [("candidate:1", 0)]

    await client.send_json({"id": 3, "type": "unsubscribe_events", "subscription": 1})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert stream.closed
    assert hass.states.get("sensor.haustur_intercom_audio").state == "bereit"


async def test_unload(hass: HomeAssistant) -> None:
    """Entladen funktioniert."""
    entry = await _setup(hass, _intercom())
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_push_failure_keeps_polling(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Scheitert die FCM-Registrierung, läuft die Einrichtung trotzdem und die Abfrage meldet."""
    intercom = _intercom()
    entry = await _setup(
        hass,
        intercom,
        push_error=RuntimeError("Unable to establish subscription with Google Cloud Messaging."),
    )
    assert entry.state.value == "loaded"
    state = hass.states.get("binary_sensor.haustur_push_connection")
    assert state.state == "off"
    assert "RuntimeError" in state.attributes["push_fehler"]

    events = []
    hass.bus.async_listen(EVENT_DING, events.append)
    intercom.async_history.return_value = [
        {"id": 777, "kind": "ding", "created_at": dt_util.utcnow()}
    ]
    freezer.tick(timedelta(seconds=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_open_door_button(hass: HomeAssistant) -> None:
    """Der Knopf öffnet die Haustür über die Intercom, eine Ablehnung wird gemeldet."""
    import pytest

    from homeassistant.exceptions import HomeAssistantError

    intercom = _intercom()
    await _setup(hass, intercom)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.haustur_open_door"}, blocking=True
    )
    intercom.async_open_door.assert_awaited_once()

    intercom.async_open_door.return_value = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.haustur_open_door"}, blocking=True
        )
