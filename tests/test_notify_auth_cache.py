"""Tests for the webhook auth header cache.

The bug these cover: the header is decrypted with the iPhone's serial number, but
most events that use it fire when the phone is gone — device_disconnected by
definition, every sync_* after unplugging, and backup_complete, which is sent
only after the e-ink has already said "Backup completed" and invited the user to
unplug. Resolution then failed, the code returned {} rather than signalling the
failure, and the request went out unauthenticated to be answered with 403 —
invisibly, since nothing carries a delivery receipt.

The cache lives in RAM-backed /run, so a powered-off device still gives nothing
away without the phone.
"""
import json
import os
import time

import pytest

import notifications


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "run"
    monkeypatch.setattr(notifications, "AUTH_CACHE_DIR", str(d))
    monkeypatch.setattr(notifications, "AUTH_CACHE_FILE", str(d / "webhook_auth.json"))
    monkeypatch.setattr(notifications, "AUTH_CACHE_TTL", 0)
    return d


WH = {"enabled": True, "auth_enabled": True, "url": "https://example.invalid/hook"}


# --- resolution and fallback -------------------------------------------------

def test_a_resolved_header_is_returned_and_cached(cache, monkeypatch):
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    assert notifications.webhook_auth_headers(WH) == {"X-Key": "s3cret"}
    assert notifications._read_auth_cache() == {"X-Key": "s3cret"}


def test_the_cache_answers_once_the_phone_is_gone(cache, monkeypatch):
    """The whole point: backup_complete and every sync_* fire after unplugging."""
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    notifications.webhook_auth_headers(WH)              # phone attached
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: None)
    assert notifications.webhook_auth_headers(WH) == {"X-Key": "s3cret"}


def test_auth_unavailable_and_uncached_returns_none_not_empty(cache, monkeypatch):
    """None means "wanted but unobtainable"; {} means "none configured". Reporting
    the first as the second is what sent unauthenticated requests into a 403."""
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: None)
    assert notifications.webhook_auth_headers(WH) is None


def test_no_auth_configured_is_an_empty_mapping(cache):
    assert notifications.webhook_auth_headers({"enabled": True}) == {}


def test_a_fresh_decrypt_overwrites_a_stale_cache(cache, monkeypatch):
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "old"})
    notifications.webhook_auth_headers(WH)
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "new"})
    assert notifications.webhook_auth_headers(WH) == {"X-Key": "new"}
    assert notifications._read_auth_cache() == {"X-Key": "new"}


# --- the file itself ---------------------------------------------------------

def test_the_cache_file_and_directory_are_owner_only(cache, monkeypatch):
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    notifications.webhook_auth_headers(WH)
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not meaningful on Windows")
    assert oct(os.stat(notifications.AUTH_CACHE_FILE).st_mode)[-3:] == "600"
    assert oct(os.stat(notifications.AUTH_CACHE_DIR).st_mode)[-3:] == "700"


def test_the_cache_lives_under_run_by_default():
    """Not /tmp (not guaranteed tmpfs on Armbian) and not /var/log (zram, but
    armbian-ramlog syncs it to the SD card). /run is RAM-only and wiped at boot."""
    assert notifications.AUTH_CACHE_DIR.startswith("/run/")


def test_only_the_header_is_cached_never_the_passphrase(cache, monkeypatch):
    """The passphrase is the phone's serial, which also unlocks the WireGuard and
    remote-sync credentials. Caching it would widen one webhook secret into all
    of them."""
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    notifications.webhook_auth_headers(WH)
    stored = json.load(open(notifications.AUTH_CACHE_FILE))
    assert set(stored) == {"headers", "at"}
    assert stored["headers"] == {"X-Key": "s3cret"}


def test_a_corrupt_cache_file_reads_as_absent(cache):
    os.makedirs(notifications.AUTH_CACHE_DIR, exist_ok=True)
    with open(notifications.AUTH_CACHE_FILE, "w") as f:
        f.write("{not json")
    assert notifications._read_auth_cache() is None


# --- lifetime ----------------------------------------------------------------

def test_clearing_the_cache_removes_it(cache, monkeypatch):
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    notifications.webhook_auth_headers(WH)
    assert notifications.clear_auth_cache() is True
    assert notifications._read_auth_cache() is None


def test_clearing_a_cache_that_is_not_there_is_not_an_error(cache):
    assert notifications.clear_auth_cache() is True


def test_a_ttl_expires_the_cache(cache, monkeypatch):
    monkeypatch.setattr(notifications, "AUTH_CACHE_TTL", 60)
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    notifications.webhook_auth_headers(WH)
    assert notifications._read_auth_cache() == {"X-Key": "s3cret"}

    data = json.load(open(notifications.AUTH_CACHE_FILE))
    data["at"] = time.time() - 3600                  # older than the TTL
    json.dump(data, open(notifications.AUTH_CACHE_FILE, "w"))
    assert notifications._read_auth_cache() is None


def test_ttl_zero_means_until_reboot(cache, monkeypatch):
    """/run is cleared at boot, so 0 needs no expiry of its own."""
    monkeypatch.setattr(notifications, "AUTH_CACHE_TTL", 0)
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    notifications.webhook_auth_headers(WH)
    data = json.load(open(notifications.AUTH_CACHE_FILE))
    data["at"] = 0                                   # ancient
    json.dump(data, open(notifications.AUTH_CACHE_FILE, "w"))
    assert notifications._read_auth_cache() == {"X-Key": "s3cret"}


# --- priming -----------------------------------------------------------------

def test_prime_auth_caches_while_the_phone_is_attached(cache, monkeypatch):
    monkeypatch.setattr(notifications, "_load_notify_config", lambda: {"webhook": WH})
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    assert notifications.prime_auth() is True
    assert notifications._read_auth_cache() == {"X-Key": "s3cret"}


def test_prime_auth_is_a_no_op_when_auth_is_not_enabled(cache, monkeypatch):
    monkeypatch.setattr(notifications, "_load_notify_config",
                        lambda: {"webhook": {"enabled": True, "auth_enabled": False}})
    assert notifications.prime_auth() is False
    assert notifications._read_auth_cache() is None


def test_prime_auth_reports_failure_when_the_phone_is_absent(cache, monkeypatch):
    monkeypatch.setattr(notifications, "_load_notify_config", lambda: {"webhook": WH})
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: None)
    assert notifications.prime_auth() is False


# --- what send_notification does with each outcome ---------------------------

def test_the_webhook_is_skipped_rather_than_sent_unauthenticated(cache, monkeypatch):
    """Sending blind guarantees a 403 and looks, from every side, exactly like
    nothing was sent. Skip and say so instead."""
    sent = []
    monkeypatch.setattr(notifications, "_load_notify_config", lambda: {"webhook": WH})
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: None)
    monkeypatch.setattr(notifications, "_spawn", lambda *a: sent.append(a))
    logged = []
    notifications.set_logger(logged.append)
    try:
        notifications.send_notification("backup_complete", {"usage": "60%"})
    finally:
        notifications.set_logger(None)
    assert sent == []
    assert any("skipped webhook" in m for m in logged)


def test_the_webhook_is_sent_when_the_header_comes_from_cache(cache, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "_load_notify_config", lambda: {"webhook": WH})
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: {"X-Key": "s3cret"})
    notifications.prime_auth()
    monkeypatch.setattr(notifications, "_resolve_auth_now", lambda wh: None)
    monkeypatch.setattr(notifications, "_spawn", lambda *a: sent.append(a))
    notifications.send_notification("sync_error", {"error": "boom"})
    assert len(sent) == 1
    assert sent[0][1][2] == {"X-Key": "s3cret"}      # headers handed to _send_webhook


def test_an_unauthenticated_webhook_still_sends_when_no_auth_is_configured(cache, monkeypatch):
    sent = []
    monkeypatch.setattr(notifications, "_load_notify_config",
                        lambda: {"webhook": {"enabled": True, "url": "https://x.invalid/h"}})
    monkeypatch.setattr(notifications, "_spawn", lambda *a: sent.append(a))
    notifications.send_notification("backup_complete")
    assert len(sent) == 1
    assert sent[0][1][2] == {}


# --- diagnostics reaching the per-run log ------------------------------------

def test_diagnostics_reach_the_callers_log():
    logged = []
    notifications.set_logger(logged.append)
    try:
        notifications._log("something went wrong")
    finally:
        notifications.set_logger(None)
    assert logged == ["[NOTIFY] something went wrong"]


def test_a_raising_logger_cannot_break_delivery():
    def bad(_):
        raise RuntimeError("log is closed")
    notifications.set_logger(bad)
    try:
        notifications._log("still fine")
    finally:
        notifications.set_logger(None)


def test_send_webhook_treats_a_none_header_as_no_header():
    """Pins why callers must check for None themselves: _send_webhook cannot
    distinguish it from {} — `if extra_headers` is false for both — so passing
    None straight through sends the request unauthenticated."""
    import inspect
    src = inspect.getsource(notifications._send_webhook)
    assert "if extra_headers:" in src
