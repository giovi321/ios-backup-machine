"""Tests for logutil.prune_logs (per-run log retention) and
wg_manager.latest_handshake (WireGuard handshake-health parsing)."""
import os
import time

import logutil
import wg_manager


def _touch(path, age_seconds=0):
    open(path, "w").close()
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(path, (t, t))


def test_prune_keeps_newest_n_per_kind(tmp_path):
    d = str(tmp_path)
    for i in range(60):
        _touch(os.path.join(d, f"sync-{i:04d}.log"), age_seconds=i * 60)
        _touch(os.path.join(d, f"backup-{i:04d}.log"), age_seconds=i * 60)
    logutil.prune_logs(log_dir=d, keep_per_kind=50, max_age_days=0)  # age disabled
    kept = os.listdir(d)
    assert len([f for f in kept if f.startswith("sync-")]) == 50
    assert len([f for f in kept if f.startswith("backup-")]) == 50
    # The newest survive; the oldest are gone.
    assert "sync-0000.log" in kept
    assert "sync-0059.log" not in kept


def test_prune_drops_files_past_max_age(tmp_path):
    d = str(tmp_path)
    _touch(os.path.join(d, "sync-fresh.log"), age_seconds=0)
    _touch(os.path.join(d, "sync-stale.log"), age_seconds=100 * 86400)
    logutil.prune_logs(log_dir=d, keep_per_kind=50, max_age_days=90)
    kept = os.listdir(d)
    assert "sync-fresh.log" in kept
    assert "sync-stale.log" not in kept


def test_prune_leaves_non_per_run_logs_alone(tmp_path):
    d = str(tmp_path)
    for name in ("webui.log", "ntp-sync.log", "autostart.log", "update.log"):
        _touch(os.path.join(d, name), age_seconds=1000 * 86400)  # ancient
    logutil.prune_logs(log_dir=d, keep_per_kind=1, max_age_days=1)
    kept = set(os.listdir(d))
    assert kept == {"webui.log", "ntp-sync.log", "autostart.log", "update.log"}


def test_prune_never_raises_on_missing_dir():
    logutil.prune_logs(log_dir="/nonexistent/iosbackupmachine/logs")  # must not raise


class _FakeProc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_latest_handshake_parses_newest(monkeypatch):
    out = "pubkeyAAA\t1700000000\npubkeyBBB\t1700000500\n"
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, out))
    assert wg_manager.latest_handshake("wg0") == 1700000500


def test_latest_handshake_zero_when_never(monkeypatch):
    # wg prints a 0 epoch for a peer that has never handshaked.
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(0, "pubkeyAAA\t0\n"))
    assert wg_manager.latest_handshake("wg0") == 0


def test_latest_handshake_zero_on_error(monkeypatch):
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(1, ""))
    assert wg_manager.latest_handshake("wg0") == 0


# ---------------------------------------------------------------------------
# stamp_stream — the shell side of line timestamping
# ---------------------------------------------------------------------------
# update.log and the backup-sync output redirected into autostart.log are raw
# stdout from shell, so they cannot use TimestampedLog directly. They pipe
# through this instead, which reuses the same stamp so every log in the system
# reads identically.

def test_stamp_stream_prefixes_every_line():
    import io
    src = io.StringIO("first\nsecond\n")
    dst = io.StringIO()
    logutil.stamp_stream(src, dst)
    lines = dst.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("first")
    assert lines[1].endswith("second")


def test_stamp_stream_uses_the_same_format_as_the_run_log():
    import io, re
    dst = io.StringIO()
    logutil.stamp_stream(io.StringIO("x\n"), dst)
    assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] x$", dst.getvalue().rstrip())


def test_stamp_stream_stamps_a_final_line_without_a_newline():
    import io
    dst = io.StringIO()
    logutil.stamp_stream(io.StringIO("no trailing newline"), dst)
    assert dst.getvalue().rstrip().endswith("no trailing newline")
    assert dst.getvalue().startswith("[")


def test_stamp_stream_preserves_blank_lines_without_stamping_them():
    import io
    dst = io.StringIO()
    logutil.stamp_stream(io.StringIO("a\n\nb\n"), dst)
    lines = dst.getvalue().split("\n")
    assert lines[1] == ""          # blank stays blank, no lone timestamp
    assert lines[0].endswith("a")
    assert lines[2].endswith("b")


def test_stamp_stream_strips_ansi_colour_codes():
    """install.sh colours its output; the codes are noise in a log file."""
    import io
    dst = io.StringIO()
    logutil.stamp_stream(io.StringIO("\033[0;32m  done\033[0m\n"), dst)
    out = dst.getvalue().rstrip()
    assert out.endswith("done")
    assert "\033" not in out


def test_stamp_stream_survives_undecodable_input():
    import io
    dst = io.StringIO()
    logutil.stamp_stream(io.StringIO("ok\n"), dst)
    assert "ok" in dst.getvalue()
