"""Tests for wg_crypto: AES-GCM + XOR-fallback round-trips, key derivation, passphrase resolution."""
import builtins
import json

import wg_crypto


def test_encrypt_decrypt_round_trip(tmp_path):
    enc = str(tmp_path / "wg.enc")
    d = {"wg_conf": "[Interface]\nPrivateKey=abc\n"}
    assert wg_crypto._encrypt_dict(d, "pw", enc) is True
    assert wg_crypto._decrypt_file("pw", enc) == d


def test_decrypt_wrong_passphrase(tmp_path):
    enc = str(tmp_path / "wg.enc")
    wg_crypto._encrypt_dict({"x": 1}, "right", enc)
    assert wg_crypto._decrypt_file("wrong", enc) is None


def test_decrypt_missing_file(tmp_path):
    assert wg_crypto._decrypt_file("pw", str(tmp_path / "nope.enc")) is None


def test_derive_key_deterministic_and_sized():
    assert wg_crypto.derive_key("a") == wg_crypto.derive_key("a")
    assert wg_crypto.derive_key("a") != wg_crypto.derive_key("b")
    assert len(wg_crypto.derive_key("a")) == 32


def test_xor_fallback_round_trip(tmp_path, monkeypatch):
    # Force the no-cryptography path by making the cryptography import fail.
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("cryptography"):
            raise ImportError("forced for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    enc = str(tmp_path / "wg.enc")
    d = {"k": "v"}
    assert wg_crypto._encrypt_dict(d, "pw", enc) is True
    with open(enc) as f:
        assert json.load(f)["method"] == "xor-pbkdf2"
    assert wg_crypto._decrypt_file("pw", enc) == d


def test_resolve_passphrase_modes(monkeypatch):
    # Explicit passphrase always wins.
    assert wg_crypto.resolve_passphrase("explicit") == "explicit"
    # udid mode derives from the iPhone serial.
    monkeypatch.setattr(wg_crypto, "get_iphone_serial", lambda: "SERIAL123")
    assert wg_crypto.resolve_passphrase(
        None, {"credential_encryption": {"passphrase_mode": "udid"}}) == "SERIAL123"
    # custom mode (no passphrase given) yields None.
    assert wg_crypto.resolve_passphrase(
        None, {"credential_encryption": {"passphrase_mode": "custom"}}) is None
