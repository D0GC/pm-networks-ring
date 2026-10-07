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
from homeassistant.helpers.event import async_track_time_interval
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

LISTENER_WATCHDOG = timedelta(minutes=2)


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
        self.listener = _RawListener(
            self,
            ring,
            listen_credentials,
            self._listen_credentials_updated,
        )
        self.last_ding: DingInfo | None = None
        self.last_push: ParsedPush | None = None
        self.last_push_received: datetime | None = None
        self.push_count = 0
        self.ding_count = {SOURCE_PUSH: 0, SOURCE_POLL: 0}
        self.recent_pushes: deque[dict[str, Any]] = deque(maxlen=20)
        self.last_poll: datetime | None = None
        self.last_poll_error: str | None = None
        self.last_push_error: str | None = None
        self._seen_ids: deque[str] = deque(maxlen=50)
        self._poll_seeded = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._poll_lock = asyncio.Lock()
        self.audio = AudioSessions(hass, self)

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
        """Push-Empfang aktiv."""
        return bool(self.listener.started)

    # ------------------------------------------------------------------ Ablauf
    async def async_start(self) -> None:
        """Push-Empfang und Abfrage starten."""
        # Push im Hintergrund starten: Ein Fehler bei Google/Ring darf die
        # Abfrage als Rückfallebene nicht verhindern.
        self.entry.async_create_background_task(
            self.hass, self._async_start_listener(), "pm_ring_intercom Push-Start"
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
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.audio.async_close_all()
        if self.listener.started:
            await self.listener.stop()

    async def _async_start_listener(self) -> None:
        try:
            started = await self.listener.start(timeout=30)
        except Exception as err:  # noqa: BLE001 - FCM wirft auch RuntimeError
            self.last_push_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning(
                "Push-Empfang von Ring konnte nicht starten, neuer Versuch in %s: %s",
                LISTENER_WATCHDOG,
                self.last_push_error,
            )
            started = False
        if started:
            self.last_push_error = None
            _LOGGER.info(
                "Push-Empfang für %s aktiv (FCM-Token registriert)", self.intercom.name
            )
        self._send_status()

    async def _async_watchdog(self, _now: datetime) -> None:
        if not self.listener.started:
            _LOGGER.info("Push-Empfang inaktiv, neuer Startversuch")
            await self._async_start_listener()

    @callback
    def _listen_credentials_updated(self, creds: dict[str, Any]) -> None:
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_LISTEN_CREDENTIALS: creds}
        )

    @callback
    def _send_status(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_STATUS.format(self.entry.entry_id))

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
