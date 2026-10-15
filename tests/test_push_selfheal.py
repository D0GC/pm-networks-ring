"""Härtung von firebase_messaging, Anmeldung bei Ring und stille Push-Ausfälle."""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.pm_ring_intercom import fcm_patch
from custom_components.pm_ring_intercom.const import SOURCE_POLL, SOURCE_PUSH
from custom_components.pm_ring_intercom.hub import DingInfo

from .test_push_watchdog import NEW_CREDS, OLD_CREDS, PUSH_ENTITY, FakeFcm, _setup, _tick
from .test_push_watchdog import fcm  # noqa: F401 - Fixture


def test_normalisiere_padding_and_names() -> None:
    """dh und salt werden nach Namen gefunden und aufgefüllt."""
    roh = base64.urlsafe_b64encode(b"\x01" * 65).decode().rstrip("=")
    salz = base64.urlsafe_b64encode(b"\x02" * 16).decode().rstrip("=")
    # firebase_messaging schneidet "dh=" bzw. "salt=" per Position ab.
    dh, salt = fcm_patch.normalisiere(f"{roh};p256ecdsa=abc", salz)
    assert base64.urlsafe_b64decode(dh) == b"\x01" * 65
    assert base64.urlsafe_b64decode(salt) == b"\x02" * 16
    # dh steht nicht vorne: Positionsschnitt liefert Unsinn, die Namenssuche nicht.
    dh, _ = fcm_patch.normalisiere(f"cdsa=abc;dh={roh}", salz)
    assert base64.urlsafe_b64decode(dh) == b"\x01" * 65


def test_anwenden_hardens_library() -> None:
    """Die Härtung greift an der echten Bibliothek und ist idempotent."""
    from firebase_messaging.fcmpushclient import FcmPushClient

    assert fcm_patch.anwenden() is True
    assert fcm_patch.anwenden() is True
    assert FcmPushClient._pm_ring_intercom_gehaertet is True


def test_kaputte_nachricht_beendet_client_nicht() -> None:
    """Eine nicht auswertbare Nachricht wird verworfen statt den Client zu beenden."""
    from firebase_messaging.fcmpushclient import FcmPushClient

    fcm_patch.anwenden()
    vorher = fcm_patch.STATISTIK["verworfen"]

    class Kaputt:
        persistent_id = "0:1"
        def __getattr__(self, name: str) -> Any:
            raise ValueError("Incorrect padding")

    client = object.__new__(FcmPushClient)
    FcmPushClient._handle_data_message(client, Kaputt())
    assert fcm_patch.STATISTIK["verworfen"] == vorher + 1
    assert fcm_patch.STATISTIK["letzter_fehler"]


def _poll_ding(ding_id: str) -> DingInfo:
    now = dt_util.utcnow()
    return DingInfo(source=SOURCE_POLL, ding_id=ding_id, created_at=now - timedelta(seconds=37), received=now)


async def test_hourly_resubscribe(
    hass: HomeAssistant, fcm: FakeFcm, freezer: FrozenDateTimeFactory  # noqa: F811
) -> None:
    """Das Token wird Ring stündlich erneut mitgeteilt, ohne den Empfang anzuhalten."""
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub
    hub.listener.fcm_token = "token"
    hub.listener.add_subscription_to_ring = AsyncMock()
    hub.listener.subscribed = True

    await _tick(hass, freezer, 3601)

    hub.listener.add_subscription_to_ring.assert_awaited_once_with("token")
    fcm.ring.async_create_session.assert_awaited()
    assert fcm.starts == [OLD_CREDS]
    state = hass.states.get(PUSH_ENTITY)
    assert state.attributes["push_ring_anmeldung_ok"] is True
    assert state.attributes["push_ring_anmeldung_um"] is not None


async def test_silent_failure_resubscribes_then_reregisters(
    hass: HomeAssistant, fcm: FakeFcm, freezer: FrozenDateTimeFactory  # noqa: F811
) -> None:
    """Klingeln nur per Abfrage trotz Verbindung: erst neu anmelden, beim zweiten Mal neu registrieren."""
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub
    hub.listener.fcm_token = "token"
    hub.listener.add_subscription_to_ring = AsyncMock()
    hub.listener.subscribed = True
    assert hub.push_connected

    hub.report_ding(_poll_ding("1"))
    await hass.async_block_till_done()
    hub.listener.add_subscription_to_ring.assert_awaited_once()
    assert hub.silent_failures == 1
    assert hub.reregistrations == 0
    assert fcm.starts == [OLD_CREDS]

    await _tick(hass, freezer, 600)
    hub.listener.fcm_token = "token"
    hub.report_ding(_poll_ding("2"))
    await hass.async_block_till_done()
    assert hub.silent_failures == 2
    assert hub.reregistrations == 1
    assert fcm.starts == [OLD_CREDS, None]
    assert hass.states.get(PUSH_ENTITY).attributes["push_stille_ausfaelle"] == 2

    # Ein dritter Ausfall kurz danach registriert nicht erneut, er meldet nur neu an.
    await _tick(hass, freezer, 300)
    hub.listener.fcm_token = "token"
    hub.listener.add_subscription_to_ring = AsyncMock()
    hub.listener.subscribed = True
    hub.report_ding(_poll_ding("3"))
    await hass.async_block_till_done()
    assert hub.reregistrations == 1
    hub.listener.add_subscription_to_ring.assert_awaited_once()


async def test_push_ding_is_no_silent_failure(
    hass: HomeAssistant, fcm: FakeFcm  # noqa: F811
) -> None:
    """Ein per Push gemeldetes Klingeln löst nichts aus."""
    entry = await _setup(hass, fcm)
    hub = entry.runtime_data.hub
    now = dt_util.utcnow()
    hub.report_ding(DingInfo(source=SOURCE_PUSH, ding_id="9", created_at=now, received=now))
    await hass.async_block_till_done()
    assert hub.silent_failures == 0
    assert NEW_CREDS not in fcm.starts

