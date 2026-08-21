#!/usr/bin/env python3
"""uipolicy.py - e-ink screen-handover decisions for the display daemon.

Two decisions that used to be inline conditionals in iosbackupmachine.py, pulled
out here because that module imports periphery / PIL / waveshare_epd at import
time and so cannot be unit-tested:

- ``should_release_hold`` — when the post-backup result screen may be given up.
- ``InfoWindow``          — how long the single-tap info screen stays up, and
                            what goes back on the panel afterwards.

Import-safe: stdlib only, no hardware modules and no clock reads (callers pass
``now``), so it unit-tests on any machine.
"""

# How long a single tap keeps the system-info screen on the panel.
INFO_SCREEN_SEC = 30


def should_release_hold(device_present, sync_running, manual_start):
    """True when the daemon should stop holding the post-backup result screen.

    After a backup the result stays up until the phone is unplugged, so the same
    device isn't immediately backed up again. But the wait used to watch only the
    cable: a sync started from the web UI, or a fresh backup request, was
    invisible for as long as the phone stayed plugged in, freezing the panel on
    the previous result. Both of those are work the user explicitly asked for, so
    they take the screen; an idle plugged-in phone still holds it.
    """
    if not device_present:
        return True
    return bool(sync_running or manual_start)


def merge_resume(fallback, pending):
    """Screen state to go back to when the info window closes.

    ``fallback`` is the screen that was up before the tap; ``pending`` is what
    the daemon tried to draw while the info screen held the panel. Queueing
    rather than discarding matters for one-shot screens: a tap in the last
    seconds of a backup would otherwise lose the "Backup complete" result,
    because nothing repaints it a second time. Newest wins, key by key.
    """
    merged = dict(fallback or {})
    merged.update(pending or {})
    return merged


class InfoWindow:
    """The single-tap system-info screen: its lifetime and what follows it.

    The window is the sole authority on whether the info screen is up. Active
    operations used to cancel it and repaint immediately, which erased the info
    screen about half a second after the tap — so a tap during a sync looked
    like it did nothing. Now a tap always wins for ``duration`` seconds, and the
    screen that was up beforehand is put back exactly once when it expires.
    """

    def __init__(self, duration=INFO_SCREEN_SEC):
        self.duration = duration
        self._until = 0.0
        self._restore = None

    def open(self, now, previous_state):
        """Start the window, remembering the screen to return to.

        Tapping again while it is up extends the window but keeps the *original*
        screen to restore — re-capturing would save the info screen itself and
        strand the user on it.
        """
        if not self.active(now):
            self._restore = previous_state
        self._until = now + self.duration

    def active(self, now):
        """True while the info screen should stay on the panel."""
        return now < self._until

    def cancel(self):
        """Drop the window and its pending restore.

        For an operation that takes the panel for a long stretch (a backup):
        without this, the pre-tap screen would be repainted over live progress
        when the window would otherwise have expired.
        """
        self._until = 0.0
        self._restore = None

    def take_restore(self, now):
        """The screen to repaint now that the window has closed, or None.

        Handed out once: repainting it every tick afterwards would fight
        whatever the daemon has since decided to show.
        """
        if self._restore is None or self.active(now):
            return None
        state, self._restore = self._restore, None
        self._until = 0.0
        return state
