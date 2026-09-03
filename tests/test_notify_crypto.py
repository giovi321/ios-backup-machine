"""Tests for webhook auth: notify_crypto round-trip, header assembly, and the
_send_webhook (status, error) contract."""
import pytest

import notify_crypto
import wg_crypto
import notifications


@pytest.fixture(autouse=True)
def isolated_auth_cache(tmp_path, monkeypatch):
    """Keep every test in this module off the real /run cache, in both
    directions: a resolved header must not be written there, and a header left
    there by the device must not answer a test."""
    d = tmp_path / "runtime"
    monkeypatch.setattr(notifications, "AUTH_CACHE_DIR", str(d))
    monkeypatch.setattr(notifications, "AUTH_CACHE_FILE", str(d / "webhook_auth.json"))
    monkeypatch.setattr(notifications, "AUTH_CACHE_TTL", 0)


def test_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_crypto, "ENC_FILE", str(tmp_path / "notify.enc"))
    d = {"header": "Authorization", "value": "Bearer abc123"}
    assert notify_crypto.encrypt_notify_config(d, "secret") is True
    assert notify_crypto.decrypt_notify_config(passphrase="secret") == d


def test_wrong_passphrase_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_crypto, "ENC_FILE", str(tmp_path / "notify.enc"))
    notify_crypto.encrypt_notify_config({"value": "x"}, "right")
    assert notify_crypto.decrypt_notify_config(passphrase="wrong") is None


def test_webhook_auth_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_crypto, "ENC_FILE", str(tmp_path / "notify.enc"))
    notify_crypto.encrypt_notify_config({"header": "Authorization", "value": "Bearer t"}, "pw")
    # simulate a resolvable passphrase (e.g. UDID mode with iPhone connected)
    monkeypatch.setattr(wg_crypto, "resolve_passphrase", lambda passphrase=None, config=None: "pw")

    assert notifications.webhook_auth_headers(
        {"auth_enabled": True, "auth_header": "Authorization"}) == {"Authorization": "Bearer t"}
    # config header name overrides the stored one
    assert notifications.webhook_auth_headers(
        {"auth_enabled": True, "auth_header": "X-Token"}) == {"X-Token": "Bearer t"}
    # disabled -> no header
    assert notifications.webhook_auth_headers({"auth_enabled": False}) == {}


def test_webhook_auth_headers_unresolvable(tmp_path, monkeypatch):
    """UDID mode, no iPhone, nothing cached -> None, meaning "auth was wanted and
    could not be obtained".

    This used to return {}, which send_notification could not tell apart from
    "no auth configured", so the request went out unauthenticated and an
    authenticated endpoint answered 403 with nothing recorded anywhere.
    """
    monkeypatch.setattr(notify_crypto, "ENC_FILE", str(tmp_path / "notify.enc"))
    monkeypatch.setattr(wg_crypto, "resolve_passphrase", lambda passphrase=None, config=None: None)
    assert notifications.webhook_auth_headers({"auth_enabled": True}) is None


def test_send_webhook_returns_status_error_tuple():
    # No network: a malformed URL fails fast and must yield (None, message),
    # not raise — proving the (status, error) contract that fixes "status: None".
    status, err = notifications._send_webhook("http://", {"x": 1})
    assert status is None
    assert err


# ---------------------------------------------------------------------------
# Corrupt / truncated credential stores degrade to None, never an exception
# ---------------------------------------------------------------------------
# Same contract as a wrong passphrase: the caller falls back to "no
# credentials" instead of crashing the code path that needed them.

def test_corrupt_enc_json_returns_none(tmp_path):
    enc = tmp_path / "wireguard.enc"
    enc.write_text("{not json at all")
    assert wg_crypto._decrypt_file("pw", str(enc)) is None


def test_truncated_enc_file_returns_none(tmp_path, monkeypatch):
    enc = tmp_path / "wireguard.enc"
    monkeypatch.setattr(wg_crypto, "ENC_FILE", str(enc))
    wg_crypto.encrypt_wg_config({"wg_conf": "x"}, passphrase="pw")
    full = enc.read_bytes()
    with open(enc, "wb") as f:
        f.write(full[: len(full) // 2])                     # torn mid-write
    assert wg_crypto._decrypt_file("pw", str(enc)) is None


def test_malformed_base64_fields_return_none(tmp_path):
    import json
    enc = tmp_path / "wireguard.enc"
    enc.write_text(json.dumps({"method": "aes-gcm", "nonce": "abc", "data": "def"}))
    assert wg_crypto._decrypt_file("pw", str(enc)) is None


def test_missing_fields_return_none(tmp_path):
    import json
    enc = tmp_path / "wireguard.enc"
    enc.write_text(json.dumps({"method": "aes-gcm"}))
    assert wg_crypto._decrypt_file("pw", str(enc)) is None


def test_non_mapping_payload_returns_none(tmp_path):
    enc = tmp_path / "wireguard.enc"
    enc.write_text('["not", "a", "mapping"]')
    assert wg_crypto._decrypt_file("pw", str(enc)) is None
