"""Tests for backup-sync.py's mutex guards: they must fail CLOSED.

A probe that cannot run (pgrep missing, status file unparseable) used to
return False, silently disabling the guard and letting overlapping
rsync/backup runs proceed. Now a probe failure means "assume busy, skip",
and every probe is bounded so it can actually reach that verdict.

The second property tested here is that a guard which skips writes nothing to
backup_status.json: the run being yielded to owns that file, and a skipped
launch overwriting it took the live run's progress away from every consumer.

The script is not importable by name (dash in the filename), so it is loaded
via importlib; its executable flow lives in main(), so importing is side-effect
free.
"""
import importlib.util
import json
import os
import types

import pytest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))


@pytest.fixture
def bs():
    spec = importlib.util.spec_from_file_location(
        "backup_sync", os.path.join(APP_DIR, "backup-sync.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Proc:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _boom(*a, **k):
    raise OSError("pgrep is gone")


# ---- backup_running --------------------------------------------------------

def test_backup_running_true_on_probe_failure(bs, monkeypatch):
    monkeypatch.setattr(bs, "subprocess", types.SimpleNamespace(run=_boom))
    assert bs.backup_running() is True


def test_backup_running_reflects_pgrep(bs, monkeypatch):
    monkeypatch.setattr(bs, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: _Proc(0)))
    assert bs.backup_running() is True
    monkeypatch.setattr(bs, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: _Proc(1)))
    assert bs.backup_running() is False


# ---- another_sync_running ---------------------------------------------------

def test_another_sync_running_true_on_probe_failure(bs, monkeypatch):
    monkeypatch.setattr(bs, "subprocess", types.SimpleNamespace(run=_boom))
    assert bs.another_sync_running() is True


def test_another_sync_running_false_when_only_self_matches(bs, monkeypatch):
    me = os.getpid()
    monkeypatch.setattr(bs, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: _Proc(0, f"{me}\n")))
    assert bs.another_sync_running() is False


def test_another_sync_running_true_when_a_stranger_matches(bs, monkeypatch):
    monkeypatch.setattr(bs, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: _Proc(0, "999999\n")))
    assert bs.another_sync_running() is True


def test_another_sync_running_false_when_pgrep_finds_nothing(bs, monkeypatch):
    monkeypatch.setattr(bs, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: _Proc(1, "")))
    assert bs.another_sync_running() is False


# ---- sync_in_progress -------------------------------------------------------

def test_sync_in_progress_false_without_a_status_file(bs, monkeypatch, tmp_path):
    # A missing status file is the normal "nothing has ever synced" case, not
    # a probe failure — it must NOT fail closed.
    monkeypatch.setattr(bs, "STATUS_FILE", str(tmp_path / "absent.json"))
    assert bs.sync_in_progress() is False


def test_sync_in_progress_true_on_unparseable_status(bs, monkeypatch, tmp_path):
    f = tmp_path / "backup_status.json"
    f.write_text("{torn write")
    monkeypatch.setattr(bs, "STATUS_FILE", str(f))
    assert bs.sync_in_progress() is True


def test_sync_in_progress_false_when_state_is_not_syncing(bs, monkeypatch, tmp_path):
    f = tmp_path / "backup_status.json"
    f.write_text(json.dumps({"state": "sync_complete"}))
    monkeypatch.setattr(bs, "STATUS_FILE", str(f))
    assert bs.sync_in_progress() is False


def test_sync_in_progress_true_when_rsync_probe_fails(bs, monkeypatch, tmp_path):
    f = tmp_path / "backup_status.json"
    f.write_text(json.dumps({"state": "syncing"}))
    monkeypatch.setattr(bs, "STATUS_FILE", str(f))
    monkeypatch.setattr(bs, "subprocess", types.SimpleNamespace(run=_boom))
    assert bs.sync_in_progress() is True


def test_sync_in_progress_reflects_rsync_pgrep(bs, monkeypatch, tmp_path):
    f = tmp_path / "backup_status.json"
    f.write_text(json.dumps({"state": "syncing"}))
    monkeypatch.setattr(bs, "STATUS_FILE", str(f))
    monkeypatch.setattr(bs, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: _Proc(0)))
    assert bs.sync_in_progress() is True
    monkeypatch.setattr(bs, "subprocess",
                        types.SimpleNamespace(run=lambda *a, **k: _Proc(1)))
    assert bs.sync_in_progress() is False


# ---- the probe failure is logged --------------------------------------------

def test_probe_failure_goes_to_stderr_and_the_run_log(bs, monkeypatch, capsys):
    written = []
    bs.logf = types.SimpleNamespace(write=written.append)
    try:
        monkeypatch.setattr(bs, "subprocess", types.SimpleNamespace(run=_boom))
        assert bs.backup_running() is True
    finally:
        bs.logf = None
    assert "assuming busy" in capsys.readouterr().err
    assert any("assuming busy" in line for line in written)


# ---- every probe subprocess call is bounded ---------------------------------

def test_every_probe_subprocess_call_passes_a_timeout(bs, monkeypatch, tmp_path):
    # Failing closed is only useful if the probe returns. An unbounded pgrep or
    # pkill parks the run in the guard stage forever instead of skipping.
    calls = []

    def _record(cmd, **kw):
        calls.append((cmd, kw))
        return _Proc(1, "")

    f = tmp_path / "backup_status.json"
    f.write_text(json.dumps({"state": "syncing"}))   # so sync_in_progress() reaches pgrep
    monkeypatch.setattr(bs, "STATUS_FILE", str(f))
    monkeypatch.setattr(bs, "subprocess", types.SimpleNamespace(run=_record))

    bs.backup_running()
    bs.another_sync_running()
    bs.sync_in_progress()
    bs.kill_stale_rsync(types.SimpleNamespace(write=lambda m: None))

    assert len(calls) == 4
    for cmd, kw in calls:
        assert kw.get("timeout") == 5, f"{cmd} runs unbounded"


# ---- the guards must not touch the status file ------------------------------

def _stage_main(bs, monkeypatch, tmp_path):
    """Redirect main()'s config, run log and status file into tmp_path and seed
    the status file with a live owner's payload. Returns the status file."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sync:\n  enabled: true\n")
    monkeypatch.setattr(bs, "CONFIG_PATH", str(cfg))

    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(bs, "LOG_DIR", str(logs))
    monkeypatch.setattr(bs.logutil, "LOG_DIR", str(logs))   # prune_logs() reads this

    runtime = tmp_path / "run"
    runtime.mkdir()
    status = runtime / "backup_status.json"
    status.write_text(json.dumps({"state": "syncing", "percent": 42, "bytes": 1000,
                                  "total": 5000, "speed": "3MB/s"}))
    monkeypatch.setattr(bs, "RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(bs, "STATUS_FILE", str(status))

    # main() hands notifications a logger closed over a run log it then closes.
    # Register it with monkeypatch so the hook does not leak into later tests.
    import notifications
    monkeypatch.setattr(notifications, "_log_hook", notifications._log_hook)
    return status


def _run_log(tmp_path):
    logs = sorted((tmp_path / "logs").glob("sync-*.log"))
    assert logs, "main() opened no run log"
    return logs[-1].read_text()


def test_another_sync_guard_leaves_the_status_file_untouched(bs, monkeypatch, tmp_path):
    status = _stage_main(bs, monkeypatch, tmp_path)
    before = status.read_bytes()
    monkeypatch.setattr(bs, "another_sync_running", lambda: True)

    with pytest.raises(SystemExit) as e:
        bs.main()

    assert e.value.code == 0
    assert status.read_bytes() == before          # the owner's progress survives
    assert "sync_skipped" not in status.read_text()
    assert "[SKIP] another backup-sync.py is already running" in _run_log(tmp_path)


def test_sync_in_progress_guard_leaves_the_status_file_untouched(bs, monkeypatch, tmp_path):
    status = _stage_main(bs, monkeypatch, tmp_path)
    before = status.read_bytes()
    monkeypatch.setattr(bs, "another_sync_running", lambda: False)
    monkeypatch.setattr(bs, "backup_running", lambda: False)
    monkeypatch.setattr(bs, "sync_in_progress", lambda: True)

    with pytest.raises(SystemExit) as e:
        bs.main()

    assert e.value.code == 0
    assert status.read_bytes() == before
    assert "sync_skipped" not in status.read_text()
    assert "[SKIP] a sync is already in progress" in _run_log(tmp_path)


def test_backup_guard_leaves_the_backups_status_untouched(bs, monkeypatch, tmp_path):
    # A backup owns the same file. Overwriting "backing_up" blanked the
    # dashboard's backup card and disabled Stop Backup while it ran, and
    # idevicebackup2 can sit on one percent for minutes before rewriting it.
    status = _stage_main(bs, monkeypatch, tmp_path)
    status.write_text(json.dumps({"state": "backing_up", "percent": 17}))
    before = status.read_bytes()
    monkeypatch.setattr(bs, "another_sync_running", lambda: False)
    monkeypatch.setattr(bs, "backup_running", lambda: True)

    with pytest.raises(SystemExit) as e:
        bs.main()

    assert e.value.code == 0
    assert status.read_bytes() == before
    assert "[SKIP] backup (idevicebackup2) in progress" in _run_log(tmp_path)


def test_a_broken_backup_probe_reports_the_probe_not_a_backup(bs, monkeypatch, tmp_path):
    # Fail-closed still skips the sync, but nothing observed a backup, so the
    # run must not leave "Backup in progress" behind as the reason.
    status = _stage_main(bs, monkeypatch, tmp_path)
    status.unlink()                       # nobody owns the slot
    monkeypatch.setattr(bs, "another_sync_running", lambda: False)
    monkeypatch.setattr(bs, "subprocess", types.SimpleNamespace(run=_boom))

    with pytest.raises(SystemExit) as e:
        bs.main()

    assert e.value.code == 0
    assert not status.exists()            # no clobber, no invented state
    log = _run_log(tmp_path)
    assert "backup_running probe failed" in log
    assert "assuming busy" in log
