"""Offline loopback UDP peer: complete SRP pairing/reconnect and controls."""

# Simulated peer fixtures retain the complete exchange in one handler.
# ruff: noqa: ANN001, ANN002, ANN201, ANN204, PLR0912, PLR0915

import asyncio
import hashlib
import struct

import pytest

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
