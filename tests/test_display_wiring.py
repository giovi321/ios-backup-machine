"""Wiring tests for the display daemon's info-screen and screen-handover paths.

These cover the code the uipolicy unit tests can't reach: how Animator.set /
resume / cancel_info and the button listener actually use the info window. That
wiring is where both e-ink bugs lived, so it is worth testing directly.

iosbackupmachine.py needs PIL and yaml (both in requirements.txt, which CI
installs), plus two modules that may not import on a dev machine: waveshare_epd
ships with the panel rather than pip, and python-periphery needs Linux fcntl. The
real module is used whenever it imports, and stubbed only when it doesn't, so
this runs on Windows and against the genuine dependency in CI. If the import
still fails the module skips rather than breaking the suite.
"""
import os
import sys
import tempfile
import types

import pytest

SANDBOX = tempfile.mkdtemp(prefix="ibm_wiring_")


def _load_daemon():
    """Import iosbackupmachine against a throwaway config/runtime dir."""
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
    # python-periphery needs Linux fcntl; on CI the real one imports and is used.
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
    """Animator only calls into the panel when it draws; these tests never tick."""
    def draw(self, **kw):
        pass


class Clock:
    """Drives ibm._now() so the 30s window can be crossed without sleeping."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(ibm, "_now", c)
    return c


@pytest.fixture
def ui(clock):
    a = ibm.Animator(FakePanel())
    ibm._info_window = ibm.uipolicy.InfoWindow(duration=30)
    return a


@pytest.fixture(autouse=True)
def fast_info_lines(monkeypatch):
    # The real one probes network/temperature; irrelevant here and slow.
    monkeypatch.setattr(ibm, "build_button_info_lines", lambda: [("info", False)])


# --- Animator honours the info window ---------------------------------------

def test_a_normal_update_applies_when_no_tap_is_active(ui):
    ui.set(screen="normal", subtitle="Syncing...")
    assert ui.get_state()["subtitle"] == "Syncing..."


def test_an_update_is_held_back_while_the_info_screen_is_up(ui):
    ibm._info_window.open(ibm._now(), ui.get_state())
    ui.set(_force=True, screen="info")
    ui.set(screen="normal", subtitle="Syncing... 40%")
    # This is the bug: the sync tick used to land here and wipe the info screen.
    assert ui.get_state()["screen"] == "info"


def test_a_held_back_update_is_applied_on_resume_not_lost(ui):
    ui.set(screen="normal", subtitle="Backing up... 87%")
    before = ui.get_state()
    ibm._info_window.open(ibm._now(), before)
    ui.set(_force=True, screen="info")
    ui.set(screen="complete", center_block="Backup complete")

    ui.resume(before)
    state = ui.get_state()
    assert state["screen"] == "complete"
    assert state["center_block"] == "Backup complete"


def test_resume_falls_back_to_the_pre_tap_screen_when_nothing_happened(ui):
    ui.set(screen="normal", subtitle="Syncing... 40%")
    before = ui.get_state()
    ibm._info_window.open(ibm._now(), before)
    ui.set(_force=True, screen="info")

    ui.resume(before)
    assert ui.get_state()["screen"] == "normal"
    assert ui.get_state()["subtitle"] == "Syncing... 40%"


def test_cancel_info_drops_the_window_and_anything_queued(ui):
    ui.set(screen="normal", subtitle="idle")
    before = ui.get_state()
    ibm._info_window.open(ibm._now(), before)
    ui.set(screen="normal", subtitle="stale queued update")

    ui.cancel_info()
    assert ibm._info_window.active(ibm._now()) is False
    # A backup now owns the panel; the queued update must not resurface.
    ui.set(screen="normal", subtitle="Device detected. Preparing...")
    ui.resume({})
    assert ui.get_state()["subtitle"] == "Device detected. Preparing..."


# --- the button listener's per-poll work ------------------------------------

def _tap():
    open(ibm.INFO_FILE, "w").close()


def test_no_flag_file_means_nothing_happens(ui):
    ui.set(screen="boot")
    ibm._handle_info_tap(ui)
    assert ui.get_state()["screen"] == "boot"


def test_a_tap_shows_the_info_screen_and_consumes_the_flag(ui):
    ui.set(screen="boot")
    _tap()
    ibm._handle_info_tap(ui)
    assert ui.get_state()["screen"] == "info"
    assert not os.path.exists(ibm.INFO_FILE)


def test_a_tap_during_a_sync_still_shows_info_then_gives_the_sync_screen_back(ui, clock):
    # The reported bug: single tap appeared to do nothing while syncing.
    ui.set(screen="normal", subtitle="Syncing to remote server...", percent=40)
    _tap()
    ibm._handle_info_tap(ui)
    assert ui.get_state()["screen"] == "info"

    # Sync ticks keep arriving during the window and must not break through.
    ui.set(screen="normal", subtitle="Syncing to remote server...", percent=55)
    clock.advance(10)
    ibm._handle_info_tap(ui)
    assert ui.get_state()["screen"] == "info"

    # Window closed: back to the sync screen, with the newer percentage.
    clock.advance(21)
    ibm._handle_info_tap(ui)
    state = ui.get_state()
    assert state["screen"] == "normal"
    assert state["percent"] == 55


def test_a_tap_while_a_result_screen_is_held_returns_to_that_result(ui, clock):
    ui.set(screen="complete", center_block="Sync failed.\nreason", animate=False)
    _tap()
    ibm._handle_info_tap(ui)
    assert ui.get_state()["screen"] == "info"

    clock.advance(31)
    ibm._handle_info_tap(ui)
    assert ui.get_state()["screen"] == "complete"
    assert ui.get_state()["center_block"] == "Sync failed.\nreason"


# --- manual start sentinel --------------------------------------------------

def test_a_fresh_start_request_is_seen():
    open(ibm.START_FILE, "w").close()
    try:
        assert ibm._manual_start_requested() is True
    finally:
        os.remove(ibm.START_FILE)


def test_a_stale_start_request_is_ignored():
    open(ibm.START_FILE, "w").close()
    try:
        old = ibm.time.time() - 3600
        os.utime(ibm.START_FILE, (old, old))
        assert ibm._manual_start_requested() is False
    finally:
        os.remove(ibm.START_FILE)


def test_no_start_request_at_all():
    assert not os.path.exists(ibm.START_FILE)
    assert ibm._manual_start_requested() is False
