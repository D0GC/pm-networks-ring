"""Härtung von firebase_messaging gegen Ring-Nachrichten, die es nicht verarbeiten kann.

firebase_messaging 0.4.5 schneidet ``dh=`` und ``salt=`` per Position aus den
Kopfzeilen ``crypto-key`` und ``encryption`` und dekodiert den Rest ohne
Base64-Auffüllung. Seit Oktober 2026 schickt Ring Nachrichten, an denen das mit
„Incorrect padding“ scheitert. Die Ausnahme beendet den gesamten FCM-Client, und
weil die Nachricht nie bestätigt wird, stellt Google sie bei jeder Anmeldung
erneut zu.

Diese Härtung
1. sucht ``dh`` und ``salt`` nach Namen (auch wenn die Bibliothek schon Zeichen
   abgeschnitten hat) und ergänzt die Auffüllung, bevor die unveränderte
   Entschlüsselung der Bibliothek läuft,
2. fängt Fehler beim Auswerten einer einzelnen Nachricht ab. Die Nachricht wird
   dann verworfen und wie jede andere bestätigt, der Client läuft weiter.

Vorbild sind die nicht übernommenen Upstream-PRs sdb9696/firebase-messaging #37
und #43. Die Härtung wirkt prozessweit, also auch für die offizielle
Ring-Integration, und wird nur angewendet, wenn die erwarteten Methoden
vorhanden sind.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

_MARKER = "_pm_ring_intercom_gehaertet"

# Zähler für die Diagnose (prozessweit, wie die Härtung selbst).
STATISTIK: dict[str, Any] = {"verworfen": 0, "letzter_fehler": None}


def _parameter(wert: str, name: str) -> str:
    """Wert eines Parameters aus einer Kopfzeile wie ``dh=…;p256ecdsa=…``.

    firebase_messaging hat die ersten Zeichen bereits abgeschnitten
    (``[3:]`` bzw. ``[5:]``). Stand der Parameter vorne, ist der erste Teil
    schon der Wert; stand er weiter hinten, findet ihn die Suche nach Namen.
    """
    teile = [t.strip() for t in (wert or "").split(";") if t.strip()]
    for teil in teile:
        if teil.startswith(f"{name}="):
            return teil[len(name) + 1 :]
    return teile[0] if teile else ""


def _aufgefuellt(wert: str) -> str:
    """Base64url-Text auf ein Vielfaches von vier auffüllen."""
    wert = wert.strip()
    return wert + "=" * (-len(wert) % 4)


def normalisiere(crypto_key: str, salt: str) -> tuple[str, str]:
    """``dh`` und ``salt`` herauslösen und auffüllen."""
    return _aufgefuellt(_parameter(crypto_key, "dh")), _aufgefuellt(_parameter(salt, "salt"))


def anwenden() -> bool:
    """Härtung einmalig anwenden. Rückgabe: ob sie aktiv ist."""
    try:
        from firebase_messaging import fcmpushclient  # noqa: PLC0415
    except ImportError:
        return False
    cls = getattr(fcmpushclient, "FcmPushClient", None)
    if cls is None:
        return False
    if getattr(cls, _MARKER, False):
        return True
    original_entschluesseln = getattr(cls, "_decrypt_raw_data", None)
    original_nachricht = getattr(cls, "_handle_data_message", None)
    if not callable(original_entschluesseln) or not callable(original_nachricht):
        _LOGGER.warning(
            "firebase_messaging hat einen unbekannten Aufbau, Härtung nicht angewendet"
        )
        return False

    def entschluesseln(
        credentials: Any, crypto_key_str: str, salt_str: str, raw_data: bytes
    ) -> bytes:
        dh, salt = normalisiere(crypto_key_str, salt_str)
        return original_entschluesseln(credentials, dh, salt, raw_data)

    def nachricht(self: Any, msg: Any) -> None:
        try:
            original_nachricht(self, msg)
        except Exception as err:  # noqa: BLE001 - eine Nachricht darf den Client nicht beenden
            STATISTIK["verworfen"] += 1
            STATISTIK["letzter_fehler"] = f"{type(err).__name__}: {err}"[:160]
            _LOGGER.warning(
                "Push-Nachricht %s nicht auswertbar und verworfen (%s). "
                "Der Push-Empfang läuft weiter",
                getattr(msg, "persistent_id", "?"),
                STATISTIK["letzter_fehler"],
            )

    cls._decrypt_raw_data = staticmethod(entschluesseln)
    cls._handle_data_message = nachricht
    setattr(cls, _MARKER, True)
    _LOGGER.debug("firebase_messaging gegen fehlerhafte Ring-Nachrichten gehärtet")
    return True
