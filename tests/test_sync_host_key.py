"""Integration tests for host key pinning inside sync_manager.

The point of these is the wiring, not the verification logic (that lives in
test_host_key.py): does a configured fingerprint actually reach the ssh command
line, does a mismatch stop the sync before rsync runs, and is the unconfigured
path byte-for-byte the behaviour it had before.
"""
import os

import pytest

import host_key
import sync_manager

FP = "SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"


@pytest.fixture
def stub_env(monkeypatch, tmp_path):
    """Neutralize everything _prepare_sync touches except the host key logic."""
    monkeypatch.setattr(sync_manager, "_check_network_allowed", lambda: (True, ""))
    monkeypatch.setattr(sync_manager, "_diagnose_unreachable", lambda host, port: None)
    monkeypatch.setattr(sync_manager, "_load_backup_dir", lambda: str(tmp_path))

    cred = {
        "host": "server.example.com", "port": 2222, "username": "backup",
        "auth_method": "key", "ssh_key": "-----BEGIN KEY-----\nx\n-----END KEY-----",
        "password": "", "remote_path": "/backups/ios",
    }
    monkeypatch.setattr(sync_manager.sync_crypto, "decrypt_sync_config",
                        lambda passphrase=None, config=None: dict(cred))
    return cred


def _ssh_opts_from(cmd):
    """The ssh command rsync is told to use, i.e. the argument after -e."""
    return cmd[cmd.index("-e") + 1]


def test_without_a_fingerprint_the_ssh_command_is_unchanged(stub_env, monkeypatch):
    def unreachable(*a, **kw):
        raise AssertionError("must not verify when no fingerprint is configured")
    monkeypatch.setattr(sync_manager.host_key, "verify_and_write_known_hosts", unreachable)

    cmd, temp_files, err = sync_manager._prepare_sync()
    try:
        assert err is None
        opts = _ssh_opts_from(cmd)
        assert "StrictHostKeyChecking=accept-new" in opts
        assert "UserKnownHostsFile" not in opts
    finally:
        sync_manager._cleanup_temp(temp_files)


def test_a_verified_fingerprint_pins_the_ssh_connection(stub_env, monkeypatch, tmp_path):
    stub_env["host_key_fingerprint"] = FP
    pinned = str(tmp_path / "known_hosts")
    open(pinned, "w").close()
    monkeypatch.setattr(sync_manager.sync_crypto, "decrypt_sync_config",
                        lambda passphrase=None, config=None: dict(stub_env))
    monkeypatch.setattr(sync_manager.host_key, "verify_and_write_known_hosts",
                        lambda host, port, expected, **kw: (pinned, None))

    cmd, temp_files, err = sync_manager._prepare_sync()
    try:
        assert err is None
        opts = _ssh_opts_from(cmd)
        assert "StrictHostKeyChecking=yes" in opts
        assert f"UserKnownHostsFile={pinned}" in opts
        assert "accept-new" not in opts
        assert pinned in temp_files
    finally:
        sync_manager._cleanup_temp(temp_files)


def test_the_verified_host_and_port_are_the_ones_being_connected_to(stub_env, monkeypatch, tmp_path):
    stub_env["host_key_fingerprint"] = FP
    monkeypatch.setattr(sync_manager.sync_crypto, "decrypt_sync_config",
                        lambda passphrase=None, config=None: dict(stub_env))
    seen = {}
    pinned = str(tmp_path / "kh")
    open(pinned, "w").close()

    def record(host, port, expected, **kw):
        seen.update(host=host, port=port, expected=expected)
        return pinned, None
    monkeypatch.setattr(sync_manager.host_key, "verify_and_write_known_hosts", record)

    cmd, temp_files, err = sync_manager._prepare_sync()
    sync_manager._cleanup_temp(temp_files)
    assert seen == {"host": "server.example.com", "port": 2222, "expected": FP}


def test_a_mismatch_stops_the_sync_before_rsync_runs(stub_env, monkeypatch):
    stub_env["host_key_fingerprint"] = FP
    monkeypatch.setattr(sync_manager.sync_crypto, "decrypt_sync_config",
                        lambda passphrase=None, config=None: dict(stub_env))
    monkeypatch.setattr(sync_manager.host_key, "verify_and_write_known_hosts",
                        lambda host, port, expected, **kw: (None, "Host key mismatch - refusing to connect."))

    cmd, temp_files, err = sync_manager._prepare_sync()
    assert cmd is None
    assert err["success"] is False
    assert "mismatch" in err["message"].lower()


def test_the_pinned_known_hosts_is_deleted_after_the_sync(stub_env, monkeypatch, tmp_path):
    stub_env["host_key_fingerprint"] = FP
    pinned = str(tmp_path / "known_hosts")
    open(pinned, "w").close()
    monkeypatch.setattr(sync_manager.sync_crypto, "decrypt_sync_config",
                        lambda passphrase=None, config=None: dict(stub_env))
    monkeypatch.setattr(sync_manager.host_key, "verify_and_write_known_hosts",
                        lambda host, port, expected, **kw: (pinned, None))

    cmd, temp_files, err = sync_manager._prepare_sync()
    key_file = [p for p in temp_files if p != pinned]
    sync_manager._cleanup_temp(temp_files)

    assert not os.path.exists(pinned)
    assert all(not os.path.exists(p) for p in key_file), "the temp SSH key must go too"


def test_test_connection_reports_a_mismatch_without_opening_ssh(stub_env, monkeypatch):
    stub_env["host_key_fingerprint"] = FP
    monkeypatch.setattr(sync_manager.sync_crypto, "decrypt_sync_config",
                        lambda passphrase=None, config=None: dict(stub_env))
    monkeypatch.setattr(sync_manager.host_key, "verify_and_write_known_hosts",
                        lambda host, port, expected, **kw: (None, "Host key mismatch - refusing to connect."))

    def no_ssh(*a, **kw):
        raise AssertionError("ssh must not run when the host key does not verify")
    monkeypatch.setattr(sync_manager.subprocess, "run", no_ssh)

    result = sync_manager.test_connection()
    assert result["success"] is False
    assert "mismatch" in result["message"].lower()


def test_test_connection_pins_the_ssh_options_when_verified(stub_env, monkeypatch, tmp_path):
    stub_env["host_key_fingerprint"] = FP
    pinned = str(tmp_path / "kh")
    open(pinned, "w").close()
    monkeypatch.setattr(sync_manager.sync_crypto, "decrypt_sync_config",
                        lambda passphrase=None, config=None: dict(stub_env))
    monkeypatch.setattr(sync_manager.host_key, "verify_and_write_known_hosts",
                        lambda host, port, expected, **kw: (pinned, None))

    captured = {}

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return Result()
    monkeypatch.setattr(sync_manager.subprocess, "run", fake_run)

    result = sync_manager.test_connection()
    assert result["success"] is True
    assert "StrictHostKeyChecking=yes" in captured["cmd"]
    assert f"UserKnownHostsFile={pinned}" in captured["cmd"]
    assert "StrictHostKeyChecking=accept-new" not in captured["cmd"]
