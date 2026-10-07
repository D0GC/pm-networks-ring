"""Konstanten für PM Ring Intercom."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "pm_ring_intercom"

CONF_TOKEN: Final = "token"
CONF_HARDWARE_ID: Final = "hardware_id"
CONF_LISTEN_CREDENTIALS: Final = "listen_credentials"
CONF_INTERCOM_ID: Final = "intercom_id"

CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_RING_DURATION: Final = "ring_duration"
CONF_DEDUP_WINDOW: Final = "dedup_window"

DEFAULT_POLL_INTERVAL: Final = 15
MIN_POLL_INTERVAL: Final = 10
DEFAULT_RING_DURATION: Final = 30
DEFAULT_DEDUP_WINDOW: Final = 90

# Dings, die älter sind, werden bei der Abfrage nicht mehr gemeldet.
MAX_POLL_DING_AGE: Final = 300

USER_AGENT: Final = "PMNetwork-HomeControl/pm_ring_intercom"

EVENT_DING: Final = f"{DOMAIN}_ding"

SIGNAL_DING: Final = f"{DOMAIN}_ding_{{}}"
SIGNAL_PUSH: Final = f"{DOMAIN}_push_{{}}"
SIGNAL_STATUS: Final = f"{DOMAIN}_status_{{}}"

SOURCE_PUSH: Final = "push"
SOURCE_POLL: Final = "abfrage"
SOURCE_TEST: Final = "test"

CARD_URL: Final = f"/{DOMAIN}/pm-intercom-card.js"

PLATFORMS: Final = ["binary_sensor", "event", "sensor"]
