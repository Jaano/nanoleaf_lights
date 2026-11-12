"""Config flow tests: every step, including recovery from each error."""

# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ANN202, ANN204

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

EUI64 = "0011223344556677"


class FakeSession:
    def __init__(self, token=b"\x01\x02", eui64=EUI64, info_error=None):
        self.token = token
        self.closed = False
        self._eui64 = eui64
        self._info_error = info_error

    async def query_device_info(self, timeout):  # noqa: ASYNC109 -- session API
        if self._info_error:
            raise self._info_error
        return {"eui64": self._eui64}

    async def close(self):
        self.closed = True


class Connector:
    """Replacement for config_flow._connect_session: queued sessions or errors."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def __call__(self, host, port, **options):
        self.calls.append((host, port, options))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def entry(unique_id=None, **data):
    return SimpleNamespace(
        unique_id=unique_id,
        data={
            "ip_address": "fd00::1",
            "port": 12566,
            "model": "NL67",
            "token": "aa",
            "name": "Essentials-E20",
            **data,
        },
    )


@pytest.fixture
def flow_module(ha_modules):
    return ha_modules.config_flow


@pytest.fixture
def make_flow(flow_module, monkeypatch):
    def make(*outcomes, existing=()):
        connector = Connector(*outcomes)
        monkeypatch.setattr(flow_module, "_connect_session", connector)
        flow_module.NanoleafLtpduConfigFlow.existing_entries = list(existing)
        return flow_module.NanoleafLtpduConfigFlow(), connector

    return make


def discovery(host="fd00::1%eth0", port=12566, name="Essentials-E20._ltpdu._udp.local.", **props):
    properties = {"md": "NL67", **props}
    return SimpleNamespace(host=host, port=port, name=name, properties=properties)


# -- Helpers ------------------------------------------------------------------------------


def test_zone_id_is_stripped_from_ipv6_hosts(flow_module):
    assert flow_module._strip_zone_id("fe80::1%eth0") == "fe80::1"
    assert flow_module._strip_zone_id("fd00::1") == "fd00::1"


async def test_connect_wrapper_uses_the_session_factory_lazily(flow_module, ha_modules):
    session_module = sys.modules[flow_module.__name__.rsplit(".", 1)[0] + ".session"]
    factory = AsyncMock(return_value="session")
    original = session_module.connect_session
    session_module.connect_session = factory
    try:
        assert await flow_module._connect_session("fd00::1", 1, model="NL67") == "session"
    finally:
        session_module.connect_session = original
    factory.assert_awaited_once_with("fd00::1", 1, model="NL67")


# -- Zeroconf discovery -------------------------------------------------------------------


async def test_discovery_with_eui64_asks_for_the_pairing_code(make_flow):
    flow, _ = make_flow()
    result = await flow.async_step_zeroconf(discovery(eui64=EUI64))
    assert result["step_id"] == "pair"
    assert flow.unique_id == EUI64
    assert flow.context["title_placeholders"] == {"name": "Essentials-E20"}
    assert flow._discovery["ip_address"] == "fd00::1"  # zone ID removed
    assert result["description_placeholders"] == {
        "name": "Essentials-E20", "model": "NL67", "host": "fd00::1",
    }


async def test_rediscovery_of_a_configured_device_updates_its_address(make_flow, ha_modules):
    existing = entry(unique_id=EUI64, ip_address="fd00::9", port=5653)
    flow, _ = make_flow(existing=[existing])
    with pytest.raises(ha_modules.AbortFlow, match="already_configured"):
        await flow.async_step_zeroconf(discovery(eui64=EUI64))
    assert existing.data["ip_address"] == "fd00::1"
    assert existing.data["port"] == 12566


async def test_discovery_without_eui64_aborts_for_a_configured_address(make_flow, ha_modules):
    flow, _ = make_flow(existing=[entry()])
    with pytest.raises(ha_modules.AbortFlow, match="already_configured"):
        await flow.async_step_zeroconf(discovery())


async def test_discovery_with_eui64_aborts_for_a_manual_entry_at_that_address(
    make_flow, ha_modules
):
    flow, _ = make_flow(existing=[entry(unique_id=None)])
    with pytest.raises(ha_modules.AbortFlow, match="already_configured"):
        await flow.async_step_zeroconf(discovery(eui64=EUI64))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Essentials-E20._ltpdu._udp.local.", "Essentials-E20"),
        ("Essentials-E20._ltpdu._udp.", "Essentials-E20"),
        ("Plain name", "Plain name"),
        ("", "Nanoleaf light"),
        (None, "Nanoleaf light"),
    ],
)
async def test_discovery_derives_a_friendly_name(make_flow, name, expected):
    flow, _ = make_flow()
    await flow.async_step_zeroconf(discovery(name=name))
    assert flow._discovery["name"] == expected


async def test_discovery_without_port_or_properties_uses_defaults(make_flow, flow_module):
    flow, _ = make_flow()
    info = SimpleNamespace(host="fd00::2", port=None, name="Bulb", properties=None)
    await flow.async_step_zeroconf(info)
    assert flow._discovery["port"] == flow_module.DEFAULT_PORT
    assert flow._discovery["model"] is None


# -- Manual add ---------------------------------------------------------------------------


async def test_user_step_shows_the_form(make_flow):
    flow, _ = make_flow()
    result = await flow.async_step_user()
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_step_tests_the_connection_then_asks_for_the_code(make_flow):
    session = FakeSession()
    flow, connector = make_flow(session)
    result = await flow.async_step_user(
        {"ip_address": "fd00::1%wpan0", "port": 12566, "model": "NL67", "name": "Bulb"}
    )
    assert result["step_id"] == "pair"
    assert session.closed
    assert connector.calls == [("fd00::1", 12566, {"model": "NL67", "timeout": 10.0})]
    assert flow._discovery == {
        "ip_address": "fd00::1", "port": 12566, "model": "NL67", "name": "Bulb", "eui64": None,
    }


async def test_user_step_recovers_after_a_failed_connection(make_flow):
    flow, _ = make_flow(OSError("unreachable"), FakeSession())
    user_input = {"ip_address": "fd00::1", "port": 12566, "model": "", "name": ""}
    result = await flow.async_step_user(user_input)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}
    result = await flow.async_step_user(user_input)
    assert result["step_id"] == "pair"
    assert flow._discovery["model"] is None
    assert flow._discovery["name"] is None


async def test_user_step_defaults_the_port(make_flow, flow_module):
    flow, connector = make_flow(FakeSession())
    await flow.async_step_user({"ip_address": "fd00::1"})
    assert connector.calls[0][1] == flow_module.DEFAULT_PORT


async def test_user_step_refuses_an_already_configured_address_before_connecting(
    make_flow, ha_modules
):
    flow, connector = make_flow(existing=[entry()])
    with pytest.raises(ha_modules.AbortFlow, match="already_configured"):
        await flow.async_step_user({"ip_address": "fd00::1", "port": 12566})
    assert connector.calls == []


# -- Pairing ------------------------------------------------------------------------------


def paired_flow(flow, eui64=EUI64, name="Essentials-E20"):
    flow._discovery = {
        "ip_address": "fd00::1", "port": 12566, "model": "NL67", "name": name, "eui64": eui64,
    }
    return flow


async def test_pair_creates_the_entry_with_the_device_unique_id(make_flow):
    session = FakeSession(token=b"\xab\xcd")
    flow, connector = make_flow(session)
    result = await paired_flow(flow).async_step_pair({"pairing_code": "33945322503"})
    assert result["type"] == "create_entry"
    assert result["title"] == "Essentials-E20"
    assert result["data"] == {
        "ip_address": "fd00::1", "port": 12566, "model": "NL67",
        "token": "abcd", "name": "Essentials-E20",
    }
    assert flow.unique_id == EUI64
    assert flow.context["raise_on_progress"] is False
    assert connector.calls[0][2]["pin"] == "33945322503"
    assert session.closed


async def test_pair_reads_the_eui64_when_discovery_had_none(make_flow):
    flow, _ = make_flow(FakeSession(eui64="8899aabbccddeeff"))
    await paired_flow(flow, eui64=None).async_step_pair({"pairing_code": "12345678"})
    assert flow.unique_id == "8899aabbccddeeff"


async def test_pair_without_a_readable_eui64_still_creates_the_entry(make_flow):
    session = FakeSession(info_error=RuntimeError("di rejected"))
    flow, _ = make_flow(session)
    result = await paired_flow(flow, eui64=None, name=None).async_step_pair(
        {"pairing_code": "12345678"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Nanoleaf @ fd00::1"
    assert result["data"]["name"] == "Nanoleaf @ fd00::1"
    assert flow.unique_id is None
    assert session.closed


async def test_pair_aborts_when_the_paired_device_is_already_configured(make_flow, ha_modules):
    flow, _ = make_flow(FakeSession(), existing=[entry(unique_id=EUI64, ip_address="fd00::9")])
    with pytest.raises(ha_modules.AbortFlow, match="already_configured"):
        await paired_flow(flow).async_step_pair({"pairing_code": "12345678"})


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RuntimeError("Pair rejected by device: bad PIN"), "invalid_pin"),
        (RuntimeError("Wrong pairing code"), "invalid_pin"),
        (RuntimeError("unexpected response"), "invalid_pin"),
        (RuntimeError("timed out"), "cannot_connect"),
        (ValueError("boom"), "unknown"),
    ],
)
async def test_pair_reports_errors_and_recovers(make_flow, error, code):
    flow, _ = make_flow(error, FakeSession())
    paired_flow(flow)
    result = await flow.async_step_pair({"pairing_code": "12345678"})
    assert result["step_id"] == "pair"
    assert result["errors"] == {"base": code}
    result = await flow.async_step_pair({"pairing_code": "12345678"})
    assert result["type"] == "create_entry"


async def test_pair_form_placeholders_have_defaults(make_flow):
    flow, _ = make_flow()
    flow._discovery = {}
    result = await flow.async_step_pair()
    assert result["description_placeholders"] == {
        "name": "Nanoleaf light", "model": "unknown", "host": "",
    }


# -- Reauthentication ---------------------------------------------------------------------


def reauth_flow(flow, existing):
    flow._get_reauth_entry = lambda: existing
    return flow


async def test_reauth_asks_for_the_pairing_code(make_flow):
    flow, _ = make_flow()
    existing = entry()
    result = await reauth_flow(flow, existing).async_step_reauth(existing.data)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"] == {"name": "Essentials-E20"}


async def test_reauth_stores_the_new_token_and_reloads(make_flow):
    session = FakeSession(token=b"\x10\x20")
    flow, connector = make_flow(session)
    existing = entry()
    result = await reauth_flow(flow, existing).async_step_reauth_confirm(
        {"pairing_code": "12345678"}
    )
    assert result["reason"] == "reauth_successful"
    assert result["data"] == {**existing.data, "token": "1020"}
    assert connector.calls[0][2]["pin"] == "12345678"
    assert session.closed


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RuntimeError("bad PIN"), "invalid_pin"),
        (RuntimeError("timed out"), "cannot_connect"),
        (ValueError("boom"), "unknown"),
    ],
)
async def test_reauth_reports_errors_and_recovers(make_flow, error, code):
    flow, _ = make_flow(error, FakeSession())
    reauth_flow(flow, entry())
    result = await flow.async_step_reauth_confirm({"pairing_code": "12345678"})
    assert result["errors"] == {"base": code}
    result = await flow.async_step_reauth_confirm({"pairing_code": "12345678"})
    assert result["reason"] == "reauth_successful"


# -- Reconfiguration ----------------------------------------------------------------------


def reconfigure_flow(flow, existing):
    flow._get_reconfigure_entry = lambda: existing
    return flow


async def test_reconfigure_shows_the_current_settings(make_flow):
    flow, _ = make_flow()
    result = await reconfigure_flow(flow, entry()).async_step_reconfigure()
    assert result["step_id"] == "reconfigure_confirm"
    assert result["description_placeholders"] == {"name": "Essentials-E20"}


async def test_reconfigure_with_a_code_re_pairs(make_flow):
    session = FakeSession(token=b"\x99")
    flow, connector = make_flow(session)
    existing = entry()
    result = await reconfigure_flow(flow, existing).async_step_reconfigure_confirm(
        {"ip_address": "fd00::7", "port": 5653, "model": "", "pairing_code": " 12345678 "}
    )
    assert result["reason"] == "reconfigure_successful"
    assert result["data"]["token"] == "99"
    assert result["data"]["model"] is None
    assert result["data"]["name"] == "Essentials-E20"  # name not submitted: unchanged
    options = connector.calls[0][2]
    assert (options["pin"], options["token"]) == ("12345678", None)


@pytest.mark.parametrize(
    ("pin", "error", "code"),
    [
        ("12345678", RuntimeError("bad PIN"), "invalid_pin"),
        ("", RuntimeError("bad PIN"), "cannot_connect"),
        ("", RuntimeError("timed out"), "cannot_connect"),
        ("", ValueError("boom"), "unknown"),
    ],
)
async def test_reconfigure_reports_errors_and_recovers(make_flow, pin, error, code):
    flow, _ = make_flow(error, FakeSession())
    reconfigure_flow(flow, entry())
    user_input = {"ip_address": "fd00::7", "port": 12566, "pairing_code": pin}
    result = await flow.async_step_reconfigure_confirm(user_input)
    assert result["errors"] == {"base": code}
    result = await flow.async_step_reconfigure_confirm(user_input)
    assert result["reason"] == "reconfigure_successful"
