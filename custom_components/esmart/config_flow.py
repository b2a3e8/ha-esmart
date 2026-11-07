"""Config flow for the eSMART integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
import voluptuous as vol

from .client import (
    CannotConnect,
    ESmartClient,
    PairingTimeout,
    async_register_account,
    async_remove_account,
    construct_off_loop,
    generate_password,
    generate_username,
)
from .const import (
    CONF_NICKNAME,
    CONF_PAIRING_CODE,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_NICKNAME,
    DOMAIN,
    USERNAME_PREFIX,
)
from .protocol import installation_id

_LOGGER = logging.getLogger(__name__)


class InvalidCode(ValueError):
    """The pairing code isn't the number shown on the wall unit."""


@dataclass(frozen=True)
class PairingResult:
    """Credentials of a freshly paired remote and the installation it joined."""

    username: str
    password: str
    nickname: str
    installation: str

    @property
    def data(self) -> dict[str, str]:
        return {
            CONF_USERNAME: self.username,
            CONF_PASSWORD: self.password,
            CONF_NICKNAME: self.nickname,
        }


def _code_schema(nickname: str | None = None) -> vol.Schema:
    fields: dict[Any, Any] = {vol.Required(CONF_PAIRING_CODE): str}
    if nickname is not None:
        fields[vol.Optional(CONF_NICKNAME, default=nickname)] = str
    return vol.Schema(fields)


class ESmartConfigFlow(ConfigFlow, domain=DOMAIN):
    """Register a remote account and pair it with the wall unit."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair a new installation using the code on the wall unit."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_try_pair(user_input, DEFAULT_NICKNAME, errors)
            if result is not None:
                await self.async_set_unique_id(result.installation)
                if self._unique_id_taken(result.installation):
                    await _async_discard_account(result)
                    return self.async_abort(reason="already_configured")
                return self.async_create_entry(title="eSMART", data=result.data)

        return self.async_show_form(
            step_id="user", data_schema=_code_schema(DEFAULT_NICKNAME), errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The wall unit no longer accepts this remote."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again with a new code."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            nickname = entry.data.get(CONF_NICKNAME) or DEFAULT_NICKNAME
            result = await self._async_try_pair(user_input, nickname, errors)
            if result is not None:
                return await self._async_replace_credentials(entry, result)
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_code_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again on purpose, e.g. after the remote was deleted."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        nickname = entry.data.get(CONF_NICKNAME) or DEFAULT_NICKNAME
        if user_input is not None:
            result = await self._async_try_pair(user_input, nickname, errors)
            if result is not None:
                return await self._async_replace_credentials(entry, result)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_code_schema(nickname), errors=errors
        )

    # -- helpers ---------------------------------------------------------------

    def _unique_id_taken(self, unique_id: str) -> bool:
        return any(
            entry.unique_id == unique_id
            for entry in self._async_current_entries(include_ignore=False)
        )

    async def _async_replace_credentials(
        self, entry: ConfigEntry, result: PairingResult
    ) -> ConfigFlowResult:
        """Swap in a re-paired account, if it belongs to the same installation."""
        # A username-based unique ID doesn't name an installation yet.
        old_id = entry.unique_id
        provisional = old_id is None or old_id.startswith(USERNAME_PREFIX)
        await self.async_set_unique_id(result.installation)
        if not provisional and entry.unique_id != result.installation:
            await _async_discard_account(result)
            return self.async_abort(reason="wrong_installation")
        old_username = entry.data[CONF_USERNAME]
        old_password = entry.data[CONF_PASSWORD]
        response = self.async_update_reload_and_abort(
            entry, unique_id=result.installation, data=result.data
        )
        # The old account is no longer used; delete it once the entry reloaded.
        self.hass.async_create_background_task(
            _async_remove_quietly(old_username, old_password),
            name="esmart remove replaced account",
        )
        return response

    async def _async_try_pair(
        self, user_input: dict[str, Any], nickname: str, errors: dict[str, str]
    ) -> PairingResult | None:
        """Register and pair; fills ``errors`` and returns None on failure."""
        code = str(user_input[CONF_PAIRING_CODE]).strip()
        nickname = str(user_input.get(CONF_NICKNAME) or nickname).strip() or nickname
        try:
            return await self._async_register_and_pair(code, nickname)
        except InvalidCode:
            errors[CONF_PAIRING_CODE] = "invalid_code"
        except PairingTimeout:
            errors["base"] = "pairing_timeout"
        except CannotConnect, TimeoutError, OSError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error while pairing with eSMART")
            errors["base"] = "unknown"
        return None

    async def _async_register_and_pair(self, code: str, nickname: str) -> PairingResult:
        """Create an XMPP account and pair it; returns the credentials."""
        # Validate the code before we touch the network.
        if not code.isdigit():
            raise InvalidCode

        username = generate_username()
        password = generate_password()
        await async_register_account(username, password)

        # Building the client constructs an SSL context (blocking I/O); build it
        # off the event loop.
        client: ESmartClient = await self.hass.async_add_executor_job(
            construct_off_loop,
            partial(
                ESmartClient,
                username=username,
                password=password,
                nickname=nickname,
                on_message=lambda *_: None,
                on_connection=lambda _connected: None,
            ),
        )
        paired = False
        try:
            await client.async_connect()
            rooms = await client.async_pair(code)
            paired = True
        finally:
            if not paired and client.session_ready:
                # Don't leave an unused account behind on the eSMART server.
                try:
                    await client.async_unregister()
                except Exception as err:  # noqa: BLE001 - best effort cleanup
                    _LOGGER.debug("Could not delete the unused account: %s", err)
            await client.async_disconnect()

        # The installation is the wall unit we paired with. If the roster hasn't
        # caught up yet, fall back to the username; the entry is migrated once
        # the device room is joined.
        installation = installation_id(rooms[0]) if rooms else username
        return PairingResult(username, password, nickname, installation)


async def _async_discard_account(result: PairingResult) -> None:
    """Delete an account that was paired but won't be used."""
    await _async_remove_quietly(result.username, result.password)


async def _async_remove_quietly(username: str, password: str) -> None:
    try:
        await async_remove_account(username, password)
    except Exception as err:  # noqa: BLE001 - best effort cleanup
        _LOGGER.debug("Could not delete an unused eSMART account: %s", err)
