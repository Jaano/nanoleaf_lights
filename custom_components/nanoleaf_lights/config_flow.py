"""Config flow for the Enhanced Nanoleaf Light integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector

from .const import (
    CONF_COMPANION_DEVICE_ID,
    CONF_COMPANION_SERIAL_NUMBER,
    CONF_DEVICE_LINK_MODE,
    CONF_EUI64,
    CONF_IP_ADDRESS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PORT,
    CONF_TOKEN,
    DEFAULT_PORT,
    DEVICE_LINK_MODE_AUTO,
    DEVICE_LINK_MODE_COMPANION,
    DEVICE_LINK_MODE_STANDALONE,
    DOMAIN,
    NATIVE_COMPANION_DOMAINS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.components.zeroconf import ZeroconfServiceInfo
    from homeassistant.config_entries import ConfigFlowResult

    from .session import Session


async def _connect_session(*args: Any, **kwargs: Any) -> Session:
    """Import the protocol factory lazily when the flow needs a connection."""
    from .session import connect_session
    return await connect_session(*args, **kwargs)

_LOGGER = logging.getLogger(__name__)

_OPTION_AUTO = "__auto__"
_OPTION_STANDALONE = "__standalone__"


def _device_name(device: dr.DeviceEntry) -> str:
    """Return the best display name for a device entry."""
    return device.name_by_user or device.name or device.model or device.id


def _config_entry_domain(hass: HomeAssistant, device: dr.DeviceEntry) -> str | None:
    """Return the owning config entry domain for a device."""
    entry = hass.config_entries.async_get_entry(device.config_entry_id)
    return entry.domain if entry is not None else None


def _enhanced_serial(hass: HomeAssistant, config_entry: ConfigEntry) -> str | None:
    """Return the latest known serial number for this Enhanced Nanoleaf entry."""
    coordinator = getattr(config_entry, "runtime_data", None)
    data = getattr(coordinator, "data", None) or {}
    device = data.get("device") or {}
    serial = device.get("serial_number")
    if isinstance(serial, str) and serial:
        return serial

    serial = config_entry.options.get(CONF_COMPANION_SERIAL_NUMBER)
    if isinstance(serial, str) and serial:
        return serial

    own_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, config_entry.unique_id or config_entry.entry_id),
        config_entry.entry_id,
    )
    serial = own_device.serial_number if own_device is not None else None
    return serial if isinstance(serial, str) and serial else None


def _native_nanoleaf_candidates(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    serial: str | None,
) -> list[dr.DeviceEntry]:
    """Return Matter/HomeKit Nanoleaf devices matching the Enhanced device serial."""
    if not serial:
        return []

    device_registry = dr.async_get(hass)
    candidates: list[dr.DeviceEntry] = []
    for device in device_registry.devices.values():
        if device.config_entry_id == config_entry.entry_id:
            continue
        if (device.manufacturer or "").casefold() != "nanoleaf":
            continue
        if device.serial_number != serial:
            continue
        if _config_entry_domain(hass, device) not in NATIVE_COMPANION_DOMAINS:
            continue
        candidates.append(device)
    return sorted(candidates, key=_device_name)


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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Create the options flow."""
        return NanoleafLtpduOptionsFlow()

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
            # Verify reachability with the new protocol, then legacy CoAP.
            try:
                session = await _connect_session(host, port, model=model, timeout=10.0)
                await session.close()
            except Exception as exc:
                _LOGGER.debug("user: connect failed: %s", exc)
                errors["base"] = "cannot_connect"
            else:
                _LOGGER.debug("user: connection succeeded, proceeding to pair")
                self._discovery = {
                    CONF_IP_ADDRESS: host,
                    CONF_PORT: port,
                    CONF_MODEL: model,
                    CONF_NAME: user_input.get(CONF_NAME) or None,
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
                    vol.Optional(CONF_NAME, default=""): str,
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

            _LOGGER.debug("pair: connecting host=%s port=%d model=%s", host, port, model)
            try:
                session = await _connect_session(
                    host, port, model=model, name=d.get(CONF_NAME), pin=pin, timeout=10.0
                )
                try:
                    token = session.token
                    _LOGGER.debug("pair: PIN accepted")
                    # pair() has already authenticated; proceed directly to queries.
                    eui64 = d.get(CONF_EUI64)
                    if not eui64:
                        try:
                            device_info = await session.query_device_info(timeout=10.0)
                            eui64 = device_info.get("eui64")
                            _LOGGER.debug("pair: resolved eui64=%s from device_info", eui64)
                        except Exception as exc:
                            _LOGGER.debug("pair: could not fetch device_info for eui64: %s", exc)
                finally:
                    await session.close()
            except RuntimeError as exc:
                _LOGGER.debug("pair: RuntimeError: %s", exc)
                if any(word in str(exc).lower() for word in ("pin", "pairing code", "unexpected")):
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
                    title=d.get(CONF_NAME) or f"Nanoleaf @ {host}",
                    data={
                        CONF_IP_ADDRESS: host,
                        CONF_PORT: port,
                        CONF_MODEL: model,
                        CONF_TOKEN: token.hex(),
                        CONF_NAME: d.get(CONF_NAME) or f"Nanoleaf @ {host}",
                    },
                )

        d = self._discovery
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Required("pairing_code"): str}),
            errors=errors,
            description_placeholders={
                "name": d.get(CONF_NAME) or "Nanoleaf light",
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
                "reauth_confirm: connecting host=%s port=%d model=%s", host, port, model
            )
            try:
                session = await _connect_session(
                    host, port, model=model, name=data.get(CONF_NAME), pin=pin, timeout=10.0
                )
                try:
                    token = session.token
                    _LOGGER.debug("reauth_confirm: pairing code accepted")
                finally:
                    await session.close()
            except RuntimeError as exc:
                _LOGGER.debug("reauth_confirm: RuntimeError: %s", exc)
                errors["base"] = (
                    "invalid_pin" if any(
                        word in str(exc).lower() for word in ("pin", "pairing code", "unexpected")
                    ) else "cannot_connect"
                )
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

    # ------------------------------------------------------------------
    # Reconfigure flow
    # ------------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point for the Reconfigure option in the device page."""
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update IP address, port, model and optionally re-pair with a new PIN."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = _strip_zone_id(user_input[CONF_IP_ADDRESS])
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            model = user_input.get(CONF_MODEL) or None
            pin = (user_input.get("pairing_code") or "").strip()
            name = user_input.get(CONF_NAME, reconfigure_entry.data.get(CONF_NAME)) or None

            _LOGGER.debug(
                "reconfigure: host=%s port=%d model=%s re-pair=%s",
                host, port, model, bool(pin),
            )
            try:
                if pin:
                    _LOGGER.debug("reconfigure: re-pairing with new PIN")
                else:
                    _LOGGER.debug("reconfigure: verifying existing token at new address")
                session = await _connect_session(
                    host, port, model=model, name=name,
                    pin=pin or None,
                    token=None if pin else bytes.fromhex(reconfigure_entry.data[CONF_TOKEN]),
                    timeout=10.0,
                )
                try:
                    token = session.token.hex()
                finally:
                    await session.close()
            except RuntimeError as exc:
                _LOGGER.debug("reconfigure: RuntimeError: %s", exc)
                if pin and any(
                    word in str(exc).lower() for word in ("pin", "pairing code", "unexpected")
                ):
                    errors["base"] = "invalid_pin"
                else:
                    errors["base"] = "cannot_connect"
            except Exception as exc:
                _LOGGER.exception("reconfigure: unexpected error: %s", exc)
                errors["base"] = "unknown"
            else:
                _LOGGER.debug("reconfigure: updating entry and reloading")
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data={
                        **reconfigure_entry.data,
                        CONF_IP_ADDRESS: host,
                        CONF_PORT: port,
                        CONF_MODEL: model,
                        CONF_TOKEN: token,
                        **({CONF_NAME: name} if CONF_NAME in user_input else {}),
                    },
                    reason="reconfigure_successful",
                )

        data = reconfigure_entry.data
        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_IP_ADDRESS, default=data.get(CONF_IP_ADDRESS, "")): str,
                    vol.Optional(CONF_PORT, default=data.get(CONF_PORT, DEFAULT_PORT)): int,
                    vol.Optional(CONF_MODEL, default=data.get(CONF_MODEL) or ""): str,
                    vol.Optional(CONF_NAME, default=data.get(CONF_NAME) or ""): str,
                    vol.Optional("pairing_code", default=""): str,
                }
            ),
            errors=errors,
            description_placeholders={"name": data.get(CONF_NAME, "Nanoleaf light")},
        )


class NanoleafLtpduOptionsFlow(OptionsFlowWithReload):
    """Handle options for Enhanced Nanoleaf Light."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Manage device-page linking options."""
        serial = _enhanced_serial(self.hass, self.config_entry)
        current_target = self.config_entry.options.get(CONF_COMPANION_DEVICE_ID)
        candidates = self._selectable_candidates(
            _native_nanoleaf_candidates(self.hass, self.config_entry, serial),
            current_target,
        )
        candidate_map = {device.id: device for device in candidates}

        if user_input is not None:
            selected = user_input[CONF_COMPANION_DEVICE_ID]
            if selected == _OPTION_AUTO:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_DEVICE_LINK_MODE: DEVICE_LINK_MODE_AUTO,
                    },
                )

            if selected == _OPTION_STANDALONE:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_DEVICE_LINK_MODE: DEVICE_LINK_MODE_STANDALONE,
                    },
                )

            target = candidate_map.get(selected)
            if target is None:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(candidates, current_target),
                    errors={"base": "invalid_companion_device"},
                    description_placeholders={
                        "serial": serial or "unknown",
                    },
                )

            return self.async_create_entry(
                title="",
                data={
                    CONF_DEVICE_LINK_MODE: DEVICE_LINK_MODE_COMPANION,
                    CONF_COMPANION_DEVICE_ID: target.id,
                    CONF_COMPANION_SERIAL_NUMBER: target.serial_number or "",
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(candidates, current_target),
            description_placeholders={
                "serial": serial or "unknown",
            },
        )

    def _schema(
        self,
        candidates: list[dr.DeviceEntry],
        current_target: Any,
    ) -> vol.Schema:
        """Return the options form schema."""
        options: list[selector.SelectOptionDict] = [
            {
                "value": _OPTION_AUTO,
                "label": "Automatic serial-number match",
            },
            {
                "value": _OPTION_STANDALONE,
                "label": "Standalone Enhanced Nanoleaf device",
            }
        ]
        for device in candidates:
            domain = _config_entry_domain(self.hass, device) or "unknown"
            options.append(
                {
                    "value": device.id,
                    "label": f"{_device_name(device)} ({domain})",
                }
            )

        current_mode = self.config_entry.options.get(CONF_DEVICE_LINK_MODE)
        candidate_ids = {device.id for device in candidates}
        if current_mode == DEVICE_LINK_MODE_STANDALONE:
            default = _OPTION_STANDALONE
        elif current_target in candidate_ids:
            default = current_target
        else:
            default = _OPTION_AUTO
        return vol.Schema(
            {
                vol.Required(
                    CONF_COMPANION_DEVICE_ID,
                    default=default,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )

    @staticmethod
    def _selectable_candidates(
        candidates: list[dr.DeviceEntry],
        current_target: Any,
    ) -> list[dr.DeviceEntry]:
        """Return verified candidate devices that can be selected safely."""
        if len(candidates) <= 1:
            return candidates
        if isinstance(current_target, str):
            return [device for device in candidates if device.id == current_target]
        return []
