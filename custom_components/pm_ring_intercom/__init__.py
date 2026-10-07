"""PM Ring Intercom: Klingeln der Ring Intercom live erfassen und gegensprechen."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from ring_doorbell import Auth, Ring
from ring_doorbell.exceptions import AuthenticationError, RingError

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    CARD_URL,
    CONF_HARDWARE_ID,
    CONF_INTERCOM_ID,
    CONF_LISTEN_CREDENTIALS,
    CONF_TOKEN,
    DOMAIN,
    PLATFORMS,
    SOURCE_TEST,
    USER_AGENT,
)
from .audio import async_register_websocket
from .hub import DingInfo, IntercomHub
from .parser import INTERCOM_KINDS

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class RuntimeData:
    """Laufzeitdaten eines Eintrags."""

    ring: Ring
    hub: IntercomHub


type PMRingIntercomConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Karte bereitstellen und Dienste registrieren."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                CARD_URL,
                str(Path(__file__).parent / "www" / "pm-intercom-card.js"),
                cache_headers=False,
            )
        ]
    )
    add_extra_js_url(hass, CARD_URL)
    async_register_websocket(hass)

    async def _simulate_ding(call: ServiceCall) -> None:
        entries = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if getattr(entry, "runtime_data", None)
        ]
        if not entries:
            raise ServiceValidationError("Keine aktive Intercom eingerichtet")
        for entry in entries:
            entry.runtime_data.hub.report_ding(
                DingInfo(source=SOURCE_TEST, received=dt_util.utcnow()), force=True
            )

    hass.services.async_register(DOMAIN, "klingeln_simulieren", _simulate_ding)
    return True


def create_auth(
    hass: HomeAssistant, entry: ConfigEntry | None, token: dict[str, Any] | None, hardware_id: str
) -> Auth:
    """Auth-Objekt mit eigenem Client-Eintrag bei Ring erzeugen."""

    @callback
    def _token_updated(new_token: dict[str, Any]) -> None:
        if entry is not None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_TOKEN: new_token}
            )

    return Auth(
        USER_AGENT,
        token,
        _token_updated,
        hardware_id=hardware_id,
        http_client_session=async_get_clientsession(hass),
    )


async def async_setup_entry(hass: HomeAssistant, entry: PMRingIntercomConfigEntry) -> bool:
    """Eintrag einrichten."""
    auth = create_auth(hass, entry, entry.data[CONF_TOKEN], entry.data[CONF_HARDWARE_ID])
    ring = Ring(auth)
    try:
        await ring.async_create_session()
        await ring.async_update_devices()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed("Ring-Anmeldung abgelaufen") from err
    except (RingError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"Ring nicht erreichbar: {err}") from err

    intercoms = [d for d in ring.devices().other if d.kind in INTERCOM_KINDS]
    intercom = next(
        (d for d in intercoms if d.id == entry.data[CONF_INTERCOM_ID]), None
    )
    if intercom is None:
        raise ConfigEntryNotReady("Konfigurierte Ring Intercom nicht gefunden")

    hub = IntercomHub(
        hass,
        entry,
        ring,
        intercom,
        single_intercom=len(intercoms) == 1,
        listen_credentials=entry.data.get(CONF_LISTEN_CREDENTIALS),
    )
    entry.runtime_data = RuntimeData(ring=ring, hub=hub)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await hub.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PMRingIntercomConfigEntry) -> bool:
    """Eintrag entladen."""
    await entry.runtime_data.hub.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
