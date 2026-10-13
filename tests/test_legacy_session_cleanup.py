"""Legacy KEX remains compatible and releases contexts before fallback."""

# Test fixtures emulate aiocoap's context and response objects.
# ruff: noqa: ANN001, ANN201

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import nl_ltpdu


class Context:
    def __init__(self, payload, pending=False) -> None:
        self.shutdown = AsyncMock()
        self.future = asyncio.get_running_loop().create_future()
        if not pending:
            self.future.set_result(
                SimpleNamespace(code=SimpleNamespace(is_successful=lambda: True), payload=payload)
            )

    def request(self, message):
        self.message = message
        return SimpleNamespace(response=self.future)


@pytest.mark.parametrize("model", ["NL67", "NL45"])
async def test_legacy_kex_crypto_unchanged(monkeypatch, model):
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    context = Context(nl_ltpdu._create_tlv(0x0101, public))
    monkeypatch.setattr(
        nl_ltpdu.aiocoap.Context, "create_client_context", AsyncMock(return_value=context)
    )
    session = await nl_ltpdu.LtpduSession.kex("::1", 5683, model=model)
    try:
        _, _, client_public = nl_ltpdu._decode_tlv(context.message.payload)
        shared = private.exchange(nl_ltpdu.X25519PublicKey.from_public_bytes(client_public))
        if model == "NL45":
            labels = (b"AES-NL-OPENAPI-KEY", b"AES-NL-OPENAPI-IV")
        else:
            labels = tuple(
                bytes.fromhex(value)
                for value in (
                    "bca9e39738e2611c25d243c985d3c592ccbee330e077b89d7978f5fc785cb5e8",
                    "41e2e9eb5e5fa56e800e346d8c2b600742eb49839626faecdc37fb94a3a3c202",
                )
            )
        derived = []
        for label in labels:
            hash_value = hashes.Hash(hashes.SHA1())
            hash_value.update(label + shared)
            derived.append(hash_value.finalize()[:16])
        expected = Cipher(algorithms.AES(derived[0]), modes.CTR(derived[1])).encryptor()
        assert session.encrypt(b"legacy payload") == expected.update(b"legacy payload")
        context.shutdown.assert_not_awaited()
    finally:
        await session.close()
    context.shutdown.assert_awaited_once()


async def test_malformed_kex_closes_context(monkeypatch):
    context = Context(b"\x01")
    monkeypatch.setattr(
        nl_ltpdu.aiocoap.Context, "create_client_context", AsyncMock(return_value=context)
    )
    with pytest.raises(ValueError, match="TLV buffer too short"):
        await nl_ltpdu.LtpduSession.kex("::1", 5683)
    context.shutdown.assert_awaited_once()


async def test_cancelled_kex_closes_context(monkeypatch):
    context = Context(b"", pending=True)
    monkeypatch.setattr(
        nl_ltpdu.aiocoap.Context, "create_client_context", AsyncMock(return_value=context)
    )
    task = asyncio.create_task(nl_ltpdu.LtpduSession.kex("::1", 5683))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    context.shutdown.assert_awaited_once()


async def test_core_failure_closes_connected_session(monkeypatch):
    session = SimpleNamespace(
        query_core=AsyncMock(side_effect=RuntimeError("bad core")), close=AsyncMock()
    )
    monkeypatch.setattr(nl_ltpdu.LtpduSession, "kex", AsyncMock(return_value=session))
    with pytest.raises(RuntimeError, match="bad core"):
        await nl_ltpdu.LtpduSession.connect("::1", 5683)
    session.close.assert_awaited_once()


async def test_busy_kex_closes_before_retry(monkeypatch):
    busy = Context(nl_ltpdu._create_tlv(0x01F1, b"\x08"))
    malformed = Context(b"\x01")
    calls = 0

    async def create() -> Context:
        nonlocal calls
        calls += 1
        if calls == 1:
            return busy
        busy.shutdown.assert_awaited_once()
        return malformed

    monkeypatch.setattr(nl_ltpdu.aiocoap.Context, "create_client_context", create)
    with pytest.raises(ValueError, match="TLV buffer too short"):
        await nl_ltpdu.LtpduSession.kex("::1", 5683)
    malformed.shutdown.assert_awaited_once()
