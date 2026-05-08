"""Config flow for the Enhanced Nanoleaf Light integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow

from .const import (
    CONF_EUI64,
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_TOKEN,
    DEFAULT_PORT,
    DOMAIN,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.components.zeroconf import ZeroconfServiceInfo
    from homeassistant.config_entries import ConfigFlowResult

    from .nl_ltpdu import LtpduSession


def _ltpdu_session() -> type[LtpduSession]:
    """Lazy import of LtpduSession to avoid blocking the event loop on module load."""
    from .nl_ltpdu import LtpduSession
    return LtpduSession

_LOGGER = logging.getLogger(__name__)


def _strip_zone_id(host: str) -> str:
    """Remove IPv6 zone ID suffix (e.g. '%eth0') from a host string."""
    if "%" in host:
        return host.split("%", 1)[0]
    return host


class NanoleafLtpduConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nanoleaf light."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Zeroconf discovery
    # ------------------------------------------------------------------

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device discovered via mDNS."""
        host = _strip_zone_id(discovery_info.host)
        port = discovery_info.port or DEFAULT_PORT
        properties = discovery_info.properties or {}
        model = properties.get("md")
        eui64 = properties.get("eui64")

        # Derive a friendly name from the service instance name.
        raw_name = discovery_info.name or ""
        # Strip the service-type suffix (e.g. "._ltpdu._udp.local.")
        for sep in ("._ltpdu._udp.local.", "._ltpdu._udp."):
            if raw_name.endswith(sep):
                raw_name = raw_name[: -len(sep)]
                break
        friendly_name = raw_name or "Nanoleaf light"

        _LOGGER.debug(
            "zeroconf: discovered host=%s port=%d model=%s eui64=%s name=%r props=%s",
            host, port, model, eui64, friendly_name, properties,
        )

        if eui64:
            await self.async_set_unique_id(eui64)
            self._abort_if_unique_id_configured(
                updates={CONF_IP_ADDRESS: host, CONF_PORT: port}
            )
        else:
            _LOGGER.debug("zeroconf: no eui64 in mDNS properties, matching by address")
            self._async_abort_entries_match({CONF_IP_ADDRESS: host, CONF_PORT: port})

        self._discovery = {
            CONF_IP_ADDRESS: host,
            CONF_PORT: port,
            CONF_MODEL: model,
            CONF_NAME: friendly_name,
            CONF_EUI64: eui64,
        }
        self.context["title_placeholders"] = {"name": friendly_name}
        _LOGGER.debug("zeroconf: proceeding to pair step for %r", friendly_name)
        return await self.async_step_pair()

    # ------------------------------------------------------------------
    # Manual add
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual IP entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = _strip_zone_id(user_input[CONF_IP_ADDRESS])
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            model = user_input.get(CONF_MODEL) or None

            _LOGGER.debug(
                "user: verifying reachability host=%s port=%d model=%s", host, port, model
            )
            # Verify reachability by attempting KEX.
            try:
                session = await _ltpdu_session().connect(host, port, model=model, timeout=10.0)
                await session.close()
            except Exception as exc:
                _LOGGER.debug("user: connect failed: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                _LOGGER.debug("user: KEX succeeded, proceeding to pair")
                self._discovery = {
                    CONF_IP_ADDRESS: host,
                    CONF_PORT: port,
                    CONF_MODEL: model,
                    CONF_NAME: user_input.get(CONF_NAME, f"Nanoleaf @ {host}"),
                    CONF_EUI64: None,
                }
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_IP_ADDRESS): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(CONF_MODEL, default=""): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect PIN and pair with the device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            pin = user_input["pairing_code"]
            d = self._discovery
            host = d[CONF_IP_ADDRESS]
            port = d[CONF_PORT]
            model = d.get(CONF_MODEL)

            _LOGGER.debug("pair: attempting KEX host=%s port=%d model=%s", host, port, model)
            try:
                session = await _ltpdu_session().connect(host, port, model=model, timeout=10.0)
                _LOGGER.debug("pair: KEX succeeded, sending PIN")
                token = await session.pair(pin)
                _LOGGER.debug("pair: PIN accepted, token=%s", token.hex())
                # Do NOT call auth() here — pair() already authenticates the session;
                # calling auth() immediately after gets status 0x02 (already authenticated).

                # Try to resolve eui64 from the device if not discovered via mDNS.
                eui64 = d.get(CONF_EUI64)
                if not eui64:
                    try:
                        device_info = await session.query_device_info(timeout=10.0)
                        eui64 = device_info.get("eui64")
                        _LOGGER.debug("pair: resolved eui64=%s from device_info", eui64)
                    except Exception as exc:
                        _LOGGER.debug("pair: could not fetch device_info for eui64: %s", exc)

                await session.close()
            except RuntimeError as exc:
                _LOGGER.debug("pair: RuntimeError: %s", exc)
                if "pin" in str(exc).lower() or "unexpected" in str(exc).lower():
                    errors["base"] = "invalid_pin"
                else:
                    errors["base"] = "cannot_connect"
            except Exception as exc:
                _LOGGER.exception("pair: unexpected error: %s", exc)
                errors["base"] = "unknown"
            else:
                # Set unique id from eui64 if available.
                # raise_on_progress=False: don't abort if a zeroconf-triggered flow
                # for this device is still pending — the user flow takes precedence.
                if eui64:
                    _LOGGER.debug("pair: setting unique_id=%s", eui64)
                    await self.async_set_unique_id(eui64, raise_on_progress=False)
                    self._abort_if_unique_id_configured()

                _LOGGER.debug("pair: creating config entry title=%r", d.get(CONF_NAME))
                return self.async_create_entry(
                    title=d.get(CONF_NAME, "Nanoleaf light"),
                    data={
                        CONF_IP_ADDRESS: host,
                        CONF_PORT: port,
                        CONF_MODEL: model,
                        CONF_TOKEN: token.hex(),
                        CONF_NAME: d.get(CONF_NAME, "Nanoleaf light"),
                    },
                )

        d = self._discovery
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Required("pairing_code"): str}),
            errors=errors,
            description_placeholders={
                "name": d.get(CONF_NAME, ""),
                "model": d.get(CONF_MODEL) or "unknown",
                "host": d.get(CONF_IP_ADDRESS, ""),
            },
        )

    # ------------------------------------------------------------------
    # Re-auth flow
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the stored token is rejected."""
        _LOGGER.debug("reauth: initiated for entry %s", self.context.get("entry_id"))
        return await self.async_step_reauth_confirm()

    def _reauth_entry(self) -> ConfigEntry:
        """Return the config entry being re-authenticated."""
        return self._get_reauth_entry()  # type: ignore[return-value]

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-pair with a new PIN and update the stored token."""
        errors: dict[str, str] = {}
        reauth_entry = self._reauth_entry()

        if user_input is not None:
            pin = user_input["pairing_code"]
            data = reauth_entry.data
            host = data[CONF_IP_ADDRESS]
            port = data[CONF_PORT]
            model = data.get(CONF_MODEL)

            _LOGGER.debug(
                "reauth_confirm: attempting KEX host=%s port=%d model=%s", host, port, model
            )
            try:
                session = await _ltpdu_session().connect(host, port, model=model, timeout=10.0)
                _LOGGER.debug("reauth_confirm: KEX succeeded, sending pairing code")
                token = await session.pair(pin)
                _LOGGER.debug("reauth_confirm: pairing code accepted, new token=%s", token.hex())
                # Do NOT call auth() here — same reason as async_step_pair.
                await session.close()
            except RuntimeError as exc:
                _LOGGER.debug("reauth_confirm: RuntimeError: %s", exc)
                errors["base"] = "invalid_pin"
            except Exception as exc:
                _LOGGER.exception("reauth_confirm: unexpected error: %s", exc)
                errors["base"] = "unknown"
            else:
                _LOGGER.debug("reauth_confirm: updating stored token and reloading entry")
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, CONF_TOKEN: token.hex()},
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("pairing_code"): str}),
            errors=errors,
            description_placeholders={"name": reauth_entry.data.get(CONF_NAME, "Nanoleaf light")},
        )
