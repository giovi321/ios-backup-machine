"""Round-trip tests for sync_crypto (encrypt/decrypt of remote-sync credentials)."""
import sync_crypto


def test_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_crypto, "ENC_FILE", str(tmp_path / "sync.enc"))
    data = {"host": "example.com", "port": 22, "username": "u",
            "password": "p", "remote_path": "/backups", "auth_method": "password"}
    assert sync_crypto.encrypt_sync_config(data, "secret") is True
    assert sync_crypto.decrypt_sync_config(passphrase="secret") == data


def test_wrong_passphrase_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_crypto, "ENC_FILE", str(tmp_path / "sync.enc"))
    sync_crypto.encrypt_sync_config({"host": "h"}, "right")
    assert sync_crypto.decrypt_sync_config(passphrase="wrong") is None


def test_decrypt_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_crypto, "ENC_FILE", str(tmp_path / "nope.enc"))
    assert sync_crypto.decrypt_sync_config(passphrase="secret") is None
