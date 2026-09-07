"""Tests for Panel, the only writer to the e-ink panel.

Nothing else in the suite constructs a real Panel: the daemon tests all use a
FakePanel, so everything below the Animator was untested. That is where the
blank-display bug lived. Two properties matter and neither fails loudly when it
breaks:

1. _safe_init_call's EBUSY retry. The waveshare driver calls
   epdconfig.module_init() from both init() and init_fast(), and Panel uses both,
   so a kernel gpiochip can refuse the second request for a line this process
   already holds. Panel recovers by releasing the lines and retrying once,
   keyed on GPIOError.errno == 16. Wrapping that error in another type is what
   silently disabled the retry and left the panel dark for a whole daemon run.

2. The API-variant resolution in __init__. epd2in13_V4 has none of the primary
   names Panel looks for first (no init_Full, no init_Part, no display_Base, no
   display_Partial), so every one of them has to land on its fallback. If one
   silently resolved to None the panel would fall back to full refreshes for
   ever: a correct picture, flashing on every tick, with nothing in any log.

The waveshare driver ships with the hardware rather than pip, so a stand-in
modelled on the real epd2in13_V4 method set is injected here.
"""
import sys

import pytest

from test_daemon_hardening import ibm

GPIOError = sys.modules["periphery.gpio"].GPIOError


class FakeEPD:
    """The epd2in13_V4 surface Panel actually touches. Method names match the
    real driver exactly, including the ones Panel has to fall back to."""
    width, height = 122, 250

    def __init__(self, log):
        self.log = log

    def init(self):
        self.log.append("init")

    def init_fast(self):
        self.log.append("init_fast")

    def Clear(self, color=0xFF):
        self.log.append("Clear")

    def getbuffer(self, image):
        return b"buf"

    def display(self, buf):
        self.log.append("display")

    def displayPartBaseImage(self, buf):
        self.log.append("base")

    def displayPartial(self, buf):
        self.log.append("partial")

    def sleep(self):
        self.log.append("sleep")


@pytest.fixture
def panel_env(monkeypatch):
    """Build Panels against FakeEPD and a recording epdconfig."""
    log = []
    monkeypatch.setattr(ibm.epd2in13_V4, "EPD", lambda: FakeEPD(log), raising=False)
    monkeypatch.setattr(ibm.epdconfig, "module_exit",
                        lambda *a, **k: log.append("module_exit"), raising=False)
    return log


def _panel(log):
    p = ibm.Panel()
    log.clear()                 # construction noise; each test drives its own
    return p


# --- the EBUSY retry ------------------------------------------------------------

def test_a_busy_gpio_line_is_released_and_the_init_retried(panel_env):
    p = _panel(panel_env)
    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) == 1:
            err = GPIOError("Opening GPIO line 25: Device or resource busy")
            err.errno = 16      # what periphery sets on an EBUSY line request
            raise err

    p._safe_init_call(fn)
    assert len(attempts) == 2, "the second attempt never happened"
    assert "module_exit" in panel_env, "the held lines were not released first"


def test_a_non_ebusy_gpio_error_is_not_retried(panel_env):
    """Only EBUSY means "someone still holds the line". Retrying a genuine wiring
    or permission fault would just double the delay before reporting it."""
    p = _panel(panel_env)
    attempts = []

    def fn():
        attempts.append(1)
        err = GPIOError("Opening GPIO line 25: Permission denied")
        err.errno = 13
        raise err

    with pytest.raises(GPIOError):
        p._safe_init_call(fn)
    assert len(attempts) == 1


def test_an_unrelated_exception_is_not_retried(panel_env):
    p = _panel(panel_env)
    attempts = []

    def fn():
        attempts.append(1)
        raise RuntimeError("display init failed: something else")

    with pytest.raises(RuntimeError):
        p._safe_init_call(fn)
    assert len(attempts) == 1


# --- the API-variant resolution -------------------------------------------------

def test_every_driver_call_resolves_to_the_v4_name(panel_env):
    """epd2in13_V4 has none of the primary names, so all four fall back."""
    p = _panel(panel_env)
    assert p._init_full0 is None                      # no init_Full on V4
    assert p._init_part0.__name__ == "init_fast"
    assert p._disp_base.__name__ == "displayPartBaseImage"
    assert p._disp_part.__name__ == "displayPartial"
    assert p._disp.__name__ == "display"


def test_construction_releases_stale_lines_before_touching_the_panel(monkeypatch):
    """A previous daemon instance may still hold the gpiochip lines through its
    stop timeout, so the first thing Panel does is let them go."""
    log = []
    monkeypatch.setattr(ibm.epd2in13_V4, "EPD", lambda: FakeEPD(log), raising=False)
    monkeypatch.setattr(ibm.epdconfig, "module_exit",
                        lambda *a, **k: log.append("module_exit"), raising=False)
    ibm.Panel()
    assert log[0] == "module_exit"
    assert log.index("module_exit") < log.index("init")


def test_construction_leaves_the_panel_in_full_mode(panel_env):
    p = ibm.Panel()
    assert p._mode == "full"
    assert "Clear" in panel_env


# --- mode transitions -----------------------------------------------------------

def test_a_redundant_full_init_is_skipped(panel_env):
    """Re-initialising costs a hardware reset and a visible flash."""
    p = _panel(panel_env)
    p._init_full()
    assert panel_env == []


def test_a_redundant_partial_init_is_skipped(panel_env):
    p = _panel(panel_env)
    p._init_part()
    assert "init_fast" in panel_env
    panel_env.clear()
    p._init_part()
    assert panel_env == []


def test_prepare_partial_sets_the_base_once_then_goes_partial(panel_env):
    p = _panel(panel_env)
    p.prepare_partial()
    assert panel_env == ["base", "init_fast", "partial"]
    assert p._partial_ready is True


def test_a_later_partial_refresh_does_not_reset_the_base(panel_env):
    """Re-sending the base image is a full refresh in disguise."""
    p = _panel(panel_env)
    p.prepare_partial()
    panel_env.clear()
    p._display_set_base_then_partial(object())
    assert "base" not in panel_env
    assert panel_env == ["partial"]


def test_a_full_refresh_forces_the_next_partial_to_re_base(panel_env):
    """After a full refresh the panel's partial reference frame is gone, so the
    next partial has to set the base again or it renders against stale pixels."""
    p = _panel(panel_env)
    p.prepare_partial()
    p._display_full(object())
    assert p._partial_ready is False
    panel_env.clear()
    p._display_set_base_then_partial(object())
    assert "base" in panel_env
