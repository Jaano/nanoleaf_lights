"""Offline transport selection and opaque scene-handle compatibility."""

# ruff: noqa: ANN001, ANN201, ANN202

import asyncio

import pytest


@pytest.mark.asyncio
async def test_modern_authentication_wins_without_legacy_attempt(factory):
    session = await factory.module.connect_session(
        "::1", 12566, model="NL67", name="Bulb-E20", token=b"token", timeout=3
    )
    assert isinstance(session, factory.modern)
    assert session.token == b"token"
    assert factory.events == [
        ("v3", "connect", "::1", 12566, {"model": "NL67", "name": "Bulb-E20", "timeout": 3}),
        ("v3", "auth", b"token", 3),
    ]


@pytest.mark.asyncio
async def test_failed_modern_auth_closes_before_legacy_authentication(factory):
    factory.modern.auth_error = RuntimeError("new handshake unavailable")
    session = await factory.module.connect_session("::1", 5653, token=b"old-token")
    assert isinstance(session, factory.legacy)
    assert session.token == b"old-token"
    assert factory.modern.instances[0].closed
    assert [event[:2] for event in factory.events] == [
        ("v3", "connect"),
        ("v3", "auth"),
        ("v3", "close"),
        ("legacy", "connect"),
        ("legacy", "auth"),
    ]


@pytest.mark.asyncio
async def test_pairing_uses_legacy_after_modern_failure_and_exposes_token(factory):
    factory.modern.pair_error = RuntimeError("pairing unsupported")
    session = await factory.module.connect_session("::1", 5653, pin="1234-567-8901")
    assert isinstance(session, factory.legacy)
    assert session.token == b"paired-token"
    assert factory.modern.instances[0].closed
    assert [event[1] for event in factory.events] == ["connect", "pair", "close", "connect", "pair"]


@pytest.mark.asyncio
async def test_public_probe_falls_back_without_authentication(factory):
    factory.modern.connect_error = RuntimeError("not v3")
    session = await factory.module.connect_session("::1", 5653)
    assert isinstance(session, factory.legacy)
    assert [event[:2] for event in factory.events] == [("v3", "connect"), ("legacy", "connect")]


@pytest.mark.asyncio
async def test_both_failures_are_reported_and_both_sessions_close(factory):
    factory.modern.auth_error = RuntimeError("v3 failure")
    factory.legacy.auth_error = RuntimeError("legacy failure")
    with pytest.raises(RuntimeError, match="LTPDUv3: v3 failure; legacy CoAP: legacy failure"):
        await factory.module.connect_session("::1", 12566, token=b"token")
    assert factory.modern.instances[0].closed
    assert factory.legacy.instances[0].closed


@pytest.mark.asyncio
async def test_token_rejection_survives_transport_failure_for_ha_reauth(factory):
    factory.modern.auth_error = factory.rejection("expired token")
    factory.legacy.connect_error = RuntimeError("KEX timed out")
    with pytest.raises(factory.rejection, match=r"expired token.*KEX timed out"):
        await factory.module.connect_session("::1", 12566, token=b"token")
    assert factory.modern.instances[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["auth", "pair"])
async def test_cancellation_closes_session_and_never_falls_back(factory, operation):
    setattr(factory.modern, f"{operation}_error", asyncio.CancelledError())
    arguments = {"token": b"token"} if operation == "auth" else {"pin": "12345678901"}
    with pytest.raises(asyncio.CancelledError):
        await factory.module.connect_session("::1", 12566, **arguments)
    assert factory.modern.instances[0].closed
    assert not factory.legacy.instances


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_prevent_fallback(factory):
    factory.modern.auth_error = RuntimeError("auth failed")
    factory.modern.close_error = RuntimeError("already closed")
    session = await factory.module.connect_session("::1", 12566, token=b"token")
    assert isinstance(session, factory.legacy)


@pytest.mark.asyncio
async def test_ambiguous_credentials_rejected_before_network_work(factory):
    with pytest.raises(ValueError):
        await factory.module.connect_session("::1", 12566, token=b"token", pin="12345678901")
    assert not factory.events


def test_scene_handle_conversion_preserves_v3_leading_zero_bytes(factory):
    handle = bytes.fromhex("000000fb")
    assert factory.module.scene_handle_bytes(handle) == handle
    assert factory.module.scene_handle_bytes(251) == b"\xfb"
    assert factory.module.scene_fallback_name(handle) == "Scene 0x000000fb"








@pytest.mark.asyncio
async def test_real_task_cancellation_during_auth_closes_without_fallback(factory, monkeypatch):
    started = asyncio.Event()

    async def pending_auth(self, token, timeout):  # noqa: ASYNC109 -- backend API
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(factory.modern, "auth", pending_auth)
    task = asyncio.create_task(factory.module.connect_session("::1", 12566, token=b"token"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert factory.modern.instances[0].closed
    assert not factory.legacy.instances
