"""Fail-safe behavior of the web UI: the global error handler renders a friendly
500 page, a degraded config shows a banner and re-engages the setup wizard, and
web-launched syncs are detached from webui.service's cgroup so a restart can't
kill the rsync.
"""
import io
import json
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


# --- launchers are the only place a refused sync can be reported -----------------
# backup-sync.py's guards no longer write backup_status.json: the run they yield to
# owns that file, and a skipped launch overwriting it dropped the daemon's sync
# screen mid-transfer. The consequence is that a refused launch is now silent to the
# user unless the launcher checks first, so both launchers must.

def _run_sync_from_settings(monkeypatch, backup_running):
    _write_config("setup_completed: true\nsync:\n  enabled: true\n")
    launched = []
    monkeypatch.setattr(webui, "_backup_in_progress", lambda: backup_running)
    monkeypatch.setattr(webui, "_read_backup_status", lambda: {"state": "idle"})
    monkeypatch.setattr(webui.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    monkeypatch.setattr(webui, "_launch_sync_detached", lambda script: launched.append(script))
    r = _client().post("/settings/sync", data={"action": "run_sync"},
                       follow_redirects=True)
    return r, launched


def test_settings_page_refuses_to_launch_a_sync_during_a_backup(monkeypatch):
    r, launched = _run_sync_from_settings(monkeypatch, backup_running=True)
    assert launched == []
    assert b"backup is in progress" in r.data.lower()


def test_settings_page_still_launches_a_sync_when_no_backup_runs(monkeypatch):
    r, launched = _run_sync_from_settings(monkeypatch, backup_running=False)
    assert len(launched) == 1


def test_backup_in_progress_probe_cannot_hang_a_request(monkeypatch):
    """A wedged pgrep inside a request would hold a worker open forever."""
    seen = {}
    monkeypatch.setattr(webui, "_read_backup_status", lambda: {"state": "idle"})

    def fake_run(argv, **kw):
        seen["kw"] = kw
        return type("R", (), {"returncode": 1})()
    monkeypatch.setattr(webui.subprocess, "run", fake_run)
    assert webui._backup_in_progress() is False
    assert seen["kw"].get("timeout")


# --- destructive actions refuse once, then take an explicit override -------------
# Reboot, shutdown and update are the three web actions that end in-flight work:
# the first two cut the power out from under idevicebackup2, and update.sh stops
# every service (KillMode=mixed reaps the idevicebackup2 child with the daemon).
# They are refused once rather than blocked outright, because the owner must
# always be able to power the device off from the web UI — a wedged backup that
# could never be stopped would otherwise trap a remote user with no way out.
# The confirmed press quiesces first: only the daemon's own interrupted flow
# writes the [INTERRUPT] line into the per-run backup log that survives the
# reboot, so killing it any other way leaves a run log that stops mid-line.


def _write_status(state):
    os.makedirs(webui.RUNTIME_DIR, exist_ok=True)
    with open(os.path.join(webui.RUNTIME_DIR, "backup_status.json"), "w") as f:
        json.dump({"state": state}, f)


def _collect_popen(monkeypatch, sink):
    """Record the power-off argv a route fires. Only "shutdown" is collected:
    following the redirect renders the dashboard, which spawns its own probes
    (netutil's `ip addr show`), and those are not what these tests are about."""
    class FakeProc:
        pid = 1234
        returncode = 0

        def __init__(self):
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, *a, **k):
            return ("", "")

        def wait(self, *a, **k):
            return 0

    def fake(argv, **kw):
        if argv and argv[0] == "shutdown":
            sink.append(list(argv))
        return FakeProc()
    monkeypatch.setattr(webui.subprocess, "Popen", fake)


def _busy(monkeypatch, state, backup_running):
    _write_config("setup_completed: true\n")
    monkeypatch.setattr(webui, "_read_backup_status", lambda: {"state": state})
    monkeypatch.setattr(webui, "_backup_in_progress", lambda: backup_running)
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)


def _repo_with_update_script(monkeypatch, tmp_path):
    (tmp_path / "update.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(webui, "REPO_DIR", str(tmp_path))


def test_reboot_refuses_once_while_a_backup_runs(monkeypatch):
    _busy(monkeypatch, "backing_up", True)
    fired = []
    _collect_popen(monkeypatch, fired)
    r = _client().post("/api/reboot")
    assert fired == []
    assert "confirm=reboot" in r.headers["Location"]


def test_shutdown_refuses_once_while_a_sync_runs(monkeypatch):
    _busy(monkeypatch, "syncing", False)
    fired = []
    _collect_popen(monkeypatch, fired)
    r = _client().post("/api/shutdown", follow_redirects=True)
    assert fired == []
    # The dashboard has a Cancel Sync button of its own, so match the refusal
    # itself rather than the word.
    assert b"Stop it first with Cancel Sync" in r.data


def test_update_refuses_once_while_a_backup_runs(monkeypatch, tmp_path):
    _busy(monkeypatch, "backing_up", True)
    _repo_with_update_script(monkeypatch, tmp_path)
    launched, marked = [], []
    monkeypatch.setattr(webui, "_update_launch_command",
                        lambda *a, **k: launched.append("cmd") or ["/bin/true"])
    monkeypatch.setattr(webui, "_mark_updating", lambda *a, **k: marked.append("mark"))
    _collect_popen(monkeypatch, launched)
    r = _client().post("/update", data={"action": "update"})
    assert launched == []
    # Nothing was marked either, so a refused update leaves no stranded
    # "Updating" screen on the e-ink.
    assert marked == []
    assert "confirm=update" in r.headers["Location"]


def test_a_refused_reboot_offers_an_explicit_override(monkeypatch):
    _busy(monkeypatch, "backing_up", True)
    _collect_popen(monkeypatch, [])
    r = _client().post("/api/reboot", follow_redirects=True)
    assert b"Reboot anyway" in r.data
    assert b'name="confirm" value="1"' in r.data
    assert b'action="/api/reboot"' in r.data


def test_confirming_a_reboot_stops_the_backup_first_then_reboots(monkeypatch):
    _busy(monkeypatch, "backing_up", True)
    order = []
    monkeypatch.setattr(webui, "_stop_backup_cleanly", lambda: order.append("stop"))
    _collect_popen(monkeypatch, order)
    _client().post("/api/reboot", data={"confirm": "1"})
    assert order == ["stop", ["shutdown", "-r", "+0"]]


def test_confirming_a_shutdown_cancels_a_running_sync_first(monkeypatch):
    _busy(monkeypatch, "syncing", False)
    order = []
    monkeypatch.setattr(webui, "_cancel_sync_cleanly", lambda: order.append("cancel"))
    _collect_popen(monkeypatch, order)
    _client().post("/api/shutdown", data={"confirm": "1"})
    assert order == ["cancel", ["shutdown", "-h", "+0"]]


def test_confirming_an_update_stops_the_backup_before_marking_the_panel(monkeypatch, tmp_path):
    """The daemon's interrupted screen and its log line must land before the
    Updating sentinel, or the run log never records why the backup ended."""
    _busy(monkeypatch, "backing_up", True)
    _repo_with_update_script(monkeypatch, tmp_path)
    order = []
    monkeypatch.setattr(webui, "_stop_backup_cleanly", lambda: order.append("stop"))
    monkeypatch.setattr(webui, "_mark_updating",
                        lambda *a, **k: order.append("mark_updating"))
    monkeypatch.setattr(webui, "_update_launch_command", lambda *a, **k: ["/bin/true"])
    monkeypatch.setattr(webui.subprocess, "Popen",
                        lambda *a, **k: order.append("launch"))
    _client().post("/update", data={"action": "update", "confirm": "1"})
    assert order == ["stop", "mark_updating", "launch"]


def test_a_live_sync_is_named_ahead_of_a_stale_backup_probe(monkeypatch):
    """A leftover idevicebackup2 still matches the pgrep during a sync; naming a
    backup would send the user to a Stop Backup button that cannot help."""
    _busy(monkeypatch, "syncing", True)
    assert webui._busy_with() == "sync"
    _collect_popen(monkeypatch, [])
    r = _client().post("/api/reboot", follow_redirects=True)
    assert b"Stop it first with Cancel Sync" in r.data
    assert b"Stop it first with Stop Backup" not in r.data


def test_a_broken_busy_probe_never_traps_the_owner(monkeypatch):
    """Fails open on purpose: a device whose status file is unreadable must
    still be possible to power off from the web UI."""
    _write_config("setup_completed: true\n")

    def boom():
        raise OSError("status file unreadable")
    monkeypatch.setattr(webui, "_read_backup_status", boom)
    assert webui._busy_with() is None
    fired = []
    _collect_popen(monkeypatch, fired)
    _client().post("/api/reboot")
    assert fired == [["shutdown", "-r", "+0"]]


def test_the_quiesce_wait_is_bounded(monkeypatch):
    """A dead daemon never clears the stop sentinel; the wait must give up and
    reboot anyway rather than leaving the owner unable to power down."""
    _busy(monkeypatch, "backing_up", True)
    os.makedirs(webui.RUNTIME_DIR, exist_ok=True)
    stop_file = os.path.join(webui.RUNTIME_DIR, "stop_requested")
    open(stop_file, "w").close()
    try:
        slept = []
        monkeypatch.setattr(webui, "_stop_backup_cleanly", lambda: None)
        monkeypatch.setattr(webui.time, "sleep", lambda s: slept.append(s))
        fired = []
        _collect_popen(monkeypatch, fired)
        _client().post("/api/reboot", data={"confirm": "1"})
        assert slept and len(slept) <= webui._QUIESCE_POLLS
        assert fired == [["shutdown", "-r", "+0"]]
    finally:
        if os.path.exists(stop_file):
            os.remove(stop_file)


def test_an_unknown_confirm_argument_renders_no_override():
    """Only the three known actions get a button, and junk cannot 500 the page."""
    _write_config("setup_completed: true\n")
    r = _client().get("/?confirm=rm-rf")
    assert r.status_code == 200
    assert b'name="confirm" value="1"' not in r.data


def test_reboot_is_unguarded_when_the_device_is_idle(monkeypatch):
    # Companion guard on behaviour that must not change; it passes pre-change too.
    _write_config("setup_completed: true\n")
    _write_status("complete")
    runs = []

    def fake_run(argv, **kw):
        runs.append(argv)
        return type("R", (), {"returncode": 1})()
    monkeypatch.setattr(webui.subprocess, "run", fake_run)
    popen = {}

    def fake_popen(argv, **kw):
        popen["argv"] = argv
        popen["kw"] = kw
        return type("P", (), {"pid": 1})()
    monkeypatch.setattr(webui.subprocess, "Popen", fake_popen)
    _client().post("/api/reboot")
    assert popen["argv"] == ["shutdown", "-r", "+0"]
    assert popen["kw"]["start_new_session"] is True
    assert not any("pkill" in a for a in runs)  # nothing was quiesced


def test_shutdown_is_unguarded_when_the_device_is_idle(monkeypatch):
    # Companion guard on behaviour that must not change; it passes pre-change too.
    _write_config("setup_completed: true\n")
    _write_status("complete")
    monkeypatch.setattr(webui.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1})())
    fired = []
    _collect_popen(monkeypatch, fired)
    _client().post("/api/shutdown")
    assert fired == [["shutdown", "-h", "+0"]]


def test_stop_backup_route_still_reports_a_failed_stop(monkeypatch):
    """The clean stop moved into a helper; the route must keep turning a failure
    into a flashed error rather than a 500."""
    _write_config("setup_completed: true\n")

    def boom():
        raise OSError("pkill missing")
    monkeypatch.setattr(webui, "_stop_backup_cleanly", boom)
    r = _client().post("/api/stop-backup", follow_redirects=True)
    assert r.status_code == 200
    assert b"pkill missing" in r.data


def test_sync_cancel_route_still_reports_a_failed_cancel(monkeypatch):
    _write_config("setup_completed: true\n")
    monkeypatch.setattr(webui, "_read_backup_status", lambda: {"state": "syncing"})

    def boom():
        raise OSError("pkill missing")
    monkeypatch.setattr(webui, "_cancel_sync_cleanly", boom)
    r = _client().post("/sync/cancel", follow_redirects=True)
    assert r.status_code == 200
    assert b"Failed to cancel sync" in r.data


# --- config import: bounded uploads and no silent corrections --------------------
# The only upload is the config import, so the request cap exists to stop a
# mis-picked photo or archive being read into RAM and handed to PyYAML on a
# 512 MB board. The 413 it raises is handled, because Werkzeug's bare "Request
# Entity Too Large" page has no sidebar and no way back. And the import now says
# which values it had to reset: it saves the corrected tree, so the next load is
# clean and the degraded-config banner can never surface them afterwards.

# 26 known keys given the wrong type — more than IMPORT_WARNING_LIMIT, so the
# list has to be trimmed. setup_completed stays valid so the import cannot
# re-engage the setup wizard for the tests that run after these.
_WRONG_TYPED_IMPORT = b"""setup_completed: true
backup_dir: 5
marker_file: 5
disk_device: 5
orientation: 5
font_path: 5
owner_lines: 5
error_codes: 5
env: 5
auth:
  password_hash: 5
backup:
  auto_start: yes-please
  notify_on_rejected: yes-please
  hang_timeout_sec: abc
  max_duration_sec: abc
backup_encryption:
  encryption_confirmed: abc
device_filter:
  enabled: abc
  allowed_devices: 5
wifi:
  enabled: abc
  ssid: 5
  password: 5
  networks: 5
ntp:
  enabled: abc
  servers: 5
webui:
  enabled: abc
  port: abc
  bind_interfaces: 5
  secret_key: 5
"""


def _import_post(payload):
    return _client().post(
        "/api/import-config",
        data={"config_file": (io.BytesIO(payload), "config.yaml")},
        content_type="multipart/form-data",
        follow_redirects=True)


def test_oversized_upload_is_refused_before_it_is_read():
    _write_config("setup_completed: true\n")
    before = open(webui.CONFIG_PATH, "rb").read()
    r = _import_post(b"a" * (webui.MAX_UPLOAD_BYTES + 1024))
    assert r.status_code == 200
    limit = b"larger than the %d KB limit" % (webui.MAX_UPLOAD_BYTES // 1024)
    assert limit in r.data
    assert open(webui.CONFIG_PATH, "rb").read() == before


def test_oversized_form_post_gets_a_page_not_a_bare_413():
    _write_config("setup_completed: true\n")
    r = _client().post("/settings/general",
                       data={"backup_dir": "y" * (600 * 1024)},
                       follow_redirects=True)
    assert r.status_code == 200
    assert b"Upload rejected" in r.data
    assert b"413 Request Entity Too Large" not in r.data


def test_import_reports_the_values_it_reset():
    _write_config("setup_completed: true\n")
    r = _import_post(b"setup_completed: true\nwebui:\n  port: abc\n")
    assert b"Config imported" in r.data
    assert b"webui.port" in r.data
    assert b"invalid value" in r.data
    assert webui.load_config()["webui"]["port"] == 8080


def test_import_warning_list_is_capped():
    _write_config("setup_completed: true\n")
    r = _import_post(_WRONG_TYPED_IMPORT)
    assert r.data.count(b"has an invalid value") == webui.IMPORT_WARNING_LIMIT
    assert b"more invalid value(s) were reset" in r.data


def test_clean_import_says_nothing_about_corrections():
    # Regression guard: a good file must not be reported as corrected.
    _write_config("setup_completed: true\n")
    r = _import_post(b"setup_completed: true\nwebui:\n  port: 9090\n")
    assert b"Config imported" in r.data
    assert b"invalid value" not in r.data
    assert webui.load_config()["webui"]["port"] == 9090


# --- the log viewer is bounded ----------------------------------------------------
# view_log used to f.read() the whole file and hand it to Jinja to escape and to
# buffer. The Logs page is opened precisely when something has already gone wrong,
# so one runaway log made the web UI the next thing to fail.

def _big_log(tmp_path, name="backup-2024-01-01.log", nlines=60000):
    p = tmp_path / name
    lines = [b"HEADFIRST first line of the run\n"]
    lines += [b"MIDDLE%06d padding padding padding padding padding padding\n" % i
              for i in range(nlines)]
    lines.append(b"TAILLAST the run ended here\n")
    p.write_bytes(b"".join(lines))
    return p


class _CountingFile:
    """A real handle that records how many bytes were actually pulled off it."""

    def __init__(self, fh, counter):
        self._fh = fh
        self._counter = counter

    def read(self, *args):
        data = self._fh.read(*args)
        self._counter[0] += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()


def _count_reads_of(monkeypatch, name):
    real_open = open

    counter = [0]

    def counting_open(path, *args, **kwargs):
        fh = real_open(path, *args, **kwargs)
        if os.path.basename(str(path)) == name:
            return _CountingFile(fh, counter)
        return fh

    monkeypatch.setattr(webui, "open", counting_open, raising=False)
    return counter


def test_view_log_never_reads_the_whole_file(tmp_path, monkeypatch):
    # The point of the item: the read is bounded by the byte ceiling, not by the
    # size of whatever file the user happened to click on.
    p = _big_log(tmp_path)
    assert p.stat().st_size > 3_000_000      # ~14x the viewer's byte ceiling
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    read_bytes = _count_reads_of(monkeypatch, p.name)
    r = _client().get("/logs/" + p.name)
    assert r.status_code == 200
    assert 0 < read_bytes[0] <= 512 * 1024
    assert read_bytes[0] <= webui.LOG_VIEW_MAX_BYTES


def test_view_log_shows_both_ends_but_not_the_whole_file(tmp_path, monkeypatch):
    p = _big_log(tmp_path)
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    r = _client().get("/logs/" + p.name)
    assert r.status_code == 200
    assert b"HEADFIRST" in r.data          # a backup's gates are at the top
    assert b"TAILLAST" in r.data           # what it died of is at the bottom
    assert b"MIDDLE030000" not in r.data
    assert len(r.data) < 200_000


def test_view_log_flags_the_truncation_and_offers_the_download(tmp_path, monkeypatch):
    p = _big_log(tmp_path)
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    c = _client()
    r = c.get("/logs/" + p.name)
    assert b"Only the beginning and the end" in r.data
    assert b"showing the first 40 and the last 500 lines" in r.data
    assert ("/logs/%s/download" % p.name).encode() in r.data
    # A log that fits is still shown whole, with nothing said about cutting it.
    small = tmp_path / "sync-2024-01-01.log"
    small.write_bytes(b"one\ntwo\n")
    r = c.get("/logs/" + small.name)
    assert b"one\ntwo" in r.data
    assert b"Only the beginning and the end" not in r.data
    assert b"showing the first" not in r.data


def test_view_log_raw_serves_the_bounded_text_as_plain_text(tmp_path, monkeypatch):
    p = _big_log(tmp_path)
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    r = _client().get("/logs/%s?raw=1" % p.name)
    assert r.status_code == 200
    assert r.content_type.startswith("text/plain")
    assert b"<pre" not in r.data
    assert r.data.rstrip().endswith(b"TAILLAST the run ended here")
    assert len(r.data) < 200_000


def test_download_log_serves_the_whole_file_as_an_attachment(tmp_path, monkeypatch):
    p = _big_log(tmp_path)
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    r = _client().get("/logs/%s/download" % p.name)
    assert r.status_code == 200
    assert "attachment" in r.headers["Content-Disposition"]
    assert int(r.headers["Content-Length"]) == p.stat().st_size
    # The file wrapper served it in chunks rather than buffering a body.
    assert r.headers.get("Accept-Ranges") == "bytes"
    assert b"HEADFIRST" in r.data and b"TAILLAST" in r.data


def test_download_log_redirects_when_the_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    r = _client().get("/logs/nope.log/download")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/logs")


def test_view_log_missing_file_still_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    r = _client().get("/logs/nope.log")
    assert r.status_code == 200
    assert b"Log file not found." in r.data


def test_read_bounded_returns_a_small_log_whole(tmp_path):
    p = tmp_path / "s.log"
    p.write_bytes(b"a\nb\nc\n")
    assert webui._read_bounded(str(p), 40, 500, 4096) == ("a\nb\nc\n", False, 6)
    p.write_bytes(b"a\nb\nc")
    assert webui._read_bounded(str(p), 40, 500, 4096) == ("a\nb\nc", False, 5)
    p.write_bytes(b"")
    assert webui._read_bounded(str(p), 40, 500, 4096) == ("", False, 0)
    p.write_bytes(b"".join(b"L%05d\n" % i for i in range(540)))
    text, truncated, _ = webui._read_bounded(str(p), 40, 500, 1 << 20)
    assert truncated is False and text.count("\n") == 540


def test_read_bounded_is_bounded_by_bytes_not_just_lines(tmp_path):
    # A writer wedged mid-line leaves a file with no newline in it at all, and a
    # line-counting tail on that one reads the whole thing.
    p = tmp_path / "wedged.log"
    p.write_bytes(b"X" * 400_000)
    text, truncated, size = webui._read_bounded(str(p), 40, 500, 8192)
    assert truncated is True
    assert size == 400_000
    assert len(text) < 12_000
    # Not the empty string a naive "drop the partial first line" tail returns.
    assert text.startswith("X") and text.rstrip().endswith("X")


def test_read_bounded_splits_a_log_that_fits_in_memory_but_not_on_screen(tmp_path):
    p = tmp_path / "many.log"
    p.write_bytes(b"".join(b"L%05d\n" % i for i in range(5000)))
    text, truncated, _ = webui._read_bounded(str(p), 40, 500, 1 << 20)
    assert truncated is True
    assert "L00000" in text and "L04999" in text
    assert "L02500" not in text
    assert text.count("[web UI] showing the first") == 1
    p.write_bytes(b"".join(b"L%05d\n" % i for i in range(541)))
    text, truncated, _ = webui._read_bounded(str(p), 40, 500, 1 << 20)
    assert truncated is True
    assert "L00039" in text and "L00540" in text
    assert "L00040" not in text


# --- the system journal is reachable from the UI ----------------------------------
# Both long-running units log with StandardOutput=journal, and the hardening pass
# sent much of its own diagnostics only there ("[WARN] display init failed",
# "[WATCHDOG] ...", "[FATAL] main loop stalled"). Before this page that entire
# class of daemon-level degradation was visible over SSH and nowhere else.


class _JR:
    """journalctl's completed process, as subprocess.run returns it."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _journal_client(monkeypatch, tail=None, boots=None, which="/usr/bin/journalctl"):
    """Stand in for journalctl. The route makes two different calls - the tail
    and --list-boots - so the fake has to dispatch on argv rather than return one
    canned result. Returns (tail_calls, boots_calls) of (argv, kwargs)."""
    tail_calls = []
    boots_calls = []

    def fake_run(argv, **kw):
        if "--list-boots" in argv:
            boots_calls.append((list(argv), kw))
            return boots() if callable(boots) else (
                boots if boots is not None else _JR(0, " 0 abc123 Mon 2026-01-01\n"))
        tail_calls.append((list(argv), kw))
        return tail() if callable(tail) else (
            tail if tail is not None else _JR(0, "journal body\n"))

    monkeypatch.setattr(webui.shutil, "which", lambda name: which)
    monkeypatch.setattr(webui.subprocess, "run", fake_run)
    _write_config("setup_completed: true\n")
    return tail_calls, boots_calls


def test_journal_page_tails_the_daemon_unit(monkeypatch):
    tail, _ = _journal_client(monkeypatch, tail=_JR(
        0, "2026-01-01T00:00:00 host x[1]: [WARN] display init failed\n"))
    r = _client().get("/logs/journal")
    assert r.status_code == 200
    argv = tail[0][0]
    assert argv[0] == "journalctl"
    assert "--no-pager" in argv and "--output=short-iso" in argv
    assert argv[argv.index("-u") + 1] == "iosbackupmachine"
    assert argv[argv.index("-n") + 1] == "500"
    assert tail[0][1].get("timeout")          # never an unbounded child
    assert b"display init failed" in r.data


def test_journal_page_ignores_an_unknown_unit(monkeypatch):
    # A hand-edited ?unit= must not reach argv: a leading "-" is read as a
    # journalctl option, and this one would vacuum the journal it is showing.
    tail, _ = _journal_client(monkeypatch)
    r = _client().get("/logs/journal?unit=--vacuum-size%3D1")
    assert r.status_code == 200
    argv = tail[0][0]
    assert "--vacuum-size=1" not in argv
    assert argv[argv.index("-u") + 1] == "iosbackupmachine"


def test_journal_page_snaps_a_bad_line_count(monkeypatch):
    tail, _ = _journal_client(monkeypatch)
    c = _client()
    c.get("/logs/journal?lines=999999")
    c.get("/logs/journal?lines=notanumber")
    c.get("/logs/journal?lines=2000")
    counts = [argv[argv.index("-n") + 1] for argv, _kw in tail]
    assert counts == ["500", "500", "2000"]


def test_journal_page_can_show_every_unit(monkeypatch):
    # The kernel's "usb 1-1: USB disconnect" is what explains an interrupted
    # backup, and it belongs to no unit at all.
    tail, _ = _journal_client(monkeypatch)
    r = _client().get("/logs/journal?unit=*")
    assert r.status_code == 200
    assert "-u" not in tail[0][0]


def test_journal_page_explains_a_missing_journalctl(monkeypatch):
    _write_config("setup_completed: true\n")
    monkeypatch.setattr(webui.shutil, "which", lambda name: None)
    monkeypatch.setattr(webui.subprocess, "run",
                        lambda *a, **k: pytest.fail("journalctl must not be run"))
    r = _client().get("/logs/journal")
    assert r.status_code == 200
    assert b"journalctl is not available" in r.data


def test_journal_page_survives_a_wedged_journalctl(monkeypatch):
    def boom():
        raise webui.subprocess.TimeoutExpired(cmd="journalctl", timeout=10)

    _journal_client(monkeypatch, tail=boom)
    r = _client().get("/logs/journal")
    assert r.status_code == 200
    assert b"may be wedged" in r.data
    # The point of the page: what explains the failure is not itself the
    # generic 500 page.
    assert b"Something went wrong" not in r.data


def test_journal_page_shows_why_journalctl_failed(monkeypatch):
    _journal_client(monkeypatch, tail=_JR(
        1, "", "Failed to add match: Invalid argument"))
    r = _client().get("/logs/journal")
    assert r.status_code == 200
    assert b"Failed to add match: Invalid argument" in r.data


def test_journal_page_caps_a_flood(monkeypatch):
    _journal_client(monkeypatch, tail=_JR(
        0, "OLDEST\n" + "x" * 2_000_000 + "\nNEWEST"))
    r = _client().get("/logs/journal")
    assert r.status_code == 200
    assert b"NEWEST" in r.data            # the newest lines explain the failure
    assert b"OLDEST" not in r.data        # so the tail is kept, not the head
    assert b"truncated" in r.data
    assert len(r.data) < 2 * webui.JOURNAL_MAX_CHARS


def test_journal_route_is_not_shadowed_by_the_log_file_viewer(monkeypatch):
    # /logs/journal has to win over /logs/<filename>, or the page silently
    # becomes "Log file not found."
    _journal_client(monkeypatch, tail=_JR(0, "journal body\n"))
    r = _client().get("/logs/journal")
    assert b"Log file not found" not in r.data
    assert b"journal body" in r.data


def test_journal_page_warns_when_only_the_current_boot_is_kept(monkeypatch):
    _journal_client(monkeypatch, boots=_JR(0, " 0 abc123 Mon 2026-01-01\n"))
    r = _client().get("/logs/journal")
    assert b"Only the current boot" in r.data

    _journal_client(monkeypatch, boots=_JR(
        0, "-2 aaa Mon\n-1 bbb Tue\n 0 ccc Wed\n"))
    r = _client().get("/logs/journal")
    assert b"Only the current boot" not in r.data
    assert b"3 boots" in r.data

    # systemd >= 254 prints a table header; counting it would report two boots
    # on a journal that holds one.
    _journal_client(monkeypatch, boots=_JR(
        0, "IDX BOOT ID                          FIRST ENTRY\n"
           "  0 abc123                           Mon 2026-01-01\n"))
    r = _client().get("/logs/journal")
    assert b"Only the current boot" in r.data

    _journal_client(monkeypatch, boots=_JR(1, "", "No journal files were found."))
    r = _client().get("/logs/journal")
    assert b"Could not determine" in r.data


def test_journal_page_can_filter_to_warnings(monkeypatch):
    body = ("2026-01-01T00:00:01 host d[1]: starting up\n"
            "2026-01-01T00:00:02 host d[1]: [DRAW] display still unavailable\n"
            "2026-01-01T00:00:03 host d[1]: backup finished\n"
            "2026-01-01T00:00:04 host d[1]: [WATCHDOG] terminating\n")
    tail, _ = _journal_client(monkeypatch, tail=_JR(0, body))
    r = _client().get("/logs/journal?problems=1")
    assert r.status_code == 200
    assert b"display still unavailable" in r.data
    assert b"[WATCHDOG] terminating" in r.data
    assert b"starting up" not in r.data
    assert b"backup finished" not in r.data
    # Filtered from what was already read - the filter is not a second fork.
    assert len(tail) == 1

    tail, _ = _journal_client(monkeypatch, tail=_JR(0, body))
    r = _client().get("/logs/journal")
    assert b"starting up" in r.data and b"backup finished" in r.data


def test_logs_page_links_to_the_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    _write_config("setup_completed: true\n")
    r = _client().get("/logs")
    assert r.status_code == 200
    assert b"/logs/journal" in r.data


def test_the_journal_problem_filter_does_not_claim_there_are_no_problems(monkeypatch):
    """The filter matches the two daemons' bracket vocabulary. A unit that
    reports trouble another way finds nothing, and saying "no warnings" there
    would be the page misleading the user about the thing it exists to show."""
    _write_config("setup_completed: true\n")
    monkeypatch.setattr(webui, "_read_journal",
                        lambda unit, lines: ("werkzeug: something went wrong\n", True))
    r = _client().get("/logs/journal?problems=1")
    body = r.data.decode()
    assert "No warnings in this window" not in body
    assert "not the same as" in body
    assert "Turn the filter off" in body


# --- /api/health reports a device that has gone quiet ----------------------------
#
# The pull half of the quiet-device alert: an external poller sees both "up but
# quiet" here and "unreachable" when the poll itself fails, which together cover
# the case a push notification from the device can never report.

def _health_backup(monkeypatch, tmp_path, record=None):
    monkeypatch.setattr(webui, "LOG_DIR", str(tmp_path))
    if record is not None:
        (tmp_path / "last_backup.json").write_text(record)
    _write_config("setup_completed: true\n")
    r = _client().get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    return body["backup"], body["warnings"]


def test_health_reports_a_stale_backup_and_says_how_long(monkeypatch, tmp_path):
    import time as _time
    old = _time.time() - 9 * 86400
    backup, warnings = _health_backup(
        monkeypatch, tmp_path, json.dumps({"completed_at": old}))
    assert backup["stale"] is True
    assert backup["last_success_ts"] == old
    assert backup["last_success"] is not None
    assert backup["age_seconds"] >= 9 * 86400
    assert any("no backup in 9 days" in w for w in warnings)


def test_health_reports_a_fresh_backup_as_not_stale(monkeypatch, tmp_path):
    import time as _time
    backup, warnings = _health_backup(
        monkeypatch, tmp_path, json.dumps({"completed_at": _time.time() - 3600}))
    assert backup["stale"] is False
    assert backup["age_seconds"] < 7200
    assert not any("no backup in" in w for w in warnings)


def test_health_degrades_to_nulls_when_the_record_is_missing_or_corrupt(monkeypatch, tmp_path):
    # A missing or unreadable record must not turn the endpoint an external
    # monitor depends on into a 500.
    backup, _ = _health_backup(monkeypatch, tmp_path)
    assert backup["last_success"] is None
    assert backup["last_success_ts"] is None
    assert backup["age_seconds"] is None
    assert backup["stale"] is False

    backup, _ = _health_backup(monkeypatch, tmp_path, "{not json")
    assert backup["last_success"] is None
    assert backup["stale"] is False


# --- the sync time limit is settable, and blank means no limit ------------------
# It shipped as a file-only 3600 that nobody could change from the UI, and a
# 130 GB first sync aborted at 20% looking like a failure.

def _save_sync_settings(monkeypatch, form):
    _write_config("setup_completed: true\nsync:\n  enabled: true\n")
    saved = {}
    monkeypatch.setattr(webui, "save_config", lambda cfg: saved.update(cfg))
    data = {"action": "save_settings", "sync_enabled": "on"}
    data.update(form)
    _client().post("/settings/sync", data=data, follow_redirects=True)
    return saved


def test_the_sync_limit_is_stored_in_seconds(monkeypatch):
    saved = _save_sync_settings(monkeypatch, {"max_minutes": "480"})
    assert saved["sync"]["max_seconds"] == 480 * 60


def test_a_blank_sync_limit_means_no_limit(monkeypatch):
    saved = _save_sync_settings(monkeypatch, {"max_minutes": ""})
    assert saved["sync"]["max_seconds"] == 0


def test_a_junk_sync_limit_does_not_reinstate_a_cap(monkeypatch):
    """Anything unparseable reads as no cap. A surprise abort on a transfer that
    was working is worse than no bound - the stall watchdogs cover a real wedge."""
    for junk in ("soon", "-30", "abc"):
        saved = _save_sync_settings(monkeypatch, {"max_minutes": junk})
        assert saved["sync"]["max_seconds"] == 0, junk


def test_the_sync_limit_field_renders_the_saved_value(monkeypatch):
    _write_config("setup_completed: true\nsync:\n  enabled: true\n  max_seconds: 28800\n")
    body = _client().get("/settings/sync").data.decode()
    assert 'name="max_minutes"' in body
    assert 'value="480"' in body
