"""Push-Überwachung: toter FCM-Client, Neuregistrierung, kein Neuladen bei Token-Refresh.

Listener und FCM-Client sind nachgebildet, es gibt kein Netzwerk.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import timedelta
from enum import Enum
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
import pytest

from homeassistant.core import HomeAssistant

from custom_components.pm_ring_intercom.const import (
    CONF_LISTEN_CREDENTIALS,
    CONF_POLL_INTERVAL,
    DOMAIN,
)

from .test_init import INTERCOM_ID, _intercom

PUSH_ENTITY = "binary_sensor.haustur_push_connection"
# Platzhalter, keine echten Zugangsdaten.
OLD_CREDS = {"gcm": {"android_id": "alt"}, "fcm": {"registration": {"token": "alt"}}}
NEW_CREDS = {"gcm": {"android_id": "neu"}, "fcm": {"registration": {"token": "neu"}}}


class FakeRunState(Enum):
    """Namensgleich mit firebase_messaging.FcmPushClientRunState."""

    CREATED = 1
    STARTING_TASKS = 2
    STARTING_CONNECTION = 3
    STARTING_LOGIN = 4
    STARTED = 5
    RESETTING = 6
    STOPPING = 7
    STOPPED = 8


class FakeReceiver:
    """Nachbildung von FcmPushClient, soweit die Überwachung sie liest."""

    def __init__(self) -> None:
        self.run_state = FakeRunState.STARTED
        self.do_listen = True
        self.tasks: list[Any] = []

    def is_started(self) -> bool:
        return self.run_state is FakeRunState.STARTED

    def die(self) -> None:
        """Wie FcmPushClient._terminate nach 'Incorrect padding'."""
        logging.getLogger("firebase_messaging.fcmpushclient").error(
            "Unknown error: Incorrect padding, shutting down FcmPushClient.\n"
            "Traceback (most recent call last): ..."
        )
        self.run_state = FakeRunState.STOPPING
        self.do_listen = False


class FakeFcm:
    """Steuert die nachgebildeten Starts des Listeners."""

    def __init__(self) -> None:
        self.starts: list[dict[str, Any] | None] = []
        self.poisoned: list[dict[str, Any]] = []
        self.fail_start: Exception | None = None

    async def start(self, listener: Any, timeout: int = 10) -> bool:
        creds = listener._credentials
        self.starts.append(creds)
        if self.fail_start:
            raise self.fail_start
        if creds is None:
            # Wie FcmRegister.checkin_or_register: neu registrieren, Callback melden.
            listener._credentials_updated_cb(NEW_CREDS)
            creds = NEW_CREDS
        if listener._receiver is None:
            listener._receiver = FakeReceiver()
        receiver = listener._receiver
        receiver.run_state = FakeRunState.STARTED
        receiver.do_listen = True
        listener.started = True
        if creds in self.poisoned:
            # Die unbestätigte Nachricht wird sofort wieder zugestellt.
            receiver.die()
        return True

    async def stop(self, listener: Any) -> None:
        listener.started = False
        receiver = listener._receiver
        if receiver and receiver.run_state not in (
            FakeRunState.STOPPING,
            FakeRunState.STOPPED,
        ):
            receiver.run_state = FakeRunState.STOPPED


@pytest.fixture
def fcm() -> Iterator[FakeFcm]:
    """Ring, Auth und Listener nachbilden, solange der Test läuft."""
    fake = FakeFcm()

    async def _start(self, timeout=10):
        return await fake.start(self, timeout)

    async def _stop(self):
        await fake.stop(self)

    ring = MagicMock()
    ring.async_create_session = AsyncMock()
    ring.async_update_devices = AsyncMock()
    fake.ring = ring
    with ExitStack() as stack:
        stack.enter_context(
            patch("custom_components.pm_ring_intercom.Ring", return_value=ring)
        )
        stack.enter_context(patch("custom_components.pm_ring_intercom.Auth"))
        stack.enter_context(
            patch("custom_components.pm_ring_intercom.hub._RawListener.start", _start)
        )
        stack.enter_context(
            patch("custom_components.pm_ring_intercom.hub._RawListener.stop", _stop)
        )
        yield fake


async def _setup(
    hass: HomeAssistant, fcm: FakeFcm, *, creds: dict[str, Any] | None = OLD_CREDS
) -> MockConfigEntry:
    data: dict[str, Any] = {
        "token": {"access_token": "x"},
        "hardware_id": "hw",
        "intercom_id": INTERCOM_ID,
    }
    if creds is not None:
        data[CONF_LISTEN_CREDENTIALS] = creds
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Haustür",
        unique_id=str(INTERCOM_ID),
        data=data,
        options={CONF_POLL_INTERVAL: 15},
    )
    entry.add_to_hass(hass)
    fcm.ring.devices.return_value.other = [_intercom()]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def test_library_still_provides_receiver_state() -> None:
    """Die Überwachung liest diese Merkmale von firebase_messaging/ring_doorbell."""
    from firebase_messaging.fcmpushclient import FcmPushClient, FcmPushClientRunState
    from ring_doorbell.listen.eventlistener import RingEventListener

    names = {state.name for state in FcmPushClientRunState}
    assert {"STARTED", "STOPPING", "STOPPED"} <= names
    assert callable(getattr(FcmPushClient, "is_started", None))
    assert callable(getattr(RingEventListener, "_credentials_updated_cb", None))


async def test_watchdog_restarts_dead_receiver(
    hass: HomeAssistant,
    fcm: FakeFcm,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A: Der Startmerker bleibt True, der Empfänger ist tot. Die Überwachung startet neu."""
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub
    assert hass.states.get(PUSH_ENTITY).state == "on"
    assert fcm.starts == [OLD_CREDS]

    hub.listener._receiver.die()
    # ring_doorbell meldet weiterhin gestartet, der wahre Zustand ist aus.
    assert hub.listener.started is True
    assert hub.push_connected is False

    caplog.set_level(logging.WARNING)
    await _tick(hass, freezer, 31)

    assert fcm.starts == [OLD_CREDS, OLD_CREDS]
    assert hub.watchdog_restarts == 1
    assert hub.reregistrations == 0
    state = hass.states.get(PUSH_ENTITY)
    assert state.state == "on"
    assert "Incorrect padding" in state.attributes["push_fehler"]
    assert state.attributes["push_fehler_um"] is not None
    assert state.attributes["push_neustarts"] == 1
    assert "Push-Überwachung: Empfang von Ring ausgefallen" in caplog.text
    # Keine Zugangsdaten im Log.
    assert str(OLD_CREDS) not in caplog.text

    # Läuft der Empfang danach stabil, verschwindet der Fehler.
    for _ in range(25):
        await _tick(hass, freezer, 30)
    assert hass.states.get(PUSH_ENTITY).attributes["push_fehler"] is None


async def test_dead_receiver_without_restart_is_off(
    hass: HomeAssistant, fcm: FakeFcm, freezer: FrozenDateTimeFactory
) -> None:
    """D: Solange der Empfänger tot ist und der Neustart scheitert, steht der Sensor auf aus."""
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub
    fcm.fail_start = RuntimeError("Unable to establish subscription with Google Cloud Messaging.")
    hub.listener._receiver.die()

    await _tick(hass, freezer, 16)

    state = hass.states.get(PUSH_ENTITY)
    assert state.state == "off"
    assert "RuntimeError" in state.attributes["push_fehler"]

    # Backoff: der nächste Versuch kommt nicht sofort beim nächsten Durchlauf.
    starts = len(fcm.starts)
    await _tick(hass, freezer, 5)
    assert len(fcm.starts) == starts

    # Sobald der Start wieder gelingt, ist die Verbindung wieder an.
    fcm.fail_start = None
    for _ in range(4):
        await _tick(hass, freezer, 30)
    assert hass.states.get(PUSH_ENTITY).state == "on"


async def test_hanging_connection_is_restarted(
    hass: HomeAssistant, fcm: FakeFcm, freezer: FrozenDateTimeFactory
) -> None:
    """A: Kommt die Anmeldung bei FCM minutenlang nicht zustande, wird neu gestartet."""
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub
    hub.listener._receiver.run_state = FakeRunState.STARTING_CONNECTION
    hub._send_status_if_changed()
    await hass.async_block_till_done()
    assert hass.states.get(PUSH_ENTITY).state == "off"

    # Kurzzeitig nicht angemeldet ist kein Ausfall.
    await _tick(hass, freezer, 31)
    assert hub.watchdog_restarts == 0

    for _ in range(7):
        await _tick(hass, freezer, 30)
    assert hub.watchdog_restarts == 1
    assert hub.reregistrations == 0
    assert hass.states.get(PUSH_ENTITY).state == "on"


async def test_poison_message_triggers_reregistration(
    hass: HomeAssistant,
    fcm: FakeFcm,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B: Stirbt der Empfänger nach jedem Start, werden die FCM-Zugangsdaten erneuert."""
    fcm.poisoned = [OLD_CREDS]
    caplog.set_level(logging.WARNING)
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub
    assert hub.push_connected is False

    for _ in range(4):
        await _tick(hass, freezer, 16)

    # Start, Neustart mit alten Daten, dann Neuregistrierung ohne Daten.
    assert fcm.starts == [OLD_CREDS, OLD_CREDS, None]
    assert hub.reregistrations == 1
    assert entry.data[CONF_LISTEN_CREDENTIALS] == NEW_CREDS
    # Speichern der neuen Zugangsdaten lädt den Eintrag nicht neu.
    assert entry.runtime_data.hub is hub
    state = hass.states.get(PUSH_ENTITY)
    assert state.state == "on"
    assert state.attributes["push_neuregistrierungen"] == 1
    assert "Neuregistrierung bei FCM und Ring" in caplog.text
    assert "Neuregistrierung abgeschlossen" in caplog.text
    for creds in (OLD_CREDS, NEW_CREDS):
        assert str(creds) not in caplog.text


async def test_reregistration_is_rate_limited(
    hass: HomeAssistant, fcm: FakeFcm, freezer: FrozenDateTimeFactory
) -> None:
    """B: Hilft die Neuregistrierung nicht, entsteht kein Registrierungssturm."""
    fcm.poisoned = [OLD_CREDS, NEW_CREDS]
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub

    # Eine Stunde lang alle 30 Sekunden prüfen.
    for _ in range(120):
        await _tick(hass, freezer, 30)

    assert hub.reregistrations == 2  # sofort und frühestens nach 30 Minuten
    assert len(fcm.starts) <= 15  # ohne Backoff wären es über 100
    assert hass.states.get(PUSH_ENTITY).state == "off"
    # Die Abfrage läuft als Rückfallebene weiter.
    assert hub.last_poll is not None


async def test_token_refresh_does_not_reload(hass: HomeAssistant, fcm: FakeFcm) -> None:
    """C: Nur geänderte Optionen laden den Eintrag neu, Token und Zugangsdaten nicht."""
    entry = await _setup(hass, fcm)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "token": {"access_token": "neu"}}
        )
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_LISTEN_CREDENTIALS: NEW_CREDS}
        )
        await hass.async_block_till_done()
        reload.assert_not_called()

        hass.config_entries.async_update_entry(entry, options={CONF_POLL_INTERVAL: 20})
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)
