"""Unit tests for uipolicy.py — e-ink screen-handover decisions.

The display daemon itself cannot be imported (it pulls in periphery / PIL /
waveshare_epd at module level), so the decisions that used to be inline
conditionals live here, where they can be tested without hardware.
"""
import uipolicy


# --- should_release_hold ----------------------------------------------------
# After a backup the daemon holds the result screen so the same phone isn't
# immediately backed up again. It must still let go for work the user asked for.

def test_unplugging_the_phone_releases_the_result_screen():
    assert uipolicy.should_release_hold(device_present=False, sync_running=False,
                                        manual_start=False) is True


def test_a_still_plugged_phone_keeps_the_result_screen():
    assert uipolicy.should_release_hold(device_present=True, sync_running=False,
                                        manual_start=False) is False


def test_a_sync_starting_takes_the_screen_from_the_old_result():
    # The bug: a sync launched from the web UI while the phone was still
    # plugged in left the previous "Sync failed" screen up for its whole run.
    assert uipolicy.should_release_hold(device_present=True, sync_running=True,
                                        manual_start=False) is True


def test_an_explicit_backup_request_takes_the_screen():
    assert uipolicy.should_release_hold(device_present=True, sync_running=False,
                                        manual_start=True) is True


# --- InfoWindow -------------------------------------------------------------

def test_a_tap_puts_the_info_screen_up_for_the_configured_time():
    w = uipolicy.InfoWindow(duration=30)
    w.open(now=100.0, previous_state={"screen": "boot"})
    assert w.active(now=100.0) is True
    assert w.active(now=129.9) is True
    assert w.active(now=130.1) is False


def test_the_previous_screen_is_not_restored_while_the_window_is_up():
    w = uipolicy.InfoWindow(duration=30)
    w.open(now=100.0, previous_state={"screen": "boot"})
    assert w.take_restore(now=110.0) is None


def test_the_previous_screen_comes_back_once_the_window_ends():
    w = uipolicy.InfoWindow(duration=30)
    w.open(now=100.0, previous_state={"screen": "normal", "subtitle": "Syncing..."})
    assert w.take_restore(now=131.0) == {"screen": "normal", "subtitle": "Syncing..."}


def test_the_restore_is_handed_out_only_once():
    # Otherwise the daemon would repaint the stale screen on every tick,
    # fighting whatever is actually current.
    w = uipolicy.InfoWindow(duration=30)
    w.open(now=100.0, previous_state={"screen": "boot"})
    assert w.take_restore(now=131.0) is not None
    assert w.take_restore(now=132.0) is None


def test_tapping_again_extends_the_window_but_keeps_the_original_screen():
    # Restoring to "info" would strand the user on the info screen.
    w = uipolicy.InfoWindow(duration=30)
    w.open(now=100.0, previous_state={"screen": "boot"})
    w.open(now=120.0, previous_state={"screen": "info"})
    assert w.active(now=145.0) is True
    assert w.take_restore(now=151.0) == {"screen": "boot"}


def test_an_untouched_window_has_nothing_to_restore():
    w = uipolicy.InfoWindow(duration=30)
    assert w.active(now=0.0) is False
    assert w.take_restore(now=0.0) is None


def test_a_backup_taking_over_cancels_the_window_and_its_restore():
    # A backup owns the panel for minutes. If the window survived it, the
    # pre-tap screen would later be repainted over live backup progress.
    w = uipolicy.InfoWindow(duration=30)
    w.open(now=100.0, previous_state={"screen": "boot"})
    w.cancel()
    assert w.active(now=105.0) is False
    assert w.take_restore(now=131.0) is None


def test_cancelling_an_unopened_window_is_harmless():
    w = uipolicy.InfoWindow(duration=30)
    w.cancel()
    assert w.active(now=0.0) is False


# --- merge_resume -----------------------------------------------------------
# Updates made while the info screen was up are queued, not dropped, so a
# terminal screen (e.g. "Backup complete") drawn during the window still lands.

def test_with_nothing_queued_the_pre_tap_screen_comes_back():
    assert uipolicy.merge_resume({"screen": "boot", "subtitle": ""}, {}) == {
        "screen": "boot", "subtitle": ""}


def test_a_screen_drawn_during_the_window_wins_over_the_pre_tap_screen():
    merged = uipolicy.merge_resume(
        {"screen": "normal", "subtitle": "Backing up... 87%", "percent": 87},
        {"screen": "complete", "subtitle": "", "center_block": "Backup complete"})
    assert merged["screen"] == "complete"
    assert merged["center_block"] == "Backup complete"


def test_keys_not_touched_during_the_window_are_kept():
    merged = uipolicy.merge_resume({"screen": "normal", "show_header": True},
                                   {"subtitle": "new"})
    assert merged == {"screen": "normal", "show_header": True, "subtitle": "new"}


def test_merge_resume_does_not_mutate_its_inputs():
    fallback = {"screen": "boot"}
    pending = {"screen": "complete"}
    uipolicy.merge_resume(fallback, pending)
    assert fallback == {"screen": "boot"}
    assert pending == {"screen": "complete"}


def test_merge_resume_tolerates_missing_state():
    assert uipolicy.merge_resume(None, None) == {}


def test_a_sync_no_longer_cancels_the_info_screen():
    # The old code zeroed the window when a sync tick arrived, wiping the info
    # screen ~0.5s after the tap. The window is now the sole authority.
    w = uipolicy.InfoWindow(duration=30)
    w.open(now=100.0, previous_state={"screen": "normal", "subtitle": "Syncing..."})
    assert w.active(now=105.0) is True
