"""Tests for webhook auth: notify_crypto round-trip, header assembly, and the
_send_webhook (status, error) contract."""
import notify_crypto
import wg_crypto
import notifications


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
    # UDID mode, no iPhone -> can't decrypt -> request goes out unauthenticated.
    monkeypatch.setattr(notify_crypto, "ENC_FILE", str(tmp_path / "notify.enc"))
    monkeypatch.setattr(wg_crypto, "resolve_passphrase", lambda passphrase=None, config=None: None)
    assert notifications.webhook_auth_headers({"auth_enabled": True}) == {}


def test_send_webhook_returns_status_error_tuple():
    # No network: a malformed URL fails fast and must yield (None, message),
    # not raise — proving the (status, error) contract that fixes "status: None".
    status, err = notifications._send_webhook("http://", {"x": 1})
    assert status is None
    assert err
