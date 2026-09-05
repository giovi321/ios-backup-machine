"""Tests for the daemon-hardening pieces: loop watchdog, shutdown exit codes,
draw-failure fallback to headless, and the pre-backup gates.

iosbackupmachine.py is imported the same way test_display_wiring does it: real
module, with waveshare_epd / periphery stubbed only when they can't import on
this machine, against a throwaway config/runtime dir.
"""
import os
import subprocess
import sys
import tempfile
import types

import pytest

SANDBOX = tempfile.mkdtemp(prefix="ibm_hardening_")


def _load_daemon():
    os.makedirs(SANDBOX, exist_ok=True)
    cfg_path = os.path.join(SANDBOX, "config.yaml")
    with open(cfg_path, "w") as f:
        f.write("backup_dir: /tmp/b\nsync:\n  enabled: false\n")
    os.environ["IOSBACKUP_CONFIG"] = cfg_path
    os.environ["IOSBACKUP_RUNTIME_DIR"] = os.path.join(SANDBOX, "rt")
    os.environ["IOSBACKUP_LOG_DIR"] = os.path.join(SANDBOX, "log")
    os.makedirs(os.environ["IOSBACKUP_RUNTIME_DIR"], exist_ok=True)
    os.makedirs(os.environ["IOSBACKUP_LOG_DIR"], exist_ok=True)

    _stub_package("waveshare_epd", ["epd2in13_V4", "epdconfig"])
    _stub_package("periphery", ["gpio"],
                  attrs={"gpio": {"GPIOError": type("GPIOError", (Exception,), {})}})

    import iosbackupmachine
    return iosbackupmachine


def _stub_package(name, submodules, attrs=None):
    """Register a stand-in for `name` only if the real package cannot import."""
    try:
        __import__(name)
        return
    except Exception:
        pass
    pkg = types.ModuleType(name)
    for sub_name in submodules:
        sub = types.ModuleType(f"{name}.{sub_name}")
        for k, v in ((attrs or {}).get(sub_name) or {}).items():
            setattr(sub, k, v)
        sys.modules[f"{name}.{sub_name}"] = sub
        setattr(pkg, sub_name, sub)
    sys.modules[name] = pkg


try:
    ibm = _load_daemon()
except Exception as exc:  # pragma: no cover - environment without the runtime deps
    pytest.skip(f"display daemon not importable: {exc}", allow_module_level=True)


class FakePanel:
    def __init__(self):
        self.painted = []
        self.slept = False

    def draw(self, **kw):
        self.painted.append(kw.get("screen"))

    def draw_owner(self):
        self.painted.append("owner")

    def draw_updating(self):
        self.painted.append("updating")

    def sleep(self):
        self.slept = True


@pytest.fixture
def no_exit(monkeypatch):
    codes = []
    monkeypatch.setattr(ibm.os, "_exit", codes.append)
    return codes


# --- LoopWatchdog -------------------------------------------------------------

def test_watchdog_exits_1_when_the_loop_stalls():
    exits, logs = [], []
    t = [1000.0]
    wd = ibm.LoopWatchdog(stall_sec=10, exit_fn=exits.append,
                          time_fn=lambda: t[0], log_fn=logs.append)
    wd.beat()
    t[0] += 11
    assert wd.check() is True
    assert exits == [1]
    assert any("stalled" in m for m in logs)


def test_watchdog_stays_quiet_while_beats_arrive():
    exits = []
    t = [1000.0]
    wd = ibm.LoopWatchdog(stall_sec=10, exit_fn=exits.append,
                          time_fn=lambda: t[0], log_fn=lambda m: None)
    for _ in range(5):
        t[0] += 8
        wd.beat()
        assert wd.check() is False
    assert exits == []


def test_watchdog_check_is_exactly_at_threshold_not_before():
    exits = []
    t = [1000.0]
    wd = ibm.LoopWatchdog(stall_sec=10, exit_fn=exits.append,
                          time_fn=lambda: t[0], log_fn=lambda m: None)
    t[0] += 10          # exactly at the threshold: not yet stalled
    assert wd.check() is False
    t[0] += 0.1
    assert wd.check() is True
    assert exits == [1]


# --- shutdown exit-code plumbing ----------------------------------------------

def test_a_normal_shutdown_exits_zero(no_exit):
    panel = FakePanel()
    ibm.Animator(panel)._do_shutdown()
    assert no_exit == [0]
    assert panel.painted == ["owner"]
    assert panel.slept is True


def test_a_fatal_shutdown_exits_nonzero(no_exit):
    a = ibm.Animator(FakePanel())
    a.exit_code = 1
    a._do_shutdown()
    assert no_exit == [1]


def test_an_explicit_exit_code_wins_over_the_default(no_exit):
    ibm.Animator(FakePanel())._do_shutdown(exit_code=1)
    assert no_exit == [1]


def test_shutdown_terminates_a_running_backup_child(no_exit, monkeypatch):
    class FakeProc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    proc = FakeProc()
    monkeypatch.setattr(ibm, "_backup_proc", proc)
    try:
        ibm.Animator(FakePanel())._do_shutdown()
    finally:
        monkeypatch.setattr(ibm, "_backup_proc", None)
    assert proc.terminated is True


def test_shutdown_tolerates_an_already_dead_backup_child(no_exit, monkeypatch):
    class DeadProc:
        def poll(self):
            return 0

        def terminate(self):
            raise AssertionError("must not terminate an exited process")

    monkeypatch.setattr(ibm, "_backup_proc", DeadProc())
    try:
        ibm.Animator(FakePanel())._do_shutdown()
    finally:
        monkeypatch.setattr(ibm, "_backup_proc", None)
    assert no_exit == [0]


# --- draw-failure streak -> re-init -> headless -------------------------------

def test_repeated_draw_failures_reinit_the_panel(monkeypatch):
    fresh = FakePanel()
    calls = []

    class FakePanelCls:
        def __init__(self):
            calls.append(1)

        def prepare_partial(self):
            pass

    monkeypatch.setattr(ibm, "Panel", FakePanelCls)
    a = ibm.Animator(FakePanel())
    a._handle_draw_failure_streak()
    assert calls == [1]
    assert not isinstance(a.panel, ibm.NullPanel)


def test_a_failed_reinit_drops_to_headless_and_resets_the_streak(monkeypatch):
    class BoomPanel:
        def __init__(self):
            raise RuntimeError("SPI gone")

    monkeypatch.setattr(ibm, "Panel", BoomPanel)
    a = ibm.Animator(FakePanel())
    a._draw_failures = ibm.DRAW_FAIL_REINIT_AFTER
    a._handle_draw_failure_streak()
    assert isinstance(a.panel, ibm.NullPanel)
    assert a._draw_failures == 0
    # The Animator's interface must keep working on the shim.
    a.panel.draw(screen="normal", subtitle="x")
    a.panel.draw_owner()
    a.panel.sleep()


# --- headless -> periodic panel re-init ---------------------------------------

class _RecordingPanel(FakePanel):
    """A Panel stand-in that records what it was asked to draw."""

    def __init__(self):
        super().__init__()
        self.draws = []
        self.prepared = False

    def prepare_partial(self, base_img=None):
        self.prepared = True

    def draw(self, **kw):
        self.draws.append(kw)
        super().draw(**kw)


class _BoomPanel:
    def __init__(self):
        raise RuntimeError("SPI gone")


def _counting_panel(built):
    class CountingPanel(_RecordingPanel):
        def __init__(self):
            super().__init__()
            built.append(1)
    return CountingPanel


def _one_tick(monkeypatch, animator):
    """Run exactly one _tick iteration: no thread, no sleep. A fake SHUTDOWN
    that isn't set on entry but whose wait() returns True ends the loop through
    the normal shutdown path, which the no_exit fixture catches."""
    monkeypatch.setattr(ibm, "SHUTDOWN", types.SimpleNamespace(
        is_set=lambda: False, wait=lambda _t: True))
    animator.running = True
    animator._tick()


def test_a_healthy_panel_never_retries_the_init(monkeypatch):
    class NeverPanel:
        def __init__(self):
            raise AssertionError("must not re-init a healthy panel")

    monkeypatch.setattr(ibm, "Panel", NeverPanel)
    a = ibm.Animator(FakePanel())
    a._retry_panel_init(1000.0)
    assert a._panel_retry_at is None


def test_a_headless_start_defers_the_first_retry(monkeypatch):
    built = []
    monkeypatch.setattr(ibm, "Panel", _counting_panel(built))
    a = ibm.Animator(ibm.NullPanel())
    # main() drops to NullPanel before the Animator exists, so the first tick is
    # what arms the retry - it must not re-ask the question immediately.
    a._retry_panel_init(1000.0)
    assert built == []
    assert a._panel_retry_at == 1000.0 + ibm.PANEL_RETRY_MIN_SEC
    a._retry_panel_init(1000.0 + ibm.PANEL_RETRY_MIN_SEC - 1)
    assert built == []
    a._retry_panel_init(1000.0 + ibm.PANEL_RETRY_MIN_SEC)
    assert built == [1]


def test_a_due_retry_leaves_headless_and_forces_a_full_repaint(monkeypatch):
    monkeypatch.setattr(ibm, "Panel", _RecordingPanel)
    a = ibm.Animator(ibm.NullPanel())
    a._retry_panel_init(1000.0)
    a._retry_panel_init(1000.0 + ibm.PANEL_RETRY_MIN_SEC)
    assert not isinstance(a.panel, ibm.NullPanel)
    assert a.panel.prepared is True
    assert a._force_full is True
    assert a._last is None
    assert a._last_layout is None
    assert a._panel_retry_at is None


def test_failed_retries_escalate_and_cap(monkeypatch):
    monkeypatch.setattr(ibm, "Panel", _BoomPanel)
    a = ibm.Animator(ibm.NullPanel())
    a._retry_panel_init(1000.0)
    gaps = [a._panel_retry_at - 1000.0]
    for _ in range(7):
        now = a._panel_retry_at
        a._retry_panel_init(now)
        gaps.append(a._panel_retry_at - now)
    expected, g = [ibm.PANEL_RETRY_MIN_SEC], ibm.PANEL_RETRY_MIN_SEC
    for _ in range(7):
        g = min(g * 2, ibm.PANEL_RETRY_MAX_SEC)
        expected.append(g)
    assert gaps == expected
    assert isinstance(a.panel, ibm.NullPanel)


def test_a_permanently_absent_panel_logs_once(monkeypatch, capsys):
    monkeypatch.setattr(ibm, "Panel", _BoomPanel)
    a = ibm.Animator(ibm.NullPanel())
    a._retry_panel_init(1000.0)
    for _ in range(8):
        a._retry_panel_init(a._panel_retry_at)
    out = capsys.readouterr().out
    # A unit built with no panel gets one line for the whole run, not one per try.
    assert out.count("[DRAW] display still unavailable") == 1


def test_a_changed_failure_is_logged_again(monkeypatch, capsys):
    tries = []

    class FlakyPanel:
        def __init__(self):
            tries.append(1)
            raise RuntimeError(("SPI gone", "display stuck busy")[len(tries) % 2])

    monkeypatch.setattr(ibm, "Panel", FlakyPanel)
    a = ibm.Animator(ibm.NullPanel())
    a._retry_panel_init(1000.0)
    a._retry_panel_init(a._panel_retry_at)
    a._retry_panel_init(a._panel_retry_at)
    out = capsys.readouterr().out
    assert out.count("[DRAW] display still unavailable") == 2


def test_the_streak_fallback_leaves_the_retry_armed(monkeypatch):
    monkeypatch.setattr(ibm, "Panel", _BoomPanel)
    a = ibm.Animator(FakePanel())
    a._handle_draw_failure_streak()
    assert isinstance(a.panel, ibm.NullPanel)
    # Same mechanism covers the streak's door: it sets no flag, the next tick
    # just sees a NullPanel.
    built = []
    monkeypatch.setattr(ibm, "Panel", _counting_panel(built))
    a._retry_panel_init(2000.0)
    assert built == []
    a._retry_panel_init(2000.0 + ibm.PANEL_RETRY_MIN_SEC)
    assert built == [1]


def test_a_null_panel_draw_does_not_reset_the_retry_gap(monkeypatch, no_exit):
    monkeypatch.setattr(ibm, "Panel", _BoomPanel)
    a = ibm.Animator(ibm.NullPanel())
    a._panel_retry_gap = 480
    a._panel_retry_at = 1e12          # not due: this tick only draws
    a.set(screen="sync", animate=True, percent=10)
    _one_tick(monkeypatch, a)
    # An animated screen draws every tick, so resetting on any successful draw
    # would pin the gap at the minimum and disable the rate limit entirely.
    assert a._panel_retry_gap == 480


def test_a_real_draw_resets_the_retry_gap(monkeypatch, no_exit):
    a = ibm.Animator(FakePanel())
    a._panel_retry_gap = 480
    _one_tick(monkeypatch, a)
    assert a._panel_retry_gap == ibm.PANEL_RETRY_MIN_SEC


def test_a_tick_retries_and_repaints_a_recovered_panel(monkeypatch, no_exit):
    monkeypatch.setattr(ibm, "Panel", _RecordingPanel)
    a = ibm.Animator(ibm.NullPanel())
    a._panel_retry_at = 0.0           # due on the next tick
    _one_tick(monkeypatch, a)
    assert isinstance(a.panel, _RecordingPanel)
    # The retry runs before the draw block, so the recovered panel paints the
    # current state in the same tick instead of waiting for a state change.
    assert len(a.panel.draws) == 1
    assert a.panel.draws[0]["full"] is True


def test_a_settled_screen_still_gets_its_retry(monkeypatch, no_exit):
    monkeypatch.setattr(ibm, "Panel", _RecordingPanel)
    a = ibm.Animator(ibm.NullPanel())
    a.set(screen="normal", subtitle="Ready", animate=False)
    _one_tick(monkeypatch, a)         # draws once, settles _last, arms the retry
    # An idle "normal" screen (or "complete" after a sync) holds identical state
    # for minutes or hours: unchanged state, no animation, no pending full
    # refresh, so the redraw branch is skipped on every tick.
    assert a._force_full is False
    assert a._last == a.state
    a._panel_retry_at = 0.0           # due on the next tick
    _one_tick(monkeypatch, a)
    # The retry has to run per tick, not per redraw: folded into the redraw
    # branch it would never run on a settled screen and the panel that came back
    # would stay dark for the life of the process.
    assert isinstance(a.panel, _RecordingPanel)
    assert a.panel.draws[0]["full"] is True


# --- disk-space gate -----------------------------------------------------------

class _VFS:
    def __init__(self, free_mb):
        self.f_frsize = 4096
        self.f_bavail = (free_mb * 1024 * 1024) // 4096


def _statvfs_for(root_mb, backup_mb):
    def fn(path):
        return _VFS(root_mb if path == "/" else backup_mb)
    return fn


def test_disk_space_ok_when_both_filesystems_have_room():
    assert ibm._disk_space_problems("/b", statvfs_fn=_statvfs_for(2000, 5000)) == []


def test_low_root_blocks_the_backup():
    problems = ibm._disk_space_problems("/b", statvfs_fn=_statvfs_for(199, 5000))
    assert len(problems) == 1
    assert "Root disk" in problems[0]


def test_low_backup_drive_blocks_the_backup():
    problems = ibm._disk_space_problems("/b", statvfs_fn=_statvfs_for(2000, 499))
    assert len(problems) == 1
    assert "Backup drive" in problems[0]


def test_unreadable_filesystems_fail_open():
    def boom(path):
        raise OSError("nope")
    assert ibm._disk_space_problems("/b", statvfs_fn=boom) == []


class RecordingUI:
    def __init__(self):
        self.last = None

    def set(self, **kw):
        self.last = kw


def test_the_gate_aborts_and_explains(monkeypatch):
    statuses = []
    monkeypatch.setattr(ibm, "write_status",
                        lambda state, **kw: statuses.append((state, kw.get("message"))))
    monkeypatch.setattr(ibm.time, "sleep", lambda s: None)
    monkeypatch.setattr(ibm.os, "statvfs", _statvfs_for(2000, 100), raising=False)
    ui = RecordingUI()
    assert ibm.check_disk_space(None, ui) is False
    assert statuses and statuses[0][0] == "error"
    assert "Not enough disk space" in statuses[0][1]
    assert "Not enough disk space" in ui.last["subtitle"]


def test_the_gate_passes_with_room(monkeypatch):
    monkeypatch.setattr(ibm.os, "statvfs", _statvfs_for(2000, 5000), raising=False)
    assert ibm.check_disk_space(None, RecordingUI()) is True


# --- backup-dir writable probe -------------------------------------------------

def test_writable_probe_accepts_a_writable_dir(tmp_path):
    assert ibm._backup_dir_writable(str(tmp_path)) is True
    # ... and leaves nothing behind
    assert [p for p in os.listdir(tmp_path) if p.startswith(".wprobe-")] == []


def test_writable_probe_rejects_a_missing_dir(tmp_path):
    assert ibm._backup_dir_writable(str(tmp_path / "nope")) is False


def test_writable_probe_rejects_a_readonly_dir(tmp_path):
    if os.name == "nt":
        pytest.skip("chmod-based read-only dirs are not enforceable on Windows")
    os.chmod(tmp_path, 0o555)
    try:
        assert ibm._backup_dir_writable(str(tmp_path)) is False
    finally:
        os.chmod(tmp_path, 0o755)


def test_mount_check_fails_on_a_readonly_backup_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ibm, "_backup_dir_writable", lambda p: False)
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(tmp_path), "marker_file": ".foldermarker"})
    (tmp_path / ".foldermarker").write_text("x")
    statuses = []
    monkeypatch.setattr(ibm, "write_status",
                        lambda state, **kw: statuses.append((state, kw.get("message"))))
    ui = RecordingUI()
    assert ibm.check_backup_mount(None, ui) is False
    assert statuses[0][0] == "error"
    assert "read-only" in statuses[0][1]


# --- get_connected_udids failure modes ----------------------------------------

def test_a_hung_idevice_id_reads_as_no_device(monkeypatch):
    def boom(*a, **kw):
        assert kw.get("timeout") == 10
        raise subprocess.TimeoutExpired(cmd="idevice_id", timeout=10)
    monkeypatch.setattr(ibm.subprocess, "run", boom)
    assert ibm.get_connected_udids() == []


def test_a_missing_idevice_id_reads_as_no_device(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("idevice_id")
    monkeypatch.setattr(ibm.subprocess, "run", boom)
    assert ibm.get_connected_udids() == []


def test_connected_udids_are_parsed(monkeypatch):
    class R:
        stdout = "  abcd1234 \nefgh5678\n"
    monkeypatch.setattr(ibm.subprocess, "run", lambda *a, **kw: R())
    assert ibm.get_connected_udids() == ["abcd1234", "efgh5678"]


# --- backup watchdog limits ----------------------------------------------------

def test_watchdog_limits_default(monkeypatch):
    monkeypatch.setattr(ibm, "CFG", {})
    assert ibm._backup_watchdog_limits() == (600, 4 * 3600)


def test_watchdog_limits_from_config(monkeypatch):
    monkeypatch.setattr(ibm, "CFG", {"backup": {"hang_timeout_sec": 120, "max_duration_sec": 900}})
    assert ibm._backup_watchdog_limits() == (120, 900)


def test_watchdog_limits_survive_junk_config(monkeypatch):
    monkeypatch.setattr(ibm, "CFG", {"backup": "not-a-dict"})
    assert ibm._backup_watchdog_limits() == (600, 4 * 3600)
    monkeypatch.setattr(ibm, "CFG", {"backup": {"hang_timeout_sec": "junk"}})
    assert ibm._backup_watchdog_limits() == (600, 4 * 3600)


# --- tee_and_parse watchdog (real subprocess, POSIX pipes only) ----------------

@pytest.mark.skipif(os.name == "nt", reason="select() on pipe fds is POSIX-only")
def test_a_silent_backup_is_terminated():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    outcome = ibm.tee_and_parse(proc, None, lambda tok: None, silence_timeout=1)
    assert outcome == "silent"
    assert proc.poll() is not None   # terminated, not left running


@pytest.mark.skipif(os.name == "nt", reason="select() on pipe fds is POSIX-only")
def test_normal_output_streams_to_the_parser():
    proc = subprocess.Popen([sys.executable, "-c", "print('hello')"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tokens = []
    outcome = ibm.tee_and_parse(proc, None, tokens.append, silence_timeout=10)
    assert outcome == "eof"
    assert "".join(t for t in tokens if t != "__LINE_BREAK__").startswith("hello")


@pytest.mark.skipif(os.name == "nt", reason="select() on pipe fds is POSIX-only")
def test_a_vanished_device_stops_a_running_backup():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys,time\nprint('x'); sys.stdout.flush(); time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    outcome = ibm.tee_and_parse(proc, None, lambda tok: None, silence_timeout=60,
                                device_gone=lambda: True, device_poll_sec=0.1)
    assert outcome == "unplugged"
    assert proc.poll() is not None
