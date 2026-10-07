"""Einrichtungsdialog für PM Ring Intercom."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
import uuid

from ring_doorbell import Ring
from ring_doorbell.exceptions import AuthenticationError, Requires2FAError, RingError
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import create_auth
from .const import (
    CONF_DEDUP_WINDOW,
    CONF_HARDWARE_ID,
    CONF_INTERCOM_ID,
    CONF_POLL_INTERVAL,
    CONF_RING_DURATION,
    CONF_TOKEN,
    DEFAULT_DEDUP_WINDOW,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RING_DURATION,
    DOMAIN,
)
from .parser import INTERCOM_KINDS

_LOGGER = logging.getLogger(__name__)

CONF_OTP = "otp"


class PMRingIntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung über Ring-Anmeldung."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialisieren."""
        self._username: str | None = None
        self._password: str | None = None
        self._hardware_id = str(uuid.uuid4())
        self._token: dict[str, Any] | None = None
        self._intercoms: dict[int, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Optionen."""
        return PMRingIntercomOptionsFlow()

    async def _async_login(self, otp: str | None = None) -> str | None:
        """Anmelden. Gibt einen Fehlerschlüssel oder None zurück."""
        assert self._username and self._password
        auth = create_auth(self.hass, None, None, self._hardware_id)
        try:
            self._token = await auth.async_fetch_token(
                self._username, self._password, otp
            )
        except Requires2FAError:
            return "2fa"
        except AuthenticationError:
            return "invalid_auth"
        except (RingError, TimeoutError):
            _LOGGER.exception("Ring nicht erreichbar")
            return "cannot_connect"
        return None

    async def _async_load_intercoms(self) -> str | None:
        auth = create_auth(self.hass, None, self._token, self._hardware_id)
        ring = Ring(auth)
        try:
            await ring.async_create_session()
            await ring.async_update_devices()
        except (RingError, TimeoutError):
            _LOGGER.exception("Geräteliste nicht abrufbar")
            return "cannot_connect"
        self._intercoms = {
            d.id: d.name for d in ring.devices().other if d.kind in INTERCOM_KINDS
        }
        return None if self._intercoms else "no_intercom"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Zugangsdaten abfragen."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            error = await self._async_login()
            if error == "2fa":
                return await self.async_step_2fa()
            if error is None:
                return await self._async_after_login()
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=self._username or ""): str,
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bestätigungscode abfragen."""
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_login(user_input[CONF_OTP])
            if error is None:
                return await self._async_after_login()
            errors["base"] = "invalid_2fa" if error in ("2fa", "invalid_auth") else error

        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema({vol.Required(CONF_OTP): str}),
            errors=errors,
        )

    async def _async_after_login(self) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(
                entry,
                data={
                    **entry.data,
                    CONF_TOKEN: self._token,
                    CONF_HARDWARE_ID: self._hardware_id,
                },
            )
        error = await self._async_load_intercoms()
        if error:
            return self.async_abort(reason=error)
        if len(self._intercoms) == 1:
            return await self.async_step_intercom(
                {CONF_INTERCOM_ID: str(next(iter(self._intercoms)))}
            )
        return await self.async_step_intercom()

    async def async_step_intercom(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Intercom auswählen."""
        if user_input is not None:
            intercom_id = int(user_input[CONF_INTERCOM_ID])
            await self.async_set_unique_id(str(intercom_id))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._intercoms.get(intercom_id, "Ring Intercom"),
                data={
                    CONF_TOKEN: self._token,
                    CONF_HARDWARE_ID: self._hardware_id,
                    CONF_INTERCOM_ID: intercom_id,
                },
            )

        return self.async_show_form(
            step_id="intercom",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INTERCOM_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=str(k), label=v)
                                for k, v in self._intercoms.items()
                            ]
                        )
                    )
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Erneute Anmeldung."""
        self._hardware_id = entry_data[CONF_HARDWARE_ID]
        return await self.async_step_user()


class PMRingIntercomOptionsFlow(OptionsFlow):
    """Optionen für Abfrage und Klingel-Sensor."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionen anzeigen."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options

        def _number(minimum: int, maximum: int) -> selector.NumberSelector:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=minimum,
                    max=maximum,
                    step=1,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                    ): _number(0, 300),
                    vol.Required(
                        CONF_RING_DURATION,
                        default=options.get(CONF_RING_DURATION, DEFAULT_RING_DURATION),
                    ): _number(5, 300),
                    vol.Required(
                        CONF_DEDUP_WINDOW,
                        default=options.get(CONF_DEDUP_WINDOW, DEFAULT_DEDUP_WINDOW),
                    ): _number(10, 600),
                }
            ),
        )
