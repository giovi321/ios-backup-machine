"""Tests for backup-sync.py's mutex guards: they must fail CLOSED.

A probe that cannot run (pgrep missing, status file unparseable) used to
return False, silently disabling the guard and letting overlapping
rsync/backup runs proceed. Now a probe failure means "assume busy, skip".

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
