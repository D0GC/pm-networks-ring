"""Anmeldedialog mit Bestätigungscode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from ring_doorbell.exceptions import Requires2FAError

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.pm_ring_intercom.const import DOMAIN


async def test_flow_with_2fa(hass: HomeAssistant) -> None:
    """Anmeldung, Code, automatische Auswahl der einzigen Intercom."""
    auth = MagicMock()
    auth.async_fetch_token = AsyncMock(side_effect=[Requires2FAError, {"access_token": "t"}])
    intercom = MagicMock(id=123456, kind="intercom_handset_audio")
    intercom.name = "Haustür"
    ring = MagicMock()
    ring.async_create_session = AsyncMock()
    ring.async_update_devices = AsyncMock()
    ring.devices.return_value.other = [intercom]

    with (
        patch("custom_components.pm_ring_intercom.config_flow.create_auth", return_value=auth),
        patch("custom_components.pm_ring_intercom.config_flow.Ring", return_value=ring),
        patch("custom_components.pm_ring_intercom.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "a@b.de", "password": "pw"}
        )
        assert result["step_id"] == "2fa"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"otp": "123456"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Haustür"
        assert result["data"]["intercom_id"] == 123456
        assert result["data"]["token"] == {"access_token": "t"}
        assert "password" not in result["data"]
