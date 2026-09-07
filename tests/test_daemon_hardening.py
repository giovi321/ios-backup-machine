"""Tests for the daemon-hardening pieces: loop watchdog, shutdown exit codes,
draw-failure fallback to headless, and the pre-backup gates.

iosbackupmachine.py is imported the same way test_display_wiring does it: real
module, with waveshare_epd / periphery stubbed only when they can't import on
this machine, against a throwaway config/runtime dir.
"""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
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


class _Notes:
    """Collects send_notification calls as (event, payload)."""

    def __init__(self):
        self.sent = []

    def __call__(self, event, payload=None):
        self.sent.append((event, payload or {}))

    def codes(self, event):
        return [pl.get("reason_code") for ev, pl in self.sent if ev == event]


def test_the_backup_floor_follows_the_configured_reserve():
    # The reserve the in-run probe enforces also raises the start floor, so a run
    # can never pass the gate and then abort on its own first health poll.
    assert ibm._disk_space_problems("/b", statvfs_fn=_statvfs_for(2000, 2000)) == []
    problems = ibm._disk_space_problems("/b", statvfs_fn=_statvfs_for(2000, 2000),
                                        backup_min_mb=3000)
    assert len(problems) == 1
    assert "3000MB" in problems[0]


def test_a_reserve_of_zero_leaves_the_root_backstop_live():
    assert ibm._disk_space_problems("/b", statvfs_fn=_statvfs_for(2000, 0),
                                    backup_min_mb=0) == []
    problems = ibm._disk_space_problems("/b", statvfs_fn=_statvfs_for(100, 0),
                                        backup_min_mb=0)
    assert len(problems) == 1
    assert "Root disk" in problems[0]


def test_the_disk_gate_reads_the_configured_reserve(monkeypatch):
    monkeypatch.setattr(ibm, "write_status", lambda state, **kw: None)
    monkeypatch.setattr(ibm, "send_notification", _Notes())
    monkeypatch.setattr(ibm.time, "sleep", lambda s: None)
    monkeypatch.setattr(ibm.os, "statvfs", _statvfs_for(2000, 2000), raising=False)
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": "/b", "backup": {"min_free_mb": 3000}})
    assert ibm.check_disk_space(None, RecordingUI()) is False
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": "/b", "backup": {}})
    assert ibm.check_disk_space(None, RecordingUI()) is True


def test_a_refused_backup_tells_someone(monkeypatch):
    # run_backup returns 2 and the main loop discards it, so a gate that only
    # writes to the panel is invisible to an owner who is not standing there.
    notes = _Notes()
    monkeypatch.setattr(ibm, "write_status", lambda state, **kw: None)
    monkeypatch.setattr(ibm, "send_notification", notes)
    monkeypatch.setattr(ibm.time, "sleep", lambda s: None)
    monkeypatch.setattr(ibm.os, "statvfs", _statvfs_for(2000, 100), raising=False)
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": "/b", "backup": {}})
    assert ibm.check_disk_space(None, RecordingUI()) is False
    assert notes.codes("backup_error") == ["disk_full"]


# --- battery gate --------------------------------------------------------------

def _battery(monkeypatch, percent, charging=False):
    import power
    monkeypatch.setattr(power, "get_battery",
                        lambda **kw: {"percent": percent, "charging": charging})


def test_a_flat_battery_refuses_the_backup(monkeypatch):
    statuses = []
    notes = _Notes()
    monkeypatch.setattr(ibm, "write_status",
                        lambda state, **kw: statuses.append((state, kw)))
    monkeypatch.setattr(ibm, "send_notification", notes)
    monkeypatch.setattr(ibm.time, "sleep", lambda s: None)
    monkeypatch.setattr(ibm, "CFG", {"backup": {"min_battery_percent": 35}})
    _battery(monkeypatch, 20.0)
    ui = RecordingUI()
    assert ibm.check_battery(None, ui) is False
    assert statuses[0][0] == "error"
    assert "20" in statuses[0][1]["message"]
    assert statuses[0][1]["reason_code"] == "battery_low"
    assert "20" in ui.last["subtitle"]
    assert "Charge" in ui.last["subtitle"]        # names the fix, not just the fault
    assert notes.codes("backup_error") == ["battery_low"]


def test_the_battery_gate_fails_open(monkeypatch):
    monkeypatch.setattr(ibm, "write_status", lambda state, **kw: None)
    monkeypatch.setattr(ibm, "send_notification", _Notes())
    monkeypatch.setattr(ibm.time, "sleep", lambda s: None)
    monkeypatch.setattr(ibm, "CFG", {"backup": {"min_battery_percent": 35}})
    _battery(monkeypatch, 10.0, charging=True)
    assert ibm.check_battery(None, RecordingUI()) is True     # charging
    _battery(monkeypatch, None)
    assert ibm.check_battery(None, RecordingUI()) is True     # unreadable pack
    _battery(monkeypatch, 80.0)
    assert ibm.check_battery(None, RecordingUI()) is True     # above the floor
    monkeypatch.setattr(ibm, "CFG", {"backup": {"min_battery_percent": 0}})
    _battery(monkeypatch, 5.0)
    assert ibm.check_battery(None, RecordingUI()) is True     # explicitly disabled


def test_the_battery_gate_survives_a_missing_power_module(monkeypatch):
    monkeypatch.setattr(ibm, "CFG", {"backup": {"min_battery_percent": 35}})
    monkeypatch.setitem(sys.modules, "power", None)   # makes `import power` raise
    assert ibm.check_battery(None, RecordingUI()) is True


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


def test_a_missing_mount_tells_someone(monkeypatch, tmp_path):
    notes = _Notes()
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(tmp_path), "marker_file": ".foldermarker"})
    monkeypatch.setattr(ibm, "write_status", lambda state, **kw: None)
    monkeypatch.setattr(ibm, "send_notification", notes)
    assert ibm.check_backup_mount(None, RecordingUI()) is False   # no marker written
    assert notes.codes("backup_error") == ["mount_lost"]


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


# --- backup safety limits ------------------------------------------------------

def test_safety_limits_default(monkeypatch):
    monkeypatch.setattr(ibm, "CFG", {})
    assert ibm._backup_safety_limits() == (35, 512)


def test_safety_limits_from_config(monkeypatch):
    monkeypatch.setattr(ibm, "CFG",
                        {"backup": {"min_battery_percent": 20, "min_free_mb": 1024}})
    assert ibm._backup_safety_limits() == (20, 1024)


def test_safety_limits_survive_junk_config(monkeypatch):
    monkeypatch.setattr(ibm, "CFG", {"backup": "not-a-dict"})
    assert ibm._backup_safety_limits() == (35, 512)
    monkeypatch.setattr(ibm, "CFG", {"backup": {"min_battery_percent": "junk"}})
    assert ibm._backup_safety_limits() == (35, 512)
    monkeypatch.setattr(ibm, "CFG", {"backup": {"min_free_mb": None}})
    assert ibm._backup_safety_limits() == (35, 512)


def test_a_battery_floor_can_never_refuse_every_backup(monkeypatch):
    # Clamped, not rejected: 95% would be a guard that permanently wedges the
    # appliance, which is the one thing none of these may do.
    monkeypatch.setattr(ibm, "CFG", {"backup": {"min_battery_percent": 95,
                                                "min_free_mb": -5}})
    assert ibm._backup_safety_limits() == (90, 0)


# --- backup staleness alert ------------------------------------------------------
#
# The one check in the daemon that fires because NOTHING happened. Everything here
# is pure file/dict work with an injected clock, so nothing sleeps or needs a board.

_STALE_NOW = 1800000000.0        # 2027-01-15 UTC, comfortably past STALE_CLOCK_FLOOR
_WEEK = 7 * 24 * 3600


def _record(tmp_path, **fields):
    (tmp_path / "last_backup.json").write_text(json.dumps(fields))


def _stale_env(monkeypatch, tmp_path, *, config="", running=False, updating=False,
               boot_time=0.0, reason="no_device"):
    """Point the watcher at a throwaway record + config and hand it a recorder.

    boot_time defaults to 0.0, which is what the real _boot_time() returns when
    /proc/uptime cannot be read - every injected clock then looks post-boot, so
    the boot grace is out of the way unless a test is about it."""
    monkeypatch.setattr(ibm, "LAST_BACKUP_FILE", str(tmp_path / "last_backup.json"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config)
    monkeypatch.setattr(ibm, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(ibm, "_backup_running", running)
    monkeypatch.setattr(ibm, "_updating_requested", lambda: updating)
    monkeypatch.setattr(ibm, "_boot_time", lambda: boot_time)
    monkeypatch.setattr(ibm, "_last_device_reason", reason)
    monkeypatch.setattr(ibm, "_stale_alerted_in_process", False)
    notes = _Notes()
    monkeypatch.setattr(ibm, "send_notification", notes)
    return notes


def test_a_backup_inside_the_threshold_is_not_stale():
    alert, age, ever, _ = ibm._stale_verdict(
        {"completed_at": _STALE_NOW - 3 * 86400}, _STALE_NOW, _WEEK)
    assert alert is False
    assert round(age) == 3 * 86400
    assert ever is True


def test_a_backup_past_the_threshold_is_stale():
    ts = _STALE_NOW - 9 * 86400
    alert, age, ever, key = ibm._stale_verdict({"completed_at": ts}, _STALE_NOW, _WEEK)
    assert alert is True
    assert round(age) == 9 * 86400
    assert ever is True
    assert key == ts


def test_a_device_that_never_completed_a_backup_alerts_from_first_seen():
    ts = _STALE_NOW - 30 * 86400
    alert, _, ever, key = ibm._stale_verdict(
        {"completed_at": None, "first_seen": ts}, _STALE_NOW, _WEEK)
    assert alert is True
    assert ever is False          # so the message can say no backup has EVER finished
    assert key == ts


def test_one_alert_per_quiet_episode():
    # The episode key IS the fact being measured, so a reboot, a Restart=on-failure
    # or an update cannot re-fire an alert that already went out.
    ts = _STALE_NOW - 9 * 86400
    state = {"completed_at": ts, "alerted_for": ts}
    assert ibm._stale_verdict(state, _STALE_NOW, _WEEK)[0] is False
    assert ibm._stale_verdict(state, _STALE_NOW + 30 * 86400, _WEEK)[0] is False


def test_a_successful_backup_rearms_the_alert(monkeypatch, tmp_path):
    monkeypatch.setattr(ibm, "LAST_BACKUP_FILE", str(tmp_path / "last_backup.json"))
    old = _STALE_NOW - 9 * 86400
    _record(tmp_path, completed_at=old, alerted_for=old)
    ibm.record_backup_success("12:00 / 15 Jan 2027", verified=True)
    state = ibm._read_last_backup()
    assert state["alerted_for"] is None
    assert ibm._stale_verdict(state, state["completed_at"] + 9 * 86400, _WEEK)[0] is True


def test_a_clock_that_stepped_backwards_does_not_alert():
    assert ibm._stale_verdict(
        {"completed_at": _STALE_NOW + 30 * 86400}, _STALE_NOW, _WEEK)[0] is False


def test_a_threshold_of_zero_turns_the_alert_off():
    assert ibm._stale_verdict(
        {"completed_at": _STALE_NOW - 900 * 86400}, _STALE_NOW, 0)[0] is False


def test_stale_limits_default_and_survive_junk_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(ibm, "CONFIG_PATH", str(cfg))
    cfg.write_text("")
    assert ibm._stale_limits() == (True, _WEEK)
    for junk in ("stale_after_sec: junk", "stale_after_sec: true",
                 "stale_after_sec: null", "stale_after_sec: -5"):
        cfg.write_text("backup:\n  " + junk + "\n")
        assert ibm._stale_limits() == (True, _WEEK)
    cfg.write_text("backup: not-a-dict\n")
    assert ibm._stale_limits() == (True, _WEEK)
    # Clamped, not honoured: 60s would alert hourly forever.
    cfg.write_text("backup:\n  stale_after_sec: 60\n")
    assert ibm._stale_limits() == (True, 3600)
    cfg.write_text("backup:\n  stale_after_sec: 0\n")
    assert ibm._stale_limits() == (True, 0)


def test_fifty_ticks_send_exactly_one_alert(monkeypatch, tmp_path):
    notes = _stale_env(monkeypatch, tmp_path)
    _record(tmp_path, completed_at=_STALE_NOW - 9 * 86400,
            completed_at_str="12:00 / 06 Jan 2027", seeded=False)
    for _ in range(50):
        ibm._stale_tick(None, now_fn=lambda: _STALE_NOW)
    assert [ev for ev, _ in notes.sent] == ["backup_stale"]
    pl = notes.sent[0][1]
    assert pl["age_seconds"] == 9 * 86400
    assert pl["age_days"] == 9.0
    assert pl["threshold_seconds"] == _WEEK
    assert pl["ever_completed"] is True
    assert pl["seeded"] is False
    assert pl["last_backup"] == "12:00 / 06 Jan 2027"
    # device_allowed vocabulary, so the alert names a probable cause without a
    # single new probe.
    assert pl["likely_cause"] == "no_device"


def test_the_once_per_episode_rule_is_proven_against_the_file(monkeypatch, tmp_path):
    notes = _stale_env(monkeypatch, tmp_path)
    _record(tmp_path, completed_at=_STALE_NOW - 9 * 86400)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is True
    # Clear the process-local latch: a reboot or a Restart=on-failure hands the
    # daemon a fresh one, so only the file may stop the second alert.
    monkeypatch.setattr(ibm, "_stale_alerted_in_process", False)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW + 30 * 86400) is False
    assert len(notes.sent) == 1


def test_the_boot_grace_holds_the_alert_until_the_clock_is_trustworthy(monkeypatch, tmp_path):
    notes = _stale_env(monkeypatch, tmp_path, boot_time=_STALE_NOW - 60)
    _record(tmp_path, completed_at=_STALE_NOW - 700 * 86400)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    assert notes.sent == []
    monkeypatch.setattr(ibm, "_boot_time", lambda: _STALE_NOW - 3600)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is True
    assert len(notes.sent) == 1


def test_a_running_backup_or_an_update_in_flight_skips_the_tick(monkeypatch, tmp_path):
    notes = _stale_env(monkeypatch, tmp_path, running=True)
    _record(tmp_path, completed_at=_STALE_NOW - 9 * 86400)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    monkeypatch.setattr(ibm, "_backup_running", False)
    monkeypatch.setattr(ibm, "_updating_requested", lambda: True)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    assert notes.sent == []
    monkeypatch.setattr(ibm, "_updating_requested", lambda: False)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is True


def test_the_alert_switch_is_read_live(monkeypatch, tmp_path):
    # The web UI never restarts this daemon on a settings save, so a CFG snapshot
    # would keep alerting after the owner switched the alert off.
    notes = _stale_env(monkeypatch, tmp_path, config="backup:\n  notify_stale: false\n")
    _record(tmp_path, completed_at=_STALE_NOW - 9 * 86400)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    (tmp_path / "config.yaml").write_text("backup:\n  stale_after_sec: 0\n")
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    assert notes.sent == []
    os.remove(str(tmp_path / "config.yaml"))       # unreadable config -> defaults
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is True
    assert len(notes.sent) == 1


def test_an_unwritable_record_still_alerts_but_only_once(monkeypatch, tmp_path):
    # A read-only rootfs must not be able to silence the alert entirely, and must
    # not be able to make it repeat every 15 minutes either.
    notes = _stale_env(monkeypatch, tmp_path)
    _record(tmp_path, completed_at=_STALE_NOW - 9 * 86400)
    monkeypatch.setattr(ibm, "_write_last_backup", lambda **kw: False)
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is True
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    assert len(notes.sent) == 1


def test_a_corrupt_record_is_reseeded_without_alerting(monkeypatch, tmp_path):
    notes = _stale_env(monkeypatch, tmp_path)
    (tmp_path / "last_backup.json").write_bytes(b"{not json")
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(tmp_path / "nothing")})
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    assert notes.sent == []
    assert ibm._read_last_backup()["first_seen"] == _STALE_NOW


def test_the_first_tick_seeds_from_the_newest_backup_folder(monkeypatch, tmp_path):
    # Without a seed, every already-working device would report "never backed up"
    # the first time this version runs.
    notes = _stale_env(monkeypatch, tmp_path)
    bd = tmp_path / "backups"
    (bd / "old").mkdir(parents=True)
    (bd / "new").mkdir()
    os.utime(bd / "old", (_STALE_NOW - 40 * 86400,) * 2)
    os.utime(bd / "new", (_STALE_NOW - 2 * 86400,) * 2)
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(bd)})
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    rec = ibm._read_last_backup()
    assert rec["seeded"] is True
    assert round(rec["completed_at"]) == round(_STALE_NOW - 2 * 86400)
    assert notes.sent == []


def test_a_device_with_no_backups_at_all_seeds_from_now(monkeypatch, tmp_path):
    notes = _stale_env(monkeypatch, tmp_path)
    bd = tmp_path / "backups"
    bd.mkdir()
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(bd)})
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    rec = ibm._read_last_backup()
    assert rec["completed_at"] is None
    assert rec["first_seen"] == _STALE_NOW
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW + 9 * 86400) is True
    assert notes.sent[0][1]["ever_completed"] is False


def test_a_clock_reading_1970_seeds_nothing(monkeypatch, tmp_path):
    # A dead RTC cell with no network: anchoring first_seen to that clock makes
    # the next tick, after NTP corrects it, compute an age of decades and alert on
    # a perfectly healthy device.
    notes = _stale_env(monkeypatch, tmp_path)
    bd = tmp_path / "backups"
    bd.mkdir()
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(bd)})
    assert ibm._stale_tick(None, now_fn=lambda: 1000.0) is False
    assert not os.path.exists(str(tmp_path / "last_backup.json"))
    assert notes.sent == []
    assert ibm._stale_tick(None, now_fn=lambda: _STALE_NOW) is False
    assert ibm._read_last_backup()["first_seen"] == _STALE_NOW


def test_record_backup_success_stamps_and_rearms(monkeypatch, tmp_path):
    monkeypatch.setattr(ibm, "LAST_BACKUP_FILE", str(tmp_path / "last_backup.json"))
    _record(tmp_path, completed_at=1.0, alerted_for=1.0, seeded=True)
    assert ibm.record_backup_success("12:00 / 15 Jan 2027", verified=True) is True
    rec = ibm._read_last_backup()
    assert rec["completed_at"] > 1.0
    assert rec["completed_at_str"] == "12:00 / 15 Jan 2027"
    assert rec["verified"] is True
    assert rec["seeded"] is False
    assert rec["alerted_for"] is None


def test_the_info_screen_prefers_the_durable_record(monkeypatch, tmp_path):
    bd = tmp_path / "backups"
    (bd / "run").mkdir(parents=True)
    os.utime(bd / "run", (_STALE_NOW - 3 * 86400,) * 2)
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(bd)})
    monkeypatch.setattr(ibm, "LAST_BACKUP_FILE", str(tmp_path / "last_backup.json"))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("")
    monkeypatch.setattr(ibm, "CONFIG_PATH", str(cfg))
    # No record: the folder scan, byte-identical to what this line always printed.
    assert ibm.get_last_backup_str() == time.strftime(
        "%H:%M / %d %b %Y", time.localtime(_STALE_NOW - 3 * 86400))
    # A record wins over the folder mtime, which an interrupted run also bumps.
    fresh = time.time() - 3600
    _record(tmp_path, completed_at=fresh)
    assert ibm.get_last_backup_str() == time.strftime(
        "%H:%M / %d %b %Y", time.localtime(fresh))
    # ... and a stale one says so, on the screen someone taps to check.
    _record(tmp_path, completed_at=time.time() - 9 * 86400)
    line = "Backup: " + ibm.get_last_backup_str()
    assert line.endswith("(stale)")
    assert len(line) <= 40          # _draw_info centres without wrapping
    os.remove(str(tmp_path / "last_backup.json"))
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(tmp_path / "gone")})
    assert ibm.get_last_backup_str() == "No backups"


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


# --- the in-run health probe ---------------------------------------------------
# Battery, mount and free space were all true when the run started; each can stop
# being true while it runs. Every dependency is injected, so none of this needs
# hardware, a subprocess or a sleep.

class _Stat:
    def __init__(self, st_dev):
        self.st_dev = st_dev


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _make_health(tmp_path, *, base_st_dev=1, stat_fn=None, exists_fn=None,
                 statvfs_fn=None, battery_fn=None, now=None, min_battery=0,
                 reserve_mb=512):
    if stat_fn is None:
        stat_fn = lambda p: _Stat(1)
    if exists_fn is None:
        exists_fn = lambda p: True
    if statvfs_fn is None:
        statvfs_fn = _statvfs_for(2000, 5000)
    return ibm._RunHealth(str(tmp_path), str(tmp_path / ".foldermarker"),
                          base_st_dev, min_battery, reserve_mb,
                          stat_fn=stat_fn, exists_fn=exists_fn,
                          statvfs_fn=statvfs_fn, battery_fn=battery_fn, now=now)


def test_a_healthy_run_reports_nothing(tmp_path):
    assert _make_health(tmp_path)() is None


def test_a_changed_device_id_aborts_immediately(tmp_path):
    # Positive evidence, not absence of it: the mountpoint is no longer the
    # filesystem the run started on, so every further second lands on the rootfs.
    h = _make_health(tmp_path, base_st_dev=1, stat_fn=lambda p: _Stat(2))
    outcome, why = h()
    assert outcome == "mount_lost"
    assert h.reason == why


def test_an_unreadable_baseline_never_trips_on_drift(tmp_path):
    h = _make_health(tmp_path, base_st_dev=None, stat_fn=lambda p: _Stat(2))
    assert h() is None


def test_a_stat_that_raises_is_no_evidence(tmp_path):
    def boom(path):
        raise OSError("hung device")
    assert _make_health(tmp_path, stat_fn=boom)() is None


def test_a_missing_marker_needs_two_ticks(tmp_path):
    h = _make_health(tmp_path, exists_fn=lambda p: False)
    assert h() is None                       # one EIO reads exactly like an unmount
    assert h()[0] == "mount_lost"


def test_a_marker_that_comes_back_resets_the_streak(tmp_path):
    seen = [False, True, False]
    h = _make_health(tmp_path, exists_fn=lambda p: seen.pop(0))
    assert h() is None
    assert h() is None
    assert h() is None                       # a single later miss must not abort


def test_an_exists_probe_that_raises_is_ignored(tmp_path):
    def boom(path):
        raise OSError("hung device")
    assert _make_health(tmp_path, exists_fn=boom)() is None


def test_an_unconfirmed_unmount_is_never_reported_as_a_full_drive(tmp_path):
    # statvfs on an unmounted mountpoint reports the ROOTFS. Evaluating disk before
    # mount would announce "Backup drive: 10MB free" for a drive that is gone.
    h = _make_health(tmp_path, exists_fn=lambda p: False,
                     statvfs_fn=_statvfs_for(2000, 10))
    assert h() is None
    assert h()[0] == "mount_lost"


def test_a_filling_drive_aborts_against_the_reserve(tmp_path):
    h = _make_health(tmp_path, statvfs_fn=_statvfs_for(2000, 100), reserve_mb=512)
    outcome, why = h()
    assert outcome == "disk_full"
    assert "100MB" in why
    assert _make_health(tmp_path, statvfs_fn=_statvfs_for(2000, 100),
                        reserve_mb=0)() is None      # drive check off ...
    assert _make_health(tmp_path, statvfs_fn=_statvfs_for(100, 5000),
                        reserve_mb=0)()[0] == "disk_full"   # ... rootfs backstop is not


def test_an_unreadable_filesystem_does_not_abort(tmp_path):
    def boom(path):
        raise OSError("nope")
    assert _make_health(tmp_path, statvfs_fn=boom)() is None


def test_a_draining_battery_aborts_after_two_low_reads(tmp_path):
    clock = _Clock()
    reads = []

    def batt():
        reads.append(1)
        return {"percent": 28.0, "charging": False}

    h = _make_health(tmp_path, min_battery=35, battery_fn=batt, now=clock)
    assert h() is None and reads == []       # not due on the first tick
    clock.advance(ibm.HEALTH_BATTERY_EVERY_SEC)
    assert h() is None                       # one low read only arms the confirmation
    clock.advance(ibm.HEALTH_BATTERY_EVERY_SEC)
    outcome, why = h()
    assert outcome == "battery_abort"
    assert "28" in why


def test_a_charging_device_is_never_aborted(tmp_path):
    clock = _Clock()
    h = _make_health(tmp_path, min_battery=35, now=clock,
                     battery_fn=lambda: {"percent": 10.0, "charging": True})
    for _ in range(4):
        clock.advance(ibm.HEALTH_BATTERY_EVERY_SEC)
        assert h() is None


def test_an_unreadable_pack_disarms_the_battery_probe(tmp_path):
    clock = _Clock()
    reads = []

    def batt():
        reads.append(1)
        return {"percent": None, "charging": None}

    h = _make_health(tmp_path, min_battery=35, battery_fn=batt, now=clock)
    for _ in range(ibm.HEALTH_BATTERY_UNREADABLE_MAX + 3):
        clock.advance(ibm.HEALTH_BATTERY_EVERY_SEC)
        assert h() is None
    # A unit with no PiSugar stops paying the socket cost for the rest of the run.
    assert len(reads) == ibm.HEALTH_BATTERY_UNREADABLE_MAX


def test_a_battery_probe_that_raises_is_ignored(tmp_path):
    clock = _Clock()

    def boom():
        raise OSError("socket wedged")

    h = _make_health(tmp_path, min_battery=35, battery_fn=boom, now=clock)
    clock.advance(ibm.HEALTH_BATTERY_EVERY_SEC)
    assert h() is None


def test_a_zero_floor_never_touches_the_battery(tmp_path):
    clock = _Clock()
    reads = []
    h = _make_health(tmp_path, min_battery=0, now=clock,
                     battery_fn=lambda: reads.append(1) or {"percent": 5.0,
                                                            "charging": False})
    for _ in range(4):
        clock.advance(ibm.HEALTH_BATTERY_EVERY_SEC)
        assert h() is None
    assert reads == []


def test_the_cheap_probes_run_every_tick_and_the_costly_one_does_not(tmp_path):
    # Two cost classes, not three: a stat and two statvfs calls are syscalls, a
    # PiSugar read is a socket round trip on the thread pumping idevicebackup2.
    clock = _Clock()
    marker_reads, battery_reads = [], []
    h = _make_health(tmp_path, min_battery=35, now=clock,
                     exists_fn=lambda p: marker_reads.append(1) or True,
                     battery_fn=lambda: battery_reads.append(1) or {"percent": 90.0,
                                                                    "charging": False})
    for _ in range(5):
        assert h() is None
    assert len(marker_reads) == 5 and battery_reads == []
    clock.advance(ibm.HEALTH_BATTERY_EVERY_SEC)
    assert h() is None
    assert len(battery_reads) == 1


# --- tee_and_parse rides the health probe on its existing poll ------------------
# select() is stubbed rather than skipped, so these run on Windows too: the real
# loop, a fake process, no pipes.

class _FakeSelect:
    @staticmethod
    def select(rlist, wlist, xlist, timeout):
        time.sleep(0.01)
        return ([], [], [])


class _FakeProc:
    def __init__(self, poll_value=None):
        self.stdout = types.SimpleNamespace(fileno=lambda: 3)
        self._poll = poll_value
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._poll

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_a_health_verdict_stops_a_running_backup(monkeypatch):
    monkeypatch.setattr(ibm, "select", _FakeSelect)
    proc = _FakeProc()
    outcome = ibm.tee_and_parse(proc, None, lambda tok: None, silence_timeout=60,
                                health_check=lambda: ("disk_full", "Backup drive: 180MB free"),
                                health_poll_sec=0.05)
    assert outcome == "disk_full"
    # The SIGTERM grace is the feature here: idevicebackup2 finishes the file in
    # flight, so the partial backup stays resumable.
    assert proc.terminated and not proc.killed


def test_a_vanished_drive_skips_the_sigterm_grace(monkeypatch):
    monkeypatch.setattr(ibm, "select", _FakeSelect)
    proc = _FakeProc()
    outcome = ibm.tee_and_parse(proc, None, lambda tok: None, silence_timeout=60,
                                health_check=lambda: ("mount_lost", "Backup drive disappeared"),
                                health_poll_sec=0.05)
    assert outcome == "mount_lost"
    # Every second of grace after the drive vanished is another second of writing
    # into the empty mountpoint on the rootfs.
    assert proc.killed and not proc.terminated


def test_a_raising_health_check_does_not_break_the_pump(monkeypatch):
    monkeypatch.setattr(ibm, "select", _FakeSelect)

    def boom():
        raise OSError("probe blew up")

    proc = _FakeProc()
    outcome = ibm.tee_and_parse(proc, None, lambda tok: None, silence_timeout=60,
                                device_gone=lambda: True, device_poll_sec=0.06,
                                health_check=boom, health_poll_sec=0.01)
    assert outcome == "unplugged"          # the loop kept running past the failure


def test_the_common_unplug_wins_the_tie(monkeypatch):
    monkeypatch.setattr(ibm, "select", _FakeSelect)
    proc = _FakeProc()
    outcome = ibm.tee_and_parse(proc, None, lambda tok: None, silence_timeout=60,
                                device_gone=lambda: True, device_poll_sec=0.05,
                                health_check=lambda: ("disk_full", "x"),
                                health_poll_sec=0.05)
    assert outcome == "unplugged"


def test_a_finished_process_is_never_mislabelled(monkeypatch):
    monkeypatch.setattr(ibm, "select", _FakeSelect)
    proc = _FakeProc(poll_value=0)          # already exited
    outcome = ibm.tee_and_parse(proc, None, lambda tok: None, silence_timeout=0.3,
                                health_check=lambda: ("disk_full", "Backup drive: 0MB free"),
                                health_poll_sec=0.01)
    assert outcome == "silent"              # not "disk_full"


# --- run_backup reports why it stopped -----------------------------------------

def _drive_run_backup(monkeypatch, tmp_path, outcome, *, use_real_probe=False,
                      cfg_backup=None, seen=None):
    """Run run_backup with everything before and after tee_and_parse stubbed, and
    tee_and_parse itself replaced by something that yields `outcome`.

    check_battery is deliberately NOT stubbed: the default cfg_backup switches the
    floor off, so the real gate returns on its first line, and a test that raises
    the floor gets the real pre-flight refusal. `seen`, when passed, collects what
    run_backup did on its way through - "popen" for the idevicebackup2 launches,
    "tee_kwargs" for what it handed the watchdog - so the wiring between
    run_backup and its guards can be asserted rather than assumed."""
    stop_file = tmp_path / "stop_requested"
    statuses, notes = [], _Notes()
    if cfg_backup is None:
        cfg_backup = {"min_battery_percent": 0}
    if seen is None:
        seen = {}
    seen["popen"] = []
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(tmp_path),
                                     "marker_file": ".foldermarker",
                                     "backup": cfg_backup})
    monkeypatch.setattr(ibm, "STOP_FILE", str(stop_file))
    monkeypatch.setattr(ibm, "check_backup_mount", lambda logf, ui: True)
    monkeypatch.setattr(ibm, "check_disk_space", lambda logf, ui: True)
    monkeypatch.setattr(ibm, "_check_encryption", lambda logf, ui: None)
    monkeypatch.setattr(ibm.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_popen(*a, **kw):
        seen["popen"].append(a)
        return _FakeProc()

    monkeypatch.setattr(ibm.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ibm, "write_status",
                        lambda state, **kw: statuses.append((state, kw)))
    monkeypatch.setattr(ibm, "send_notification", notes)
    import notifications
    monkeypatch.setattr(notifications, "prime_auth", lambda *a, **kw: None)

    def fake_tee(proc, logf, on_line, **kw):
        seen["tee_kwargs"] = kw
        # A stop request landing just before the abort: this branch returns without
        # reaching the code that clears the sentinel, and webui's quiesce wait polls
        # for it to disappear.
        stop_file.write_text("x")
        proc.returncode = 0
        if use_real_probe:
            health = kw["health_check"]
            for _ in range(3):
                verdict = health()
                if verdict:
                    return verdict[0]
        return outcome

    monkeypatch.setattr(ibm, "tee_and_parse", fake_tee)
    ui = RecordingUI()
    rc = ibm.run_backup(FakePanel(), None, ui)
    return rc, statuses, notes, ui, stop_file


def test_a_vanished_drive_is_reported_not_swallowed(monkeypatch, tmp_path):
    # tmp_path has no marker file, so the REAL _RunHealth reaches mount_lost after
    # two ticks - the probe and the reporting are wired together here, not faked.
    rc, statuses, notes, ui, stop_file = _drive_run_backup(
        monkeypatch, tmp_path, None, use_real_probe=True)
    interrupted = [kw for state, kw in statuses if state == "interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["reason_code"] == "mount_lost"
    assert "drive" in interrupted[0]["reason"].lower()
    assert notes.codes("backup_error") == ["mount_lost"]
    assert "Backup stopped." in ui.last["subtitle"]
    assert not stop_file.exists()      # the abort cleared it
    assert rc == 0                     # reported, not retried


def test_a_low_battery_abort_reaches_the_dashboard(monkeypatch, tmp_path):
    rc, statuses, notes, ui, _stop = _drive_run_backup(
        monkeypatch, tmp_path, "battery_abort")
    interrupted = [kw for state, kw in statuses if state == "interrupted"]
    assert interrupted[0]["reason_code"] == "battery_abort"
    assert interrupted[0]["reason"] == ibm.BACKUP_ABORT_REASONS["battery_abort"]
    assert notes.codes("backup_error") == ["battery_abort"]
    assert "sync_error" not in [ev for ev, _ in notes.sent]   # no rsync after an abort
    assert rc == 0


def test_a_flat_battery_stops_the_run_before_idevicebackup2_starts(monkeypatch, tmp_path):
    """The gate's own behaviour is covered above; this pins that run_backup still
    calls it. Nothing else in the suite notices if that call goes away, and
    without it a backup starting at 33% is back to being powered off mid-write by
    PiSugar at 30%."""
    monkeypatch.setattr(ibm.time, "sleep", lambda s: None)
    _battery(monkeypatch, 20.0)
    seen = {}
    rc, statuses, notes, ui, _stop = _drive_run_backup(
        monkeypatch, tmp_path, "eof", cfg_backup={"min_battery_percent": 35},
        seen=seen)
    assert rc == 2
    assert seen["popen"] == []          # refused outright, not started and aborted
    assert statuses[0][0] == "error"
    assert notes.codes("backup_error") == ["battery_low"]


def test_the_in_run_battery_guard_gets_the_configured_floor(monkeypatch, tmp_path):
    """The other half of the same policy: a charged device passes the pre-flight
    gate, and the floor and reserve _backup_safety_limits resolved have to reach
    the probe riding tee_and_parse's poll. _RunHealth arms its battery check from
    min_battery, so a 0 handed in here is a mid-run guard that never fires."""
    _battery(monkeypatch, 80.0)
    seen = {}
    rc, statuses, notes, ui, _stop = _drive_run_backup(
        monkeypatch, tmp_path, "silent", seen=seen,
        cfg_backup={"min_battery_percent": 35, "min_free_mb": 2048})
    assert len(seen["popen"]) == 1      # the gate passed, so the run happened
    health = seen["tee_kwargs"]["health_check"]
    assert health.min_battery == 35
    assert health.reserve_mb == 2048
    assert rc == 0


def test_the_hung_backup_report_is_unchanged(monkeypatch, tmp_path):
    # The widened branch must not disturb the two outcomes that already worked.
    rc, statuses, notes, ui, _stop = _drive_run_backup(monkeypatch, tmp_path, "silent")
    interrupted = [kw for state, kw in statuses if state == "interrupted"]
    assert interrupted[0]["reason"] == "Backup hung (no progress)"
    assert ui.last["screen"] == "interrupted"
    assert notes.sent[-1][0] == "backup_error"
    assert rc == 0


# --- the daemon's own run log is bounded too ------------------------------------
# backup-*.log is one file for the whole daemon lifetime, not one per backup, so it
# is the run log most able to fill the rootfs. It used to be opened with a bare
# open(), which left logutil's per-file cap applying only to sync logs.

def test_the_daemon_run_log_goes_through_the_capped_writer(tmp_path, monkeypatch):
    ibm = _load_daemon()
    import logutil
    monkeypatch.setattr(ibm, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logutil, "prune_logs", lambda *a, **kw: None)
    f, path = ibm.log_open()
    try:
        assert isinstance(f, logutil.TimestampedLog)
        assert f._max > 0                      # a cap is actually in force
    finally:
        f.close()
    assert os.path.dirname(path) == str(tmp_path)


def test_the_daemon_run_log_keeps_its_own_line_format(tmp_path, monkeypatch):
    """stamp=False: these lines carry their own tags, and a second wall-clock
    prefix in front of the daemon's own would be a format change, not a cap."""
    ibm = _load_daemon()
    import logutil
    monkeypatch.setattr(ibm, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logutil, "prune_logs", lambda *a, **kw: None)
    f, path = ibm.log_open()
    f.write("[ERROR] something\n")
    f.close()
    lines = open(path).read().splitlines()
    assert lines[1] == "[ERROR] something"     # unstamped, exactly as written


def test_a_runaway_daemon_run_log_is_capped(tmp_path, monkeypatch):
    ibm = _load_daemon()
    import logutil
    monkeypatch.setattr(ibm, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logutil, "prune_logs", lambda *a, **kw: None)
    monkeypatch.setattr(logutil, "LOG_MAX_BYTES_PER_FILE", 2000)
    f, path = ibm.log_open()
    for i in range(5000):
        f.write(f"[ERROR] runaway loop line {i}\n")
    f.close()
    size = os.path.getsize(path)
    assert size < 200_000, f"run log grew to {size} bytes despite the cap"
    assert "cap" in open(path).read()           # and it says it was truncated


# --- backup completion check ----------------------------------------------------
# The old check picked the newest directory under backup_dir and asked only whether
# a Manifest.plist was there and parsed. That passes a snapshot the device says is
# still uploading, passes a folder with no file index at all, and - because an
# interrupted run leaves the PREVIOUS Manifest.plist in place - passes a folder
# whose newest data never landed.

_UDID = "00008030-000E4CE23C40001E"


def _write_status_plist(folder, state, full):
    payload = {}
    if state is not None:
        payload["SnapshotState"] = state
    if full is not None:
        payload["IsFullBackup"] = full
    with open(os.path.join(str(folder), "Status.plist"), "wb") as f:
        plistlib.dump(payload, f)


def _backup_folder(root, udid=_UDID, *, state="finished", full=False,
                   status=True, index="Manifest.db", index_bytes=b"SQLite format 3\x00"):
    """A backup folder as the drive holds one: <backup_dir>/<UDID>/."""
    folder = root / udid
    folder.mkdir(parents=True, exist_ok=True)
    with open(str(folder / "Manifest.plist"), "wb") as f:
        plistlib.dump({"Version": "10.0", "IsEncrypted": False}, f)
    if status:
        _write_status_plist(folder, state, full)
    if index:
        target = folder / index
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(index_bytes)
    return folder


def test_an_uploading_snapshot_is_not_complete(tmp_path):
    _backup_folder(tmp_path, state="uploading")
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is False
    assert "uploading" in msg


def test_a_backup_with_no_file_index_is_not_complete(tmp_path):
    _backup_folder(tmp_path, status=False, index=None)
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is False
    assert "Manifest.db" in msg


def test_an_empty_index_is_not_complete(tmp_path):
    _backup_folder(tmp_path, index_bytes=b"")
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is False
    assert "empty" in msg


def test_an_untouched_manifest_is_not_this_runs_backup(tmp_path):
    folder = _backup_folder(tmp_path)
    before = ibm._completion_marks(str(folder))
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID, before=before)
    assert ok is False
    assert "no new Manifest.plist" in msg
    # ...and the moment the device does rewrite one, the same fingerprint passes.
    # Without this half the rule would be a guard that can never be satisfied.
    _write_status_plist(folder, "finished", True)
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID, before=before)
    assert ok is True, msg


def test_the_udid_folder_wins_over_a_newer_sibling(tmp_path):
    # lost+found, a desktop's .Trash-1000 or a second phone can all be the newest
    # directory on the drive; verifying one of those reports a good backup as
    # missing its Manifest.plist.
    _backup_folder(tmp_path)
    sibling = tmp_path / "lost+found"
    sibling.mkdir()
    future = time.time() + 3600
    os.utime(str(sibling), (future, future))
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is True, msg


def test_an_unrecognised_snapshot_state_degrades(tmp_path):
    # A state string this appliance has never seen must not fail a backup - it is
    # echoed so the vocabulary can be corrected from a real run log.
    _backup_folder(tmp_path, state="quiescing")
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is True
    assert "quiescing" in msg


def test_a_backup_with_no_status_plist_still_verifies(tmp_path):
    # Degrades to the old behaviour rather than failing closed.
    _backup_folder(tmp_path, status=False)
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is True, msg
    assert "Status.plist" in msg


def test_an_index_only_under_snapshot_is_found(tmp_path):
    _backup_folder(tmp_path, index="Snapshot/Manifest.db")
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is True, msg


def test_an_old_mbdb_index_is_accepted(tmp_path):
    _backup_folder(tmp_path, index="Manifest.mbdb", state=None)
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is True, msg


def test_a_missing_index_is_only_a_note_when_the_device_said_finished(tmp_path):
    # Where the index sits on this appliance is not settled, so a miss the device
    # itself contradicts is reported, not failed: marking every good backup
    # unverified would just train the owner to ignore the field.
    _backup_folder(tmp_path, state="finished", index=None)
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID)
    assert ok is True, msg
    assert "Manifest.db" in msg


def test_completion_marks_fail_open(tmp_path):
    assert ibm._completion_marks(None) is None
    marks = ibm._completion_marks(str(tmp_path / "never-existed"))
    assert marks["files"] == {"Manifest.plist": None, "Status.plist": None}
    # A fingerprint of files that were not there, or one taken on another folder,
    # can never produce the staleness verdict - a probe that failed is not
    # evidence.
    _backup_folder(tmp_path)
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID, before=marks)
    assert ok is True, msg
    elsewhere = dict(ibm._completion_marks(str(tmp_path / _UDID)), dir="/somewhere/else")
    ok, msg = ibm.verify_backup_integrity(str(tmp_path), None, udid=_UDID, before=elsewhere)
    assert ok is True, msg


class _CompleteUI(RecordingUI):
    def request_full(self):
        pass


def _drive_completed_backup(monkeypatch, tmp_path, on_run):
    """run_backup down the rc == 0 path, with `on_run` standing in for whatever
    idevicebackup2 wrote while it ran."""
    statuses, notes = [], _Notes()
    monkeypatch.setattr(ibm, "CFG", {"backup_dir": str(tmp_path),
                                     "marker_file": ".foldermarker",
                                     "disk_device": "/dev/sda1",
                                     "owner_lines": ["owner", "phone", "mail", "note"],
                                     "backup": {"min_battery_percent": 0}})
    monkeypatch.setattr(ibm, "STOP_FILE", str(tmp_path / "stop_requested"))
    monkeypatch.setattr(ibm, "check_backup_mount", lambda logf, ui: True)
    monkeypatch.setattr(ibm, "check_disk_space", lambda logf, ui: True)
    monkeypatch.setattr(ibm, "_check_encryption", lambda logf, ui: None)
    monkeypatch.setattr(ibm.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ibm.subprocess, "Popen", lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(ibm, "get_disk_usage_pct", lambda dev: 42)
    monkeypatch.setattr(ibm, "write_status",
                        lambda state, **kw: statuses.append((state, kw)))
    monkeypatch.setattr(ibm, "send_notification", notes)
    monkeypatch.setattr(ibm.time, "sleep", lambda s: None)
    import notifications
    monkeypatch.setattr(notifications, "prime_auth", lambda *a, **kw: None)

    def fake_tee(proc, logf, on_line, **kw):
        on_run()
        proc.returncode = 0
        return "eof"

    monkeypatch.setattr(ibm, "tee_and_parse", fake_tee)
    rc = ibm.run_backup(FakePanel(), None, _CompleteUI(), udid=_UDID)
    return rc, statuses, notes


def test_the_verdict_and_its_reason_reach_the_dashboard(monkeypatch, tmp_path):
    folder = _backup_folder(tmp_path)
    rc, statuses, notes = _drive_completed_backup(
        monkeypatch, tmp_path, lambda: _write_status_plist(folder, "finished", True))
    complete = [kw for state, kw in statuses if state == "complete"]
    assert complete[0]["verified"] is True
    # The boolean alone says a check ran, not what it found; both places that
    # already carry the boolean now carry the sentence with it.
    assert "complete" in complete[0]["verify_detail"]
    payload = [pl for ev, pl in notes.sent if ev == "backup_complete"][0]
    assert payload["verified"] is True
    assert payload["verify_detail"] == complete[0]["verify_detail"]
    assert rc == 0


def test_a_run_that_rewrote_nothing_is_not_reported_as_verified(monkeypatch, tmp_path):
    # rc == 0 with the previous run's Manifest.plist untouched: the case the old
    # check could not see. The fingerprint is taken before Popen, so run_backup
    # itself is what has to be carrying it here.
    _backup_folder(tmp_path)
    rc, statuses, notes = _drive_completed_backup(monkeypatch, tmp_path, lambda: None)
    complete = [kw for state, kw in statuses if state == "complete"]
    assert complete[0]["verified"] is False
    assert "no new Manifest.plist" in complete[0]["verify_detail"]
    # Still a completed backup on the panel and still one backup_complete event:
    # a check that can be wrong does not get to paint a failure.
    assert [ev for ev, _ in notes.sent].count("backup_complete") == 1
    assert rc == 0


def test_a_completed_backup_stamps_the_durable_record(monkeypatch, tmp_path):
    # The status file lives on zram and main() overwrites it with "waiting" on
    # every start, so the completion has to be recorded somewhere that survives a
    # reboot or the quiet-device alert has nothing to measure against.
    monkeypatch.setattr(ibm, "LAST_BACKUP_FILE", str(tmp_path / "last_backup.json"))
    folder = _backup_folder(tmp_path)
    rc, _statuses, notes = _drive_completed_backup(
        monkeypatch, tmp_path, lambda: _write_status_plist(folder, "finished", True))
    rec = ibm._read_last_backup()
    assert rec["completed_at"] > 0
    assert rec["verified"] is True
    assert rec["seeded"] is False
    assert rec["alerted_for"] is None
    payload = [pl for ev, pl in notes.sent if ev == "backup_complete"][0]
    assert payload["was_stale"] is False
    assert rc == 0


def test_recovery_from_a_quiet_episode_rides_the_completion_event(monkeypatch, tmp_path):
    # No thirteenth event: the alert an automation raised is cleared by the one
    # notification that already fires at the exact moment of recovery.
    monkeypatch.setattr(ibm, "LAST_BACKUP_FILE", str(tmp_path / "last_backup.json"))
    alerted = time.time() - 9 * 86400
    (tmp_path / "last_backup.json").write_text(
        json.dumps({"completed_at": alerted, "alerted_for": alerted}))
    folder = _backup_folder(tmp_path)
    _rc, _statuses, notes = _drive_completed_backup(
        monkeypatch, tmp_path, lambda: _write_status_plist(folder, "finished", True))
    payload = [pl for ev, pl in notes.sent if ev == "backup_complete"][0]
    assert payload["was_stale"] is True
    assert payload["stale_days"] == 9.0
    assert ibm._read_last_backup()["alerted_for"] is None
