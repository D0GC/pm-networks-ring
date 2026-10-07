"""Tests für die Auswertung der Ring-Push-Nachrichten."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

_PATH = (
    Path(__file__).parents[1] / "custom_components" / "pm_ring_intercom" / "parser.py"
)
_spec = importlib.util.spec_from_file_location("pm_parser", _PATH)
parser = importlib.util.module_from_spec(_spec)
sys.modules["pm_parser"] = parser
_spec.loader.exec_module(parser)

INTERCOM_ID = 123456


def _current(category: str, *, device_id=INTERCOM_ID, kind="intercom_handset_audio",
             subtype="ding", ding=True) -> dict:
    event = {"ding": {"id": "7400000000000000001", "created_at": "2026-10-07T12:19:19.000Z",
                      "subtype": subtype}} if ding else {"type": category}
    return {
        "data": {
            "android_config": json.dumps({"category": category}),
            "data": json.dumps({
                "device": {"id": device_id, "name": "Haustür", "kind": kind},
                "event": event,
            }),
        }
    }


def test_known_intercom_category_is_ding():
    p = parser.parse_push(_current("com.ring.pn.live-event.intercom"))
    assert p.is_ding
    assert p.device_id == INTERCOM_ID
    assert p.created_at.isoformat() == "2026-10-07T12:19:19+00:00"


def test_unknown_call_category_is_ding():
    # ring_doorbell würde diese Kategorie als "Unknown" verwerfen.
    p = parser.parse_push(_current("com.ring.pn.live-event.ding-call", subtype="call"))
    assert p.is_ding
    assert parser.matches_intercom(p, INTERCOM_ID, single_intercom=True)


def test_unlock_is_not_ding():
    p = parser.parse_push(_current("com.ring.pn.intercom.virtual.unlock", subtype="unlock"))
    assert p.kind == parser.KIND_UNLOCK


def test_motion_is_not_ding():
    p = parser.parse_push(_current("com.ring.pn.live-event.motion", subtype="human"))
    assert p.kind == parser.KIND_MOTION


def test_other_device_does_not_match():
    p = parser.parse_push(_current("com.ring.pn.live-event.ding", device_id=999))
    assert p.is_ding
    assert not parser.matches_intercom(p, INTERCOM_ID, single_intercom=True)


def test_missing_ding_block_does_not_crash():
    p = parser.parse_push(_current("com.ring.pn.live-event.intercom", ding=False))
    assert p.is_ding
    assert p.ding_id is None


def test_legacy_gcm_ding():
    gcm = {"action": "com.ring.push.HANDLE_NEW_DING", "subtype": "ding",
           "ding": {"id": 42, "doorbot_id": INTERCOM_ID, "device_kind": "intercom_handset_audio",
                    "created_at": "2026-10-07T12:19:19Z"}}
    p = parser.parse_push({"data": {"gcmData": json.dumps(gcm)}})
    assert p.is_ding
    assert p.ding_id == "42"
    assert p.fmt == "gcmData"


def test_legacy_unlock():
    gcm = {"action": "com.ring.push.INTERCOM_UNLOCK_FROM_APP",
           "alarm_meta": {"device_zid": INTERCOM_ID}}
    p = parser.parse_push({"data": {"gcmData": json.dumps(gcm)}})
    assert p.kind == parser.KIND_UNLOCK


def test_alarm_message_is_other():
    p = parser.parse_push({"data": {"android_config": json.dumps(
        {"category": "com.ring.push.HANDLE_NEW_SECURITY_PANEL_MODE_NONE_NOTICE"})}})
    assert p.kind == parser.KIND_OTHER


def test_garbage_does_not_crash():
    assert parser.parse_push({"data": {"foo": "bar"}}).kind == parser.KIND_OTHER
    assert parser.parse_push({"data": "kaputt"}).kind == parser.KIND_OTHER


def test_redact():
    out = parser.redact({"token": "x", "nested": json.dumps({"location_id": "y", "a": 1})})
    assert out["token"] == "**geschwärzt**"
    assert out["nested"]["location_id"] == "**geschwärzt**"
    assert out["nested"]["a"] == 1
