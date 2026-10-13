"""Offline SRP and secure-session cipher vectors."""

# Synthetic fixtures and vector assertions use inferred types.
# ruff: noqa: ANN001, ANN201, ANN202

import hashlib

import pytest
from cryptography.exceptions import InvalidTag

from nl_ltpdu_v3 import N512, N2048, SecureCipher, SRPClient

PLAINTEXTS = [
    b"first request",
    b"long response spanning two AES blocks 0123456789",
    b"last",
]
NATIVE_CIPHERTEXTS = {
    0x0300: [
        "4e1fad9fe05dde6cb2165cbd419a2e",
        "4309490216589e19681f8a46684313cede567b7683caf8a4334b75792684d9f0700102"
        "e6daa7b47c53a8d49ab91044fd907d",
        "91f619b938e2",
    ],
    0x0301: [
        "0e76870cbb8af61d8050bde9ff0abc0711",
        "974eb77525f9a129b75c5d0e4a2be28887feb67534f1f34a863da33a0344569528b75"
        "ba56e5ce4dbeb30e13bb392cd95579601c9",
        "790c7edca5a9f77c",
    ],
    0x0302: [
        "cf9bdced0c891b79b333e18619b04b3fc9",
        "c8f4f6eedb61912c2d138fba687f10bb49b1962afe10149fe1720b4c8de220ab5c2cc"
        "de790298de7491704945de39ab5ccadc564",
        "d06a597cb5872249",
    ],
}


@pytest.mark.parametrize("version", NATIVE_CIPHERTEXTS)
def test_native_cipher_vectors_ordered_request_response(version):
    cipher = SecureCipher(version, bytes(range(32)), counter=7)
    vectors = [bytes.fromhex(value) for value in NATIVE_CIPHERTEXTS[version]]
    assert cipher.encrypt(PLAINTEXTS[0]) == vectors[0]
    assert cipher.decrypt(vectors[1]) == PLAINTEXTS[1]
    assert cipher.encrypt(PLAINTEXTS[2]) == vectors[2]
    assert cipher.counter == 10


@pytest.mark.parametrize("version", NATIVE_CIPHERTEXTS)
def test_native_cipher_vectors_decrypt(version):
    cipher = SecureCipher(version, bytes(range(32)), counter=7)
    for plain, wire in zip(PLAINTEXTS, NATIVE_CIPHERTEXTS[version], strict=True):
        assert cipher.decrypt(bytes.fromhex(wire)) == plain


@pytest.mark.parametrize("version", [0x0301, 0x0302])
def test_ccm_authentication_failure_does_not_advance_counter(version):
    cipher = SecureCipher(version, bytes(range(32)), counter=7)
    wire = bytes.fromhex(NATIVE_CIPHERTEXTS[version][0])
    corrupted = wire[:-1] + bytes([wire[-1] ^ 1])
    with pytest.raises(InvalidTag):
        cipher.decrypt(corrupted)
    assert cipher.counter == 7
    assert cipher.decrypt(wire) == PLAINTEXTS[0]


def test_ctr_crc_failure():
    cipher = SecureCipher(0x0300, bytes(range(32)), counter=7)
    wire = bytes.fromhex(NATIVE_CIPHERTEXTS[0x0300][0])
    with pytest.raises(ValueError, match="CRC mismatch"):
        cipher.decrypt(wire[:-1] + bytes([wire[-1] ^ 1]))
    assert cipher.counter == 7


@pytest.mark.parametrize("version", NATIVE_CIPHERTEXTS)
def test_truncated_ciphertext_rejected(version):
    cipher = SecureCipher(version, bytes(range(32)))
    with pytest.raises((ValueError, InvalidTag)):
        cipher.decrypt(b"\x01")
    assert cipher.counter == 0


@pytest.mark.parametrize("version,limit", [(0x0301, 2**40), (0x0302, 2**32)])
def test_ccm_counter_must_not_wrap(version, limit):
    cipher = SecureCipher(version, bytes(range(32)), limit)
    with pytest.raises(ValueError, match="counter exhausted"):
        cipher.encrypt(b"test")


def test_native_srp_512_vector():
    client = SRPClient(
        "Nanoleaf",
        bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 1]),
        private_key=bytes.fromhex(
            "11094fd849aaad9fd18f9aa9b8b1fade0eda411ced0cd6d05940b252472635678"
            "ab77b88c5f774a96b31cdb8ab9849c6583381f25d14880f3a3c40a5eb590405"
        ),
    )
    proof = client.process_challenge(
        bytes(range(16)),
        bytes.fromhex(
            "b9fd79b435642c73545ba6356defd95ee75ca3c4246700c04b107f1a57926fc24"
            "932b62cae313b37f38b93ada39aa84061f7f1fce3d49317e7680c657f02994f"
        ),
    )
    assert proof.hex() == "3ed6e4096ad9cef4f43ab2a3cbf79304ac01f28f9e6078189b640da7df9fd39b"
    assert client.shared_secret.hex() == (
        "a67406b59a1924a82ae8cd6d7385bc7da825c2d70c844db18d337bc86b3a717f"
    )
    assert client.server_proof.hex() == (
        "630df82a56df533140e8f641bbaa354d32d396854c60964f056a60739abde8a6"
    )
    client.verify_server(client.server_proof)
    with pytest.raises(ValueError, match="server proof"):
        client.verify_server(bytes(32))


@pytest.mark.parametrize("group,modulus", [(0, N512), (2, N2048)])
def test_srp_client_matches_independent_synthetic_server(group, modulus):
    def encode(number):
        return number.to_bytes((number.bit_length() + 7) // 8, "big")

    def digest(value):
        return hashlib.sha256(value).digest()

    username, password, salt = b"Nanoleaf", bytes([1, 2, 3, 4]), bytes(range(16))
    client = SRPClient(username.decode(), password, group, private_key=b"\x01")
    # Small A exercises minimal integer encoding instead of padding.
    assert client.public_key == b"\x02"
    multiplier = int.from_bytes(digest(encode(modulus) + b"\x02"), "big")
    x = int.from_bytes(digest(salt + digest(username + b":" + password)), "big")
    verifier = pow(2, x, modulus)
    server_private = 0x123456789ABCDEF
    server_public = (multiplier * verifier + pow(2, server_private, modulus)) % modulus
    proof = client.process_challenge(salt, encode(server_public))
    u = int.from_bytes(digest(client.public_key + encode(server_public)), "big")
    server_secret = pow(2 * pow(verifier, u, modulus), server_private, modulus)
    key = digest(encode(server_secret))
    assert client.shared_secret == key
    client.verify_server(digest(client.public_key + proof + key))


@pytest.mark.parametrize("server_public", [b"", b"\x00", N512.to_bytes(64, "big")])
def test_srp_rejects_zero_server_public(server_public):
    client = SRPClient("Nanoleaf", b"test", private_key=b"\x01")
    with pytest.raises(ValueError, match="server public"):
        client.process_challenge(bytes(16), server_public)


def test_srp_rejects_server_proof_before_challenge():
    client = SRPClient("Nanoleaf", b"test")
    with pytest.raises(ValueError, match="server proof"):
        client.verify_server(bytes(32))


def test_unsupported_protocol_parameters():
    with pytest.raises(ValueError, match="SRP group"):
        SRPClient("Nanoleaf", b"test", group=1)
    with pytest.raises(ValueError, match="private key"):
        SRPClient("Nanoleaf", b"test", private_key=b"\x00")
    with pytest.raises(ValueError, match="secure version"):
        SecureCipher(0x0303, bytes(32))
    with pytest.raises(ValueError, match="32 bytes"):
        SecureCipher(0x0301, bytes(16))
