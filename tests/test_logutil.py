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


def test_latest_handshake_none_on_error(monkeypatch):
    # A failed `wg` call means the state is unknown — None, not 0 ("no handshake").
    monkeypatch.setattr(wg_manager.subprocess, "run",
                        lambda *a, **k: _FakeProc(1, ""))
    assert wg_manager.latest_handshake("wg0") is None


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


# ---------------------------------------------------------------------------
# TimestampedLog.write — a failing disk must never reach the caller
# ---------------------------------------------------------------------------
# iosbackupmachine.py writes log lines unguarded all over, including from
# inside its own fatal handler, so an ENOSPC/EROFS from the file must be
# swallowed here. The first failure is printed to stderr (the journal) so the
# disk problem is still visible somewhere.

class _BoomFile:
    def __init__(self, fail=True):
        self.calls = 0
        self.fail = fail

    def write(self, s):
        self.calls += 1
        if self.fail:
            raise OSError(28, "No space left on device")

    def flush(self):
        pass

    def close(self):
        pass


def test_write_never_propagates_oserror(capsys):
    log = logutil.TimestampedLog(_BoomFile())
    for _ in range(3):
        log.write("line\n")                      # must not raise
    assert "[logutil] log write failed" in capsys.readouterr().err


def test_write_reports_only_the_first_failure_of_a_streak(capsys):
    log = logutil.TimestampedLog(_BoomFile())
    for _ in range(3):
        log.write("line\n")
    assert capsys.readouterr().err.count("[logutil]") == 1


def test_write_stops_hitting_the_disk_after_repeated_failures():
    fh = _BoomFile()
    log = logutil.TimestampedLog(fh)
    for _ in range(20):
        log.write("line\n")
    assert fh.calls == 10                        # then the log goes dead


def test_write_recovers_when_the_disk_does(capsys):
    fh = _BoomFile()
    log = logutil.TimestampedLog(fh)
    log.write("dropped\n")
    fh.fail = False
    log.write("back\n")
    assert fh.calls == 2                         # kept trying after one failure


def test_close_on_a_dead_log_still_does_not_raise():
    fh = _BoomFile()
    log = logutil.TimestampedLog(fh)
    log.write("partial line without newline")
    log.close()                                  # must not raise


# ---------------------------------------------------------------------------
# prune_logs — aggregate size cap per kind
# ---------------------------------------------------------------------------

def _sized(path, size, age_seconds=0):
    with open(path, "w") as f:
        f.write("x" * size)
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(path, (t, t))


def test_prune_size_cap_deletes_oldest_until_under_cap(tmp_path):
    d = str(tmp_path)
    for i in range(5):                           # sync-0 oldest ... sync-4 newest
        _sized(os.path.join(d, f"sync-{i}.log"), 1000, age_seconds=(5 - i) * 60)
    logutil.prune_logs(log_dir=d, keep_per_kind=50, max_age_days=0,
                       max_bytes_per_kind=2500)
    kept = sorted(os.listdir(d))
    # 5 x 1000 B: deleting oldest first — 4000, 3000 still over — leaves 2000 B.
    assert kept == ["sync-3.log", "sync-4.log"]


def test_prune_size_cap_is_per_kind(tmp_path):
    d = str(tmp_path)
    _sized(os.path.join(d, "sync-a.log"), 1000, age_seconds=60)
    _sized(os.path.join(d, "backup-a.log"), 1000, age_seconds=60)
    logutil.prune_logs(log_dir=d, keep_per_kind=50, max_age_days=0,
                       max_bytes_per_kind=100)
    # Each kind is over the cap on its own and loses its oldest (only) file.
    assert os.listdir(d) == []


def test_prune_size_cap_leaves_logs_under_the_cap_alone(tmp_path):
    d = str(tmp_path)
    _sized(os.path.join(d, "sync-a.log"), 100)
    _sized(os.path.join(d, "sync-b.log"), 100)
    logutil.prune_logs(log_dir=d, keep_per_kind=50, max_age_days=0,
                       max_bytes_per_kind=1024)
    assert sorted(os.listdir(d)) == ["sync-a.log", "sync-b.log"]


def test_prune_default_cap_is_100mb():
    assert logutil.LOG_MAX_BYTES_PER_KIND == 100 * 1024 * 1024
