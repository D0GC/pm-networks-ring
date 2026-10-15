"""Zentrale Logik: Push-Empfang, Abfrage als Rückfallebene, Entprellung."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from ring_doorbell import Ring, RingEventListener
from ring_doorbell.exceptions import AuthenticationError, RingError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEDUP_WINDOW,
    CONF_LISTEN_CREDENTIALS,
    CONF_POLL_INTERVAL,
    CONF_RING_DURATION,
    DEFAULT_DEDUP_WINDOW,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RING_DURATION,
    EVENT_DING,
    MAX_POLL_DING_AGE,
    MIN_POLL_INTERVAL,
    SIGNAL_DING,
    SIGNAL_PUSH,
    SIGNAL_STATUS,
    SOURCE_POLL,
    SOURCE_PUSH,
)
from .audio import AudioSessions
from .parser import KIND_DING, ParsedPush, matches_intercom, parse_push, redact

_LOGGER = logging.getLogger(__name__)

# Prüfintervall der Push-Überwachung. Die Prüfung liest nur Zustände, ohne Netzwerk.
LISTENER_WATCHDOG = timedelta(seconds=30)
# Zusätzliche Prüfung kurz nach jedem Start: Eine Giftnachricht beendet den
# FCM-Client innerhalb von Sekunden nach der Anmeldung.
LISTENER_EARLY_CHECK = 15
# Ist der FCM-Client so lange nicht angemeldet, gilt er als hängend.
LISTENER_CONNECT_TIMEOUT = timedelta(minutes=3)
# Wartezeit vor erneutem Start nach wiederholten Ausfällen (verdoppelt sich).
RESTART_BACKOFF_BASE = timedelta(seconds=30)
RESTART_BACKOFF_MAX = timedelta(minutes=15)
# So viele Ausfälle des Empfängers in Folge, ohne dass er dazwischen
# STABLE_RUNTIME lang lief, lösen eine Neuregistrierung bei FCM aus.
REREGISTER_AFTER_FAILURES = 2
# Mindestabstand zwischen zwei Neuregistrierungen (verdoppelt sich).
REREGISTER_BACKOFF_BASE = timedelta(minutes=30)
REREGISTER_BACKOFF_MAX = timedelta(hours=12)
# Nach so langer ungestörter Laufzeit gelten Ausfälle als ausgestanden.
STABLE_RUNTIME = timedelta(minutes=10)
STABLE_RUNTIME_REREGISTER = timedelta(hours=6)

# Zustände von firebase_messaging.FcmPushClientRunState, in denen der Client
# nichts mehr empfängt. Abgleich über den Namen, damit eine geänderte
# Bibliothek nicht zum Absturz führt.
_RECEIVER_DEAD_STATES = frozenset({"STOPPING", "STOPPED"})
_FCM_LOGGER = "firebase_messaging"


class _FcmErrorCapture(logging.Handler):
    """Merkt sich die letzte Fehlermeldung von firebase_messaging.

    Der FCM-Client beendet sich bei einem Fehler nur mit einer Logzeile. Die
    erste Zeile davon wird als verständlicher Grund für `push_fehler`
    übernommen. Sie enthält keine Zugangsdaten (nur Ausnahmetext).
    """

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.last_error: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - Logging darf nie stören
            return
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        self.last_error = first_line[:160] or None


@dataclass
class DingInfo:
    """Ein erkanntes Klingeln."""

    source: str
    received: datetime
    created_at: datetime | None = None
    ding_id: str | None = None
    category: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def delay(self) -> float | None:
        """Verzögerung zwischen Klingeln bei Ring und Erfassung in HA."""
        if self.created_at is None:
            return None
        return round((self.received - self.created_at).total_seconds(), 1)

    def as_event_data(self) -> dict[str, Any]:
        """Daten für Bus-Ereignis und Attribute."""
        return {
            "quelle": self.source,
            "ding_id": self.ding_id,
            "kategorie": self.category,
            "geklingelt_um": self.created_at.isoformat() if self.created_at else None,
            "erfasst_um": self.received.isoformat(),
            "verzoegerung_s": self.delay,
            **self.extra,
        }


class _RawListener(RingEventListener):
    """Event-Listener, der jede Rohnachricht an den Hub gibt.

    Die Auswertung von ring_doorbell wird bewusst umgangen, weil sie
    unbekannte Kategorien verwirft.
    """

    def __init__(self, hub: IntercomHub, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._hub = hub

    def _on_notification(
        self,
        notification: dict[str, Any],
        persistent_id: str,
        obj: Any | None = None,
    ) -> None:
        self._hub.hass.loop.call_soon_threadsafe(self._hub.handle_raw_push, notification)


class IntercomHub:
    """Verwaltet Empfang und Weitergabe der Klingel-Ereignisse."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        ring: Ring,
        intercom: Any,
        *,
        single_intercom: bool,
        listen_credentials: dict[str, Any] | None,
    ) -> None:
        """Initialisieren."""
        self.hass = hass
        self.entry = entry
        self.ring = ring
        self.intercom = intercom
        self.single_intercom = single_intercom
        self.listener = self._create_listener(listen_credentials)
        self.last_ding: DingInfo | None = None
        self.last_push: ParsedPush | None = None
        self.last_push_received: datetime | None = None
        self.push_count = 0
        self.ding_count = {SOURCE_PUSH: 0, SOURCE_POLL: 0}
        self.recent_pushes: deque[dict[str, Any]] = deque(maxlen=20)
        self.last_poll: datetime | None = None
        self.last_poll_error: str | None = None
        self.last_push_error: str | None = None
        self.last_push_error_at: datetime | None = None
        self.push_connected_since: datetime | None = None
        self.watchdog_restarts = 0
        self.reregistrations = 0
        self._seen_ids: deque[str] = deque(maxlen=50)
        self._poll_seeded = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._poll_lock = asyncio.Lock()
        # Push-Überwachung
        self._listener_lock = asyncio.Lock()
        self._stopping = False
        self._started_at: datetime | None = None
        self._not_connected_since: datetime | None = None
        self._next_start_attempt: datetime | None = None
        self._restart_attempts = 0
        self._receiver_failures = 0
        self._reregister_count = 0
        self._last_reregister: datetime | None = None
        self._reported_connected: bool | None = None
        self._cancel_early_check: CALLBACK_TYPE | None = None
        self._fcm_errors = _FcmErrorCapture()
        self.audio = AudioSessions(hass, self)

    def _create_listener(self, credentials: dict[str, Any] | None) -> _RawListener:
        """Listener anlegen; ohne Zugangsdaten registriert er sich neu bei FCM."""
        return _RawListener(
            self,
            self.ring,
            credentials,
            self._listen_credentials_updated,
        )

    # ------------------------------------------------------------------ Optionen
    @property
    def poll_interval(self) -> int:
        """Abfrageintervall in Sekunden (0 = aus)."""
        value = int(self.entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        return 0 if value <= 0 else max(value, MIN_POLL_INTERVAL)

    @property
    def ring_duration(self) -> int:
        """Dauer, die der Klingel-Sensor eingeschaltet bleibt."""
        return int(self.entry.options.get(CONF_RING_DURATION, DEFAULT_RING_DURATION))

    @property
    def dedup_window(self) -> int:
        """Zeitfenster, in dem ein zweites Klingeln als Duplikat gilt."""
        return int(self.entry.options.get(CONF_DEDUP_WINDOW, DEFAULT_DEDUP_WINDOW))

    @property
    def listener_started(self) -> bool:
        """Startmerker von ring_doorbell.

        Bleibt True, auch wenn sich der FCM-Client inzwischen beendet hat.
        Für den tatsächlichen Zustand `push_connected` verwenden.
        """
        return bool(self.listener.started)

    @property
    def push_connected(self) -> bool:
        """Push-Empfang tatsächlich aktiv: gestartet, lebt und bei FCM angemeldet."""
        if not self.listener.started:
            return False
        receiver = getattr(self.listener, "_receiver", None)
        if receiver is None:
            # Unbekannter Aufbau der Bibliothek: dem Startmerker vertrauen.
            return True
        if self._receiver_failure(receiver) is not None:
            return False
        is_started = getattr(receiver, "is_started", None)
        if not callable(is_started):
            return True
        try:
            return bool(is_started())
        except Exception:  # noqa: BLE001 - Bibliotheksänderung darf nicht stören
            return True

    @staticmethod
    def _receiver_failure(receiver: Any) -> str | None:
        """Grund, warum der FCM-Client nichts mehr empfängt, sonst None.

        Liest nur Attribute von firebase_messaging.FcmPushClient, jeweils per
        getattr, damit eine geänderte Bibliothek nicht zum Absturz führt.
        """
        state = getattr(getattr(receiver, "run_state", None), "name", None)
        if state in _RECEIVER_DEAD_STATES:
            return f"FCM-Client hat sich beendet (Zustand {state})"
        if getattr(receiver, "do_listen", True) is False:
            return "FCM-Client hat den Empfang eingestellt"
        tasks = getattr(receiver, "tasks", None)
        if isinstance(tasks, (list, tuple)) and tasks:
            try:
                if all(task.done() for task in tasks):
                    return "Empfangsschleife des FCM-Clients beendet"
            except Exception:  # noqa: BLE001 - nur Zusatzprüfung
                return None
        return None

    # ------------------------------------------------------------------ Ablauf
    async def async_start(self) -> None:
        """Push-Empfang und Abfrage starten."""
        logging.getLogger(_FCM_LOGGER).addHandler(self._fcm_errors)
        # Push im Hintergrund starten: Ein Fehler bei Google/Ring darf die
        # Abfrage als Rückfallebene nicht verhindern.
        self.entry.async_create_background_task(
            self.hass, self._async_initial_start(), "pm_ring_intercom Push-Start"
        )
        if self.poll_interval:
            await self._async_poll(seed=True)
            self._unsubs.append(
                async_track_time_interval(
                    self.hass,
                    self._async_poll_interval,
                    timedelta(seconds=self.poll_interval),
                    name=f"{self.entry.title} Klingel-Abfrage",
                )
            )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_watchdog,
                LISTENER_WATCHDOG,
                name=f"{self.entry.title} Push-Überwachung",
            )
        )

    async def async_stop(self) -> None:
        """Alles beenden."""
        self._stopping = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_early()
        logging.getLogger(_FCM_LOGGER).removeHandler(self._fcm_errors)
        await self.audio.async_close_all()
        await self._async_stop_listener()

    async def _async_initial_start(self) -> None:
        async with self._listener_lock:
            await self._async_start_listener()

    async def _async_stop_listener(self) -> None:
        """Listener anhalten; Fehler der Bibliothek beim Anhalten ignorieren."""
        try:
            await self.listener.stop()
        except Exception as err:  # noqa: BLE001 - Anhalten darf nie scheitern
            _LOGGER.debug("Push-Empfang ließ sich nicht sauber anhalten: %s", err)

    async def _async_start_listener(self) -> bool:
        """Push-Empfang starten. Aufrufer hält `_listener_lock`."""
        if self._stopping:
            return False
        self._fcm_errors.last_error = None
        try:
            started = await self.listener.start(timeout=30)
        except Exception as err:  # noqa: BLE001 - FCM wirft auch RuntimeError
            self._set_push_error(f"{type(err).__name__}: {err}")
            started = False
        else:
            if not started:
                self._set_push_error(
                    self._fcm_errors.last_error
                    or "Anmeldung des Push-Empfangs bei FCM oder Ring fehlgeschlagen"
                )

        now = dt_util.utcnow()
        if self._stopping:
            await self._async_stop_listener()
            return False
        if started:
            self._started_at = now
            self._not_connected_since = None
            self._next_start_attempt = None
            _LOGGER.info(
                "Push-Empfang für %s gestartet (FCM-Token registriert)", self.intercom.name
            )
            # Eine Giftnachricht beendet den Client binnen Sekunden; früh nachsehen.
            self._cancel_early()
            self._cancel_early_check = async_call_later(
                self.hass, LISTENER_EARLY_CHECK, self._async_early_check
            )
        else:
            # Halb gestartete Empfänger-Tasks nicht weiterlaufen lassen.
            await self._async_stop_listener()
            self._restart_attempts += 1
            delay = self._restart_delay()
            self._next_start_attempt = now + delay
            _LOGGER.warning(
                "Push-Empfang von Ring konnte nicht starten, neuer Versuch in %s s: %s",
                int(delay.total_seconds()),
                self.last_push_error,
            )
        self._send_status()
        return started

    def _restart_delay(self) -> timedelta:
        """Wartezeit vor dem nächsten Start; der erste Neustart erfolgt sofort."""
        if self._restart_attempts <= 1:
            return timedelta(0)
        delay = RESTART_BACKOFF_BASE * (2 ** (self._restart_attempts - 2))
        return min(delay, RESTART_BACKOFF_MAX)

    def _reregister_allowed(self, now: datetime) -> bool:
        """Neuregistrierung fällig und nicht durch den Mindestabstand gesperrt."""
        if self._receiver_failures < REREGISTER_AFTER_FAILURES:
            return False
        if self._last_reregister is None:
            return True
        wait = min(
            REREGISTER_BACKOFF_BASE * (2 ** max(self._reregister_count - 1, 0)),
            REREGISTER_BACKOFF_MAX,
        )
        return now - self._last_reregister >= wait

    @callback
    def _cancel_early(self) -> None:
        if self._cancel_early_check:
            self._cancel_early_check()
            self._cancel_early_check = None

    async def _async_early_check(self, _now: datetime) -> None:
        self._cancel_early_check = None
        await self._async_watchdog(_now)

    async def _async_watchdog(self, _now: datetime | None = None) -> None:
        """Empfänger prüfen, bei Ausfall neu starten oder neu registrieren."""
        if self._stopping or self._listener_lock.locked():
            return
        async with self._listener_lock:
            now = dt_util.utcnow()
            if self.listener.started:
                reason, counts = self._check_receiver(now)
                if reason is None:
                    self._send_status_if_changed()
                    return
                await self._async_handle_failure(now, reason, counts_for_reregister=counts)

            if self._reregister_allowed(now):
                await self._async_reregister(now)
                return
            if self._next_start_attempt is not None and now < self._next_start_attempt:
                self._send_status_if_changed()
                return
            _LOGGER.info("Push-Überwachung: neuer Startversuch des Push-Empfangs")
            await self._async_start_listener()

    def _check_receiver(self, now: datetime) -> tuple[str | None, bool]:
        """Zustand des laufenden Empfängers bewerten.

        Rückgabe: Ausfallgrund (None = in Ordnung) und ob der Ausfall für eine
        Neuregistrierung zählt. Ein hängender Verbindungsaufbau zählt nicht,
        weil er auf Netzprobleme deutet, nicht auf eine Giftnachricht.
        """
        receiver = getattr(self.listener, "_receiver", None)
        if receiver is None:
            return None, False
        if (failure := self._receiver_failure(receiver)) is not None:
            if detail := self._fcm_errors.last_error:
                failure = f"{failure}: {detail}"
            return failure, True

        is_started = getattr(receiver, "is_started", None)
        connected = True
        if callable(is_started):
            try:
                connected = bool(is_started())
            except Exception:  # noqa: BLE001 - Bibliotheksänderung darf nicht stören
                connected = True
        if not connected:
            if self._not_connected_since is None:
                self._not_connected_since = now
            elif now - self._not_connected_since >= LISTENER_CONNECT_TIMEOUT:
                minutes = int(LISTENER_CONNECT_TIMEOUT.total_seconds() // 60)
                return f"FCM-Client seit über {minutes} Minuten nicht angemeldet", False
            return None, False

        self._not_connected_since = None
        if self.push_connected_since is None:
            self.push_connected_since = now
        if self._started_at is not None:
            runtime = now - self._started_at
            if runtime >= STABLE_RUNTIME:
                # Ausfälle sind ausgestanden: Backoff und Fehler zurücksetzen.
                self._restart_attempts = 0
                self._receiver_failures = 0
                if self.last_push_error is not None:
                    self.last_push_error = None
                    self._send_status()
            if runtime >= STABLE_RUNTIME_REREGISTER:
                self._reregister_count = 0
        return None, False

    async def _async_handle_failure(
        self, now: datetime, reason: str, *, counts_for_reregister: bool
    ) -> None:
        """Toten Empfänger anhalten und Ausfall vermerken."""
        runtime = (
            int((now - self._started_at).total_seconds()) if self._started_at else None
        )
        self._set_push_error(reason)
        self.watchdog_restarts += 1
        self._restart_attempts += 1
        if counts_for_reregister:
            self._receiver_failures += 1
        delay = self._restart_delay()
        self._next_start_attempt = now + delay
        _LOGGER.warning(
            "Push-Überwachung: Empfang von Ring ausgefallen (%s; %s s nach dem Start). "
            "Push-Empfang wird angehalten und in %s s neu gestartet, Klingeln kommt "
            "bis dahin nur über die Abfrage",
            reason,
            runtime,
            int(delay.total_seconds()),
        )
        self._cancel_early()
        await self._async_stop_listener()
        self._started_at = None
        self._not_connected_since = None
        self.push_connected_since = None
        self._send_status()

    async def _async_reregister(self, now: datetime) -> None:
        """Gespeicherte FCM-Zugangsdaten verwerfen und neu registrieren.

        Eine Nachricht, an der die Entschlüsselung scheitert, wird nie bestätigt
        und bei jeder Anmeldung erneut zugestellt. Mit einer neuen Registrierung
        verfällt die alte samt dieser Nachricht. ring_doorbell registriert sich
        ohne Zugangsdaten neu bei FCM, meldet die neuen Daten über den Callback
        und trägt das neue Token per Sitzungs-PATCH bei Ring ein.
        """
        self._reregister_count += 1
        self.reregistrations += 1
        self._last_reregister = now
        self._receiver_failures = 0
        _LOGGER.warning(
            "Push-Überwachung: Push-Empfang fällt wiederholt kurz nach dem Start aus "
            "(zuletzt: %s). Gespeicherte FCM-Zugangsdaten werden verworfen, "
            "Neuregistrierung bei FCM und Ring (Versuch %s)",
            self.last_push_error,
            self._reregister_count,
        )
        await self._async_stop_listener()
        self._discard_listen_credentials()
        self.listener = self._create_listener(None)
        if await self._async_start_listener():
            _LOGGER.warning(
                "Push-Überwachung: Neuregistrierung abgeschlossen, neues FCM-Token bei "
                "Ring angemeldet"
            )

    @callback
    def _discard_listen_credentials(self) -> None:
        if CONF_LISTEN_CREDENTIALS not in self.entry.data:
            return
        data = {k: v for k, v in self.entry.data.items() if k != CONF_LISTEN_CREDENTIALS}
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    @callback
    def _set_push_error(self, reason: str) -> None:
        self.last_push_error = reason
        self.last_push_error_at = dt_util.utcnow()

    @callback
    def _listen_credentials_updated(self, creds: dict[str, Any]) -> None:
        """Neue FCM-Zugangsdaten speichern (löst kein Neuladen aus)."""
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_LISTEN_CREDENTIALS: creds}
        )

    @callback
    def _send_status(self) -> None:
        self._reported_connected = self.push_connected
        async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.entry.entry_id))

    @callback
    def _send_status_if_changed(self) -> None:
        if self.push_connected != self._reported_connected:
            self._send_status()

    # ------------------------------------------------------------------ Push
    @callback
    def handle_raw_push(self, notification: dict[str, Any]) -> None:
        """Rohe Push-Nachricht verarbeiten (läuft im Event-Loop)."""
        now = dt_util.utcnow()
        try:
            parsed = parse_push(notification)
        except Exception:  # noqa: BLE001 - nichts darf den Empfang abbrechen
            _LOGGER.exception("Push-Nachricht nicht auswertbar: %s", notification)
            return

        self.push_count += 1
        self.last_push = parsed
        self.last_push_received = now
        self.recent_pushes.append(
            {
                "empfangen": now.isoformat(),
                "art": parsed.kind,
                "kategorie": parsed.category,
                "format": parsed.fmt,
                "geraet_id": parsed.device_id,
                "geraet_art": parsed.device_kind,
                "subtype": parsed.subtype,
                "roh": redact(parsed.raw),
            }
        )
        _LOGGER.info(
            "Ring-Push: art=%s kategorie=%s geraet=%s (%s) subtype=%s",
            parsed.kind,
            parsed.category,
            parsed.device_id,
            parsed.device_kind,
            parsed.subtype,
        )
        _LOGGER.debug("Ring-Push roh: %s", redact(parsed.raw))
        async_dispatcher_send(self.hass, SIGNAL_PUSH.format(self.entry.entry_id))

        if parsed.kind != KIND_DING:
            return
        if not matches_intercom(
            parsed, self.intercom.id, single_intercom=self.single_intercom
        ):
            _LOGGER.debug("Klingeln eines anderen Geräts ignoriert: %s", parsed.device_id)
            return

        self.report_ding(
            DingInfo(
                source=SOURCE_PUSH,
                received=now,
                created_at=parsed.created_at,
                ding_id=parsed.ding_id,
                category=parsed.category,
                extra={"subtype": parsed.subtype},
            )
        )

    # ------------------------------------------------------------------ Abfrage
    async def _async_poll_interval(self, _now: datetime) -> None:
        await self._async_poll(seed=False)

    async def _async_poll(self, *, seed: bool) -> None:
        if self._poll_lock.locked():
            return
        async with self._poll_lock:
            try:
                history = await self.intercom.async_history(limit=5)
            except AuthenticationError:
                self.last_poll_error = "Anmeldung abgelaufen"
                self.entry.async_start_reauth(self.hass)
                return
            except (RingError, TimeoutError, KeyError, ValueError) as err:
                self.last_poll_error = str(err) or type(err).__name__
                _LOGGER.debug("Abfrage der Intercom-Historie fehlgeschlagen: %s", err)
                return
            self.last_poll = dt_util.utcnow()
            self.last_poll_error = None

        now = dt_util.utcnow()
        for item in reversed(history or []):
            kind = str(item.get("kind", "")).lower()
            if "ding" not in kind:
                continue
            ding_id = str(item.get("id"))
            created = item.get("created_at")
            created = created if isinstance(created, datetime) else None
            if seed or not self._poll_seeded:
                self._seen_ids.append(ding_id)
                continue
            if ding_id in self._seen_ids:
                continue
            if created and (now - created).total_seconds() > MAX_POLL_DING_AGE:
                self._seen_ids.append(ding_id)
                continue
            self.report_ding(
                DingInfo(
                    source=SOURCE_POLL,
                    received=now,
                    created_at=created,
                    ding_id=ding_id,
                    category=kind,
                )
            )
        self._poll_seeded = True

    # ------------------------------------------------------------------ Melden
    def _is_duplicate(self, ding: DingInfo) -> bool:
        if ding.ding_id and ding.ding_id in self._seen_ids:
            return True
        last = self.last_ding
        if last is None:
            return False
        ref_new = ding.created_at or ding.received
        ref_old = last.created_at or last.received
        return abs((ref_new - ref_old).total_seconds()) < self.dedup_window

    @callback
    def report_ding(self, ding: DingInfo, *, force: bool = False) -> None:
        """Klingeln melden, sofern es nicht bereits gemeldet wurde."""
        duplicate = self._is_duplicate(ding)
        if ding.ding_id:
            self._seen_ids.append(ding.ding_id)
        if duplicate and not force:
            _LOGGER.debug("Klingeln bereits gemeldet (%s, %s)", ding.source, ding.ding_id)
            return

        self.last_ding = ding
        if ding.source in self.ding_count:
            self.ding_count[ding.source] += 1
        if ding.source == SOURCE_POLL:
            _LOGGER.warning(
                "Klingeln um %s nur über die Abfrage erfasst (kein Push, %s s verzögert)",
                ding.created_at,
                ding.delay,
            )
        else:
            _LOGGER.info("Klingeln erfasst über %s (%s s)", ding.source, ding.delay)

        data = {
            "geraet": self.intercom.name,
            "ring_device_id": self.intercom.id,
            **ding.as_event_data(),
        }
        self.hass.bus.async_fire(EVENT_DING, data)
        async_dispatcher_send(self.hass, SIGNAL_DING.format(self.entry.entry_id), ding)
