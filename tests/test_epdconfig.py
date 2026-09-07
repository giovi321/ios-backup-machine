"""Tests for the e-paper HAL (app/epdconfig.py) against a fake gpiochip.

Two contracts here are easy to break and impossible to notice without hardware,
because nothing else in the suite constructs a Panel:

1. The waveshare epd2in13_V4 driver calls ``epdconfig.module_init()`` from BOTH
   ``init()`` and ``init_fast()``, and Panel uses both (full mode, then partial
   mode) during startup. A kernel gpiochip refuses a second request for a line
   the same process already holds (EBUSY), so re-init must work, and any error
   it does raise must stay a GPIOError with .errno intact — that is what
   Panel._safe_init_call keys off to release the lines and retry.

2. epd2in13_V4.ReadBusy() loops ``while digital_read(busy_pin) == 1`` and
   documents ``0: idle, 1: busy``. The BUSY watchdog must therefore count time
   spent reading 1, not 0. Getting it backwards makes an idle panel — its
   resting state — look stuck.

The real python-periphery needs Linux and actual hardware, so a fake gpiochip
stands in unconditionally.
"""
import importlib.util
import os
import sys
import types

import pytest

EPDCONFIG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "epdconfig.py"))


class FakeGPIOError(OSError):
    def __init__(self, err, msg):
        super().__init__(msg)
        self.errno = err


class FakeChip:
    """Models the one gpiochip behaviour that matters: a line can be requested
    by only one holder at a time (kernel returns EBUSY otherwise)."""
    def __init__(self):
        self.held = set()
        self.busy_level = False   # what the BUSY pin reads; False == LOW == idle
        self.unavailable = set()  # lines a foreign holder owns, so we never get them


def _fake_periphery(chip):
    class GPIO:
        def __init__(self, path, line, direction):
            if line in chip.held or line in chip.unavailable:
                raise FakeGPIOError(16, f"Opening GPIO line {line}: Device or resource busy")
            chip.held.add(line)
            self.line = line
        def read(self):
            return chip.busy_level
        def write(self, value):
            pass
        def close(self):
            chip.held.discard(self.line)

    class SPI:
        def __init__(self, *args):
            self.open = True
        def transfer(self, tx):
            return tx
        def close(self):
            self.open = False

    pkg = types.ModuleType("periphery")
    pkg.GPIO, pkg.SPI = GPIO, SPI
    sub = types.ModuleType("periphery.gpio")
    sub.GPIOError = FakeGPIOError
    pkg.gpio = sub
    return pkg, sub


@pytest.fixture
def epd(monkeypatch):
    """A freshly imported epdconfig bound to a fresh fake gpiochip, so module
    globals (_busy_since, the handles) never leak between tests."""
    chip = FakeChip()
    pkg, sub = _fake_periphery(chip)
    monkeypatch.setitem(sys.modules, "periphery", pkg)
    monkeypatch.setitem(sys.modules, "periphery.gpio", sub)

    spec = importlib.util.spec_from_file_location("epdconfig_under_test", EPDCONFIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.chip = chip
    yield mod
    try:
        mod.module_exit()
    except Exception:
        pass


@pytest.fixture
def clock(monkeypatch, epd):
    """Drive epdconfig's monotonic clock by hand."""
    now = [1000.0]
    monkeypatch.setattr(epd, "time", types.SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda s: None))
    return now


# --- module_init / module_exit ---------------------------------------------

def test_reinit_while_lines_are_held_succeeds(epd):
    """Panel startup is init() then init_fast(); both call module_init()."""
    assert epd.module_init() == 0
    assert epd.module_init() == 0
    assert epd.chip.held == {epd.PIN_DC, epd.PIN_RST, epd.PIN_BUSY, epd.PIN_PWR}


def test_reinit_leaves_working_handles(epd):
    epd.module_init()
    epd.module_init()
    assert epd._spi is not None
    assert None not in (epd._gpio_dc, epd._gpio_rst, epd._gpio_busy)
    epd.digital_write(epd.PIN_DC, 1)          # would raise if the handle were dead
    assert epd.digital_read(epd.PIN_BUSY) == 0


def test_unavailable_line_raises_gpioerror_with_errno(epd):
    """Panel._safe_init_call retries on `except GPIOError` + errno == 16.
    Wrapping the error in another type silently disables that recovery."""
    epd.chip.unavailable.add(epd.PIN_DC)
    with pytest.raises(FakeGPIOError) as exc:
        epd.module_init()
    assert exc.value.errno == 16


def test_failed_init_releases_every_line_it_opened(epd):
    epd.chip.unavailable.add(epd.PIN_BUSY)
    with pytest.raises(OSError):
        epd.module_init()
    assert epd.chip.held == set()
    assert epd._spi is None


def test_optional_power_pin_may_be_missing(epd):
    epd.chip.unavailable.add(epd.PIN_PWR)
    assert epd.module_init() == 0
    assert epd._gpio_pwr is None


# --- digital_read BUSY watchdog --------------------------------------------

def test_idle_panel_never_reports_stuck(epd, clock):
    """LOW is 0 is idle for epd2in13_V4 — its resting state between refreshes.
    A static screen can hold for minutes; that is not a stuck panel."""
    epd.module_init()
    epd.chip.busy_level = False
    assert epd.digital_read(epd.PIN_BUSY) == 0
    for _ in range(10):
        clock[0] += 60.0
        assert epd.digital_read(epd.PIN_BUSY) == 0


def test_stuck_busy_panel_raises_after_the_timeout(epd, clock):
    epd.module_init()
    epd.chip.busy_level = True
    assert epd.digital_read(epd.PIN_BUSY) == 1     # arms the timer
    clock[0] += epd.BUSY_TIMEOUT_SEC / 2
    assert epd.digital_read(epd.PIN_BUSY) == 1     # still inside the budget
    clock[0] += epd.BUSY_TIMEOUT_SEC
    with pytest.raises(RuntimeError, match="stuck busy"):
        epd.digital_read(epd.PIN_BUSY)


def test_normal_refresh_clears_the_timer(epd, clock):
    """A refresh is: busy for a couple of seconds, then idle. Repeating that
    for longer than the timeout must never trip it."""
    epd.module_init()
    for _ in range(5):
        epd.chip.busy_level = True
        for _ in range(3):
            clock[0] += 1.0
            assert epd.digital_read(epd.PIN_BUSY) == 1
        epd.chip.busy_level = False
        clock[0] += 1.0
        assert epd.digital_read(epd.PIN_BUSY) == 0
        clock[0] += 30.0                            # screen held static


def test_reinit_forgets_a_previous_sessions_busy_timer(epd, clock):
    epd.module_init()
    epd.chip.busy_level = True
    epd.digital_read(epd.PIN_BUSY)
    clock[0] += epd.BUSY_TIMEOUT_SEC * 10
    epd.module_init()                               # panel reset; timer starts over
    assert epd.digital_read(epd.PIN_BUSY) == 1
