"""Offline native UDP exchanges and legacy CoAP session lifecycle tests."""

# ruff: noqa: ANN001, ANN002, ANN201, ANN204, PLR0912, PLR0915

import asyncio
import hashlib
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import nl_ltpdu
from nl_ltpdu import AuthRejectedError, SessionExpiredError
from nl_ltpdu_v3 import (
    N512,
    LtpduV3Session,
    SecureCipher,
    _Datagrams,
    _fields,
    _operation_response,
    _operation_tlv,
    _tlv,
)


def integer(value):
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def digest(*values):
    return hashlib.sha256(b"".join(values)).digest()


class Peer(asyncio.DatagramProtocol):
    """Independent server SRP calculation; uses verified cipher primitive."""

    def __init__(self, version=0x302, bad_proof=False, drop_first=False, drop_reply=False):
        self.version = version
        self.bad_proof = bad_proof
        self.drop_first = drop_first
        self.drop_reply = drop_reply
        self.seen = []
        self.responses = {}
        self.token = bytes(range(16))
        self.cipher = None
        self.passwords = []
        self.requests = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, packet, addr):
        self.seen.append(packet)
        version, kind, mid, cid, endpoint, method, size = struct.unpack_from("!BBHHBBI", packet)
        assert (version, kind, size) == (1, 1, len(packet) - 12)
        if self.drop_first and len(self.seen) == 1:
            return
        if packet in self.responses:
            self.transport.sendto(self.responses[packet], addr)
            return
        raw = packet[12:]
        if endpoint == 5:
            assert method == 0 and raw == b""
            status, data = 2, b""  # valid framing, unsupported public GET
        elif endpoint == 1 and (self.cipher is None or raw.startswith(b"\x01\x00\x01")):
            fields = dict(_fields(raw))
            if 1 in fields:
                assert fields[7] == b"\x02"
                password = (
                    bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1]) if fields[1] == b"\x01" else self.token
                )
                self.passwords.append(password)
                if fields[1] == b"\x02":
                    assert cid == 42
                username, salt, private = b"Nanoleaf A19 E20", b"synthetic salt", 987654321
                modulus, generator = N512, 2
                a_public = int.from_bytes(fields[3], "big")
                x = int.from_bytes(digest(salt, digest(username, b":", password)), "big")
                verifier = pow(generator, x, modulus)
                k = int.from_bytes(digest(integer(modulus), integer(generator)), "big")
                b_public = (k * verifier + pow(generator, private, modulus)) % modulus
                u = int.from_bytes(digest(integer(a_public), integer(b_public)), "big")
                shared = digest(
                    integer(pow(a_public * pow(verifier, u, modulus), private, modulus))
                )
                xor = bytes(
                    a ^ b
                    for a, b in zip(
                        digest(integer(modulus)), digest(integer(generator)), strict=True
                    )
                )
                self.proof = digest(
                    xor, digest(username), salt, integer(a_public), integer(b_public), shared
                )
                self.server_proof = digest(integer(a_public), self.proof, shared)
                self.next_cipher = SecureCipher(self.version, shared)
                self.cipher = None
                data = (
                    _tlv(17, self.version.to_bytes(2, "big"))
                    + _tlv(2, salt)
                    + _tlv(3, integer(b_public))
                )
            else:
                assert fields[4] == self.proof
                data = _tlv(4, bytes(32) if self.bad_proof else self.server_proof)
                self.cipher = self.next_cipher
            status = 0
        else:
            plain = self.cipher.decrypt(raw)
            if endpoint == 1:
                fields = dict(_fields(plain))
                if 5 in fields:
                    data = self.cipher.encrypt(_tlv(6, self.token))
                else:
                    assert 9 in fields
                    data = b""  # native unpair accepts no payload
            else:
                assert endpoint == 2
                tag, length = struct.unpack_from("!HH", plain)
                assert tag == 1
                name = plain[4 : 4 + length].decode()
                self.requests.append(name)
                data = self.cipher.encrypt(
                    _operation_tlv(1, name.encode()) + _operation_tlv(3, b"\x00\x32")
                )
            status = 0
        response = struct.pack("!BBHHHI", 1, 2, mid, 42, status, len(data)) + data
        self.responses[packet] = response
        if endpoint == 2 and self.drop_reply:
            self.drop_reply = False
            return
        self.transport.sendto(response, addr)


@pytest.mark.parametrize("version", [0x300, 0x301, 0x302])
async def test_pair_token_reconnect_control_and_unpair(version):
    loop = asyncio.get_running_loop()
    transport, peer = await loop.create_datagram_endpoint(
        lambda: Peer(version, drop_reply=True), local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    session = None
    try:
        session = await LtpduV3Session.connect(
            "127.0.0.1", port, name="Nanoleaf A19 E20-ABCD", timeout=0.1
        )
        token = await session.pair("1234-567-8901", timeout=0.1)
        assert token.startswith(b"NLV3\x01")
        assert await session._request("lb/0/pb", timeout=0.01) == b"\x32"
        assert peer.seen[-2] == peer.seen[-1]
        await session.close()
        # Reset name omitted: username is retained in opaque stored credentials.
        session = await LtpduV3Session.connect("127.0.0.1", port, timeout=0.1)
        await session.auth(token, timeout=0.1)
        assert session.token == token
        assert await session._request("lb/0/pb", timeout=0.1) == b"\x32"
        await session.unpair(timeout=0.1)
        assert session.token is None
        assert peer.passwords == [bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1]), peer.token]
    finally:
        if session:
            await session.close()
        transport.close()


async def test_bad_server_proof_is_rejected():
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: Peer(bad_proof=True), local_addr=("127.0.0.1", 0)
    )
    session = await LtpduV3Session.connect(
        "127.0.0.1",
        transport.get_extra_info("sockname")[1],
        name="Nanoleaf A19 E20-ABCD",
        timeout=0.1,
    )
    try:
        with pytest.raises(AuthRejectedError, match="server authentication"):
            await session.pair("12345678901", timeout=0.1)
        with pytest.raises(SessionExpiredError):
            await session._request("di")
    finally:
        await session.close()
        transport.close()


async def test_retry_is_identical_and_malformed_packets_are_ignored():
    loop = asyncio.get_running_loop()
    transport, peer = await loop.create_datagram_endpoint(
        lambda: Peer(drop_first=True), local_addr=("127.0.0.1", 0)
    )
    client, protocol = await loop.create_datagram_endpoint(
        _Datagrams, remote_addr=("127.0.0.1", transport.get_extra_info("sockname")[1])
    )
    session = LtpduV3Session(client, protocol)
    try:
        status, _, _ = await session._exchange("nlinfo", b"", method=0, timeout=0.01)
        assert status == 2
        assert peer.seen[0] == peer.seen[1]
        assert not protocol.pending
        future = loop.create_future()
        protocol.pending[7] = future
        protocol.datagram_received(b"\x01", ())
        protocol.datagram_received(struct.pack("!BBHHHI", 1, 2, 7, 42, 0, 99), ())
        assert not future.done()
        protocol.pending.clear()
        future.cancel()
    finally:
        await session.close()
        transport.close()


def test_retransmit_delay_depends_on_the_endpoint():
    import nl_ltpdu_v3

    assert nl_ltpdu_v3.retransmit_after("nlsecure") == 15.0
    for endpoint in ("nlltpdu", "nlinfo", "nlevents", "nlidentify"):
        assert nl_ltpdu_v3.retransmit_after(endpoint) == 5.0


async def test_lost_datagram_is_resent_after_the_retransmit_delay_not_the_timeout(
    monkeypatch,
):
    import nl_ltpdu_v3

    monkeypatch.setattr(nl_ltpdu_v3, "_RETRANSMIT_AFTER", 0.02)
    loop = asyncio.get_running_loop()
    transport, peer = await loop.create_datagram_endpoint(
        lambda: Peer(drop_first=True), local_addr=("127.0.0.1", 0)
    )
    client, protocol = await loop.create_datagram_endpoint(
        _Datagrams, remote_addr=("127.0.0.1", transport.get_extra_info("sockname")[1])
    )
    session = LtpduV3Session(client, protocol)
    try:
        started = loop.time()
        status, _, _ = await session._exchange("nlinfo", b"", method=0, timeout=30.0)
        assert status == 2
        assert loop.time() - started < 5.0
        assert len(peer.seen) == 2
        assert peer.seen[0] == peer.seen[1]
    finally:
        await session.close()
        transport.close()


async def test_retransmit_delay_never_exceeds_the_caller_timeout(monkeypatch):
    import nl_ltpdu_v3

    monkeypatch.setattr(nl_ltpdu_v3, "_RETRANSMIT_AFTER", 30.0)
    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    client, protocol = await loop.create_datagram_endpoint(
        _Datagrams, remote_addr=("127.0.0.1", server.get_extra_info("sockname")[1])
    )
    session = LtpduV3Session(client, protocol)
    try:
        started = loop.time()
        with pytest.raises(TimeoutError, match="nlinfo timed out"):
            await session._exchange("nlinfo", b"", method=0, timeout=0.02, attempts=2)
        assert loop.time() - started < 5.0
    finally:
        await session.close()
        server.close()


async def test_cancelled_exchange_removes_pending_and_breaks_cipher():
    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    transport, protocol = await loop.create_datagram_endpoint(
        _Datagrams, remote_addr=("127.0.0.1", server.get_extra_info("sockname")[1])
    )
    session = LtpduV3Session(transport, protocol)
    session._cipher = SecureCipher(0x302, bytes(range(32)))
    try:
        task = asyncio.create_task(session._request("di"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not protocol.pending
        with pytest.raises(SessionExpiredError):
            await session._request("di")
    finally:
        await session.close()
        server.close()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        _operation_tlv(3, b"\0"),
        _operation_tlv(1, b"other") + _operation_tlv(3, b"\0"),
        _operation_tlv(1, b"di") + _operation_tlv(3, b"\x04"),
    ],
)
def test_invalid_operation_responses(data):
    with pytest.raises(RuntimeError):
        _operation_response(data, "di")


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

    async def create(**_kwargs: object) -> Context:
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


@pytest.mark.parametrize(("platform", "transport"), [("linux", "udp6"), ("darwin", "simple6")])
def test_coap_transport_matches_platform(platform, transport):
    # The transport is chosen at import time, so re-evaluate the module under each platform.
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("_nl_ltpdu_platform", nl_ltpdu.__file__)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "platform", platform)
        patch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    assert [transport] == module._COAP_TRANSPORTS


async def test_kex_names_transport_and_skips_default_probing(monkeypatch):
    # Default-transport probing imports optional packages and scans sys.path on the loop.
    def probing(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("default transport probing must not run")

    context = Context(b"\x01")
    create = AsyncMock(return_value=context)
    monkeypatch.setattr(nl_ltpdu.aiocoap.Context, "create_client_context", create)
    monkeypatch.setattr(nl_ltpdu.aiocoap.defaults, "get_default_clienttransports", probing)
    with pytest.raises(ValueError, match="TLV buffer too short"):
        await nl_ltpdu.LtpduSession.kex("::1", 5683)
    create.assert_awaited_once_with(transports=nl_ltpdu._COAP_TRANSPORTS)
