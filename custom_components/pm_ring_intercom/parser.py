"""Auswertung roher Ring-Push-Nachrichten.

Bewusst ohne Abhängigkeit zu Home Assistant oder ring_doorbell, damit die
Logik isoliert getestet werden kann.

Hintergrund: ring_doorbell ordnet jede Push-Kategorie über eine feste Tabelle
zu. Unbekannte Kategorien werden zu "Unknown" und von der offiziellen
Integration verworfen. Nachrichten mit abweichendem Aufbau lösen zudem einen
KeyError aus. Dieser Parser arbeitet tolerant: Er sucht Kategorie, Gerät und
Ding-Daten an allen bekannten Stellen und entscheidet über Schlüsselwörter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Any

KIND_DING = "ding"
KIND_UNLOCK = "unlock"
KIND_MOTION = "motion"
KIND_OTHER = "other"

INTERCOM_KINDS = ("intercom_handset_audio", "intercom_handset_video")

# Reihenfolge ist relevant: "unlock" vor "intercom", "motion" vor "ding".
_UNLOCK_WORDS = ("unlock",)
_MOTION_WORDS = ("motion",)
_DING_WORDS = ("ding", "intercom", "call", "ring_event", "doorbell", "press")
_IGNORE_WORDS = ("community_alert", "low_battery", "battery", "offline", "online")


@dataclass
class ParsedPush:
    """Ergebnis der Auswertung einer Push-Nachricht."""

    kind: str
    category: str | None
    device_id: int | None = None
    device_name: str | None = None
    device_kind: str | None = None
    ding_id: str | None = None
    subtype: str | None = None
    created_at: datetime | None = None
    fmt: str = "unbekannt"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ding(self) -> bool:
        """Klingeln erkannt."""
        return self.kind == KIND_DING


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    """ISO-Zeitstempel oder Epoch (s/ms) in UTC-datetime wandeln."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def classify(category: str | None, subtype: str | None = None) -> str:
    """Kategorie bzw. Aktion einer Ring-Nachricht einordnen."""
    text = f"{category or ''} {subtype or ''}".lower()
    if not text.strip():
        return KIND_OTHER
    if any(word in text for word in _UNLOCK_WORDS):
        return KIND_UNLOCK
    if any(word in text for word in _MOTION_WORDS):
        return KIND_MOTION
    if any(word in text for word in _IGNORE_WORDS):
        return KIND_OTHER
    if any(word in text for word in _DING_WORDS):
        return KIND_DING
    return KIND_OTHER


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def parse_push(notification: dict[str, Any]) -> ParsedPush:
    """Rohe FCM-Nachricht von Ring auswerten."""
    msg_data: dict[str, Any] = notification.get("data") or notification
    if not isinstance(msg_data, dict):
        return ParsedPush(KIND_OTHER, None, raw={"data": msg_data})

    # Altes Format: alles in gcmData.
    if "gcmData" in msg_data:
        gcm = _loads(msg_data["gcmData"])
        if isinstance(gcm, dict):
            return _parse_legacy(gcm, msg_data)

    return _parse_current(msg_data)


def _parse_legacy(gcm: dict[str, Any], msg_data: dict[str, Any]) -> ParsedPush:
    action = gcm.get("action")
    ding = gcm.get("ding") if isinstance(gcm.get("ding"), dict) else {}
    alarm_meta = gcm.get("alarm_meta") if isinstance(gcm.get("alarm_meta"), dict) else {}
    subtype = gcm.get("subtype")
    kind = classify(action, subtype)
    if kind == KIND_OTHER and ding and "community_alert" not in gcm:
        kind = classify("ding", subtype)
    return ParsedPush(
        kind=kind,
        category=action,
        device_id=_to_int(_first(ding.get("doorbot_id"), alarm_meta.get("device_zid"))),
        device_name=ding.get("device_name"),
        device_kind=ding.get("device_kind"),
        ding_id=_str_or_none(ding.get("id")),
        subtype=subtype,
        created_at=parse_datetime(ding.get("created_at")),
        fmt="gcmData",
        raw=msg_data,
    )


def _parse_current(msg_data: dict[str, Any]) -> ParsedPush:
    android_config = _loads(msg_data.get("android_config"))
    android_config = android_config if isinstance(android_config, dict) else {}
    data = _loads(msg_data.get("data"))
    data = data if isinstance(data, dict) else {}

    device = data.get("device") if isinstance(data.get("device"), dict) else {}
    event = data.get("event") if isinstance(data.get("event"), dict) else {}
    ding = event.get("ding") if isinstance(event.get("ding"), dict) else {}
    if not ding and isinstance(data.get("ding"), dict):
        ding = data["ding"]

    category = _first(
        android_config.get("category"),
        msg_data.get("category"),
        msg_data.get("action"),
        data.get("action"),
        event.get("type"),
    )
    subtype = _first(ding.get("subtype"), event.get("subtype"), data.get("subtype"))
    kind = classify(category, subtype)

    return ParsedPush(
        kind=kind,
        category=category,
        device_id=_to_int(
            _first(device.get("id"), ding.get("doorbot_id"), data.get("doorbot_id"))
        ),
        device_name=_first(device.get("name"), ding.get("device_name")),
        device_kind=_first(device.get("kind"), ding.get("device_kind")),
        ding_id=_str_or_none(_first(ding.get("id"), event.get("id"))),
        subtype=subtype,
        created_at=parse_datetime(
            _first(ding.get("created_at"), event.get("created_at"), data.get("created_at"))
        ),
        fmt="android_config" if android_config else "unbekannt",
        raw=msg_data,
    )


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def matches_intercom(
    parsed: ParsedPush, intercom_id: int, *, single_intercom: bool
) -> bool:
    """Prüfen, ob eine Nachricht zur konfigurierten Intercom gehört."""
    if parsed.device_id is not None:
        return parsed.device_id == intercom_id
    if parsed.device_kind in INTERCOM_KINDS:
        return single_intercom
    category = (parsed.category or "").lower()
    return single_intercom and "intercom" in category


_REDACT_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "push_notification_token",
    "image_uuid",
    "location_id",
    "address",
    "latitude",
    "longitude",
    "email",
    "first_name",
    "last_name",
}


def redact(value: Any) -> Any:
    """Personenbezogene und geheime Felder einer Rohnachricht schwärzen."""
    value = _loads(value)
    if isinstance(value, dict):
        return {
            key: "**geschwärzt**" if key.lower() in _REDACT_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
