"""Fail-safe behavior of the web UI: the global error handler renders a friendly
500 page, a degraded config shows a banner and re-engages the setup wizard, and
web-launched syncs are detached from webui.service's cgroup so a restart can't
kill the rsync.
"""
import os
import tempfile

import pytest

SANDBOX = tempfile.mkdtemp(prefix="ibm_failsafe_")
os.environ.setdefault("IOSBACKUP_RUNTIME_DIR", os.path.join(SANDBOX, "rt"))
os.environ.setdefault("IOSBACKUP_LOG_DIR", os.path.join(SANDBOX, "log"))
os.environ.setdefault("IOSBACKUP_CONFIG", os.path.join(SANDBOX, "config.yaml"))

try:
    import webui
except Exception as exc:  # pragma: no cover - environment without flask
    pytest.skip(f"webui not importable: {exc}", allow_module_level=True)

import config_schema

webui.app.secret_key = "test-failsafe"


@webui.app.route("/__boom")
def _boom():
    raise RuntimeError("boom")


def _write_config(text):
    with open(webui.CONFIG_PATH, "w") as f:
        f.write(text)


def _client():
    webui.app.config["TESTING"] = False
    return webui.app.test_client()


# --- global error handler -----------------------------------------------------

def test_unhandled_exception_renders_friendly_500():
    r = _client().get("/__boom")
    assert r.status_code == 500
    assert b"Something went wrong" in r.data
    assert b"boom" not in r.data  # no internals leak


def test_http_errors_are_not_swallowed_by_the_handler():
    r = _client().get("/definitely-not-a-page")
    assert r.status_code == 404


# --- degraded config: banner + setup gate --------------------------------------

def test_degraded_banner_shows_when_config_has_warnings():
    _write_config("setup_completed: true\nwebui:\n  port: abc\n")
    r = _client().get("/settings/backup")
    assert r.status_code == 200
    assert b"config.yaml was corrupt or invalid" in r.data
    assert b"webui.port" in r.data


def test_no_banner_when_config_is_clean():
    _write_config("setup_completed: true\n")
    r = _client().get("/settings/backup")
    assert r.status_code == 200
    assert b"config.yaml was corrupt or invalid" not in r.data


def test_corrupt_config_reengages_setup_wizard():
    _write_config("wifi: [unclosed\n")
    webui.load_config()
    assert webui._setup_needed() is True


def test_missing_config_reengages_setup_wizard():
    if os.path.exists(webui.CONFIG_PATH):
        os.remove(webui.CONFIG_PATH)
    assert webui._setup_needed() is True
    # and a valid, completed config does not
    _write_config("setup_completed: true\n")
    assert webui._setup_needed() is False


# --- API guards ------------------------------------------------------------------

def test_export_config_404s_when_no_file_on_disk(monkeypatch):
    _write_config("setup_completed: true\n")
    monkeypatch.setattr(webui.os.path, "isfile", lambda p: False)
    r = _client().get("/api/export-config")
    assert r.status_code == 404
    assert r.get_json()["error"]


def test_sync_decrypt_with_corrupt_store_returns_json_error(monkeypatch):
    _write_config("setup_completed: true\n")
    monkeypatch.setattr(webui.wg_crypto, "get_iphone_serial", lambda: "serial")

    def boom(passphrase=None):
        raise ValueError("corrupt sync.enc")
    monkeypatch.setattr(webui.sync_crypto, "decrypt_sync_config", boom)
    r = _client().post("/api/sync/decrypt")
    assert r.status_code == 400
    assert "corrupt" in r.get_json()["error"]


# --- web-launched sync survives a webui restart ----------------------------------

def test_sync_launch_uses_systemd_run_when_available(monkeypatch):
    calls = {}

    class R:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(argv, **kw):
        calls["argv"] = argv
        return R()

    monkeypatch.setattr(webui.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(webui.subprocess, "run", fake_run)
    monkeypatch.setattr(webui.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen must not be used"))
    webui._launch_sync_detached("/app/backup-sync.py")
    # Outside webui.service's cgroup, so restarting the web UI can't kill it.
    assert calls["argv"][0] == "systemd-run"
    assert "--unit=iosbackup-web-sync" in calls["argv"]
    assert "--collect" in calls["argv"]
    assert "/app/backup-sync.py" in calls["argv"]


def test_sync_launch_falls_back_to_detached_popen_without_systemd(monkeypatch):
    popen = {}
    monkeypatch.setattr(webui.shutil, "which", lambda name: None)

    class FakePopen:
        def __init__(self, argv, **kw):
            popen.update(kw)
            popen["argv"] = argv

    monkeypatch.setattr(webui.subprocess, "Popen", FakePopen)
    webui._launch_sync_detached("/app/backup-sync.py")
    assert popen["argv"][1] == "/app/backup-sync.py"
    assert popen["start_new_session"] is True


def test_sync_launch_falls_back_when_systemd_run_refuses(monkeypatch):
    popen = {}

    class R:
        returncode = 1
        stderr = "Unit already active"
        stdout = ""

    class FakePopen:
        def __init__(self, argv, **kw):
            popen.update(kw)

    monkeypatch.setattr(webui.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(webui.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(webui.subprocess, "Popen", FakePopen)
    webui._launch_sync_detached("/app/backup-sync.py")
    assert popen["start_new_session"] is True


# --- purge keeps the live webui.log ----------------------------------------------

def test_purge_logs_keeps_webui_log(tmp_path, monkeypatch):
    (tmp_path / "webui.log").write_text("keep me")
    (tmp_path / "backup-2024.log").write_text("old")
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    r = _client().post("/logs/purge")
    assert r.status_code == 302
    assert (tmp_path / "webui.log").exists()
    assert not (tmp_path / "backup-2024.log").exists()
