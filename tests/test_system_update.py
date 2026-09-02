"""How a web-UI-triggered update is launched.

The bug these cover: the updater used to be a plain Popen child of
webui.service. install.sh restarts that unit partway through, and webui.service
has no KillMode=process, so systemd killed the whole cgroup - updater included -
after every service had been stopped but before any were restarted. The device
came back with the display daemon and the WireGuard reconciler down, which is
what took it off the VPN. It also never reached the reboot, because both shell
prompts read EOF from the non-tty and answered "no".
"""
import os
import sys
import tempfile

import pytest

SANDBOX = tempfile.mkdtemp(prefix="ibm_update_")
os.environ.setdefault("IOSBACKUP_RUNTIME_DIR", os.path.join(SANDBOX, "rt"))
os.environ.setdefault("IOSBACKUP_LOG_DIR", os.path.join(SANDBOX, "log"))
os.environ.setdefault("IOSBACKUP_CONFIG", os.path.join(SANDBOX, "config.yaml"))

try:
    import webui
except Exception as exc:  # pragma: no cover - environment without flask
    pytest.skip(f"webui not importable: {exc}", allow_module_level=True)


# --- how the updater is launched --------------------------------------------

def test_the_updater_runs_in_its_own_transient_unit():
    argv = webui._update_launch_command("/repo/update.sh", "/log/update.log",
                                        have_systemd_run=True)
    assert argv[0] == "systemd-run"
    # Outside webui.service's cgroup, so restarting the web UI can't kill it.
    assert "--unit=iosbackup-update" in argv
    # Reaps the unit afterwards, including a failed one from a previous attempt.
    assert "--collect" in argv


def test_the_updater_gets_the_non_interactive_and_auto_reboot_flags():
    argv = webui._update_launch_command("/repo/update.sh", "/log/update.log",
                                        have_systemd_run=True)
    assert "--setenv=IOSBACKUP_NONINTERACTIVE=1" in argv
    assert "--setenv=IOSBACKUP_AUTO_REBOOT=1" in argv
    assert "--setenv=IOSBACKUP_SKIP_VERSION_CHECK=1" in argv
    # Same set goes into the process env for the fallback path.
    assert webui.UPDATE_ENV["IOSBACKUP_NONINTERACTIVE"] == "1"
    assert webui.UPDATE_ENV["IOSBACKUP_AUTO_REBOOT"] == "1"


def test_output_still_lands_in_the_update_log():
    argv = webui._update_launch_command("/repo/update.sh", "/log/update.log",
                                        have_systemd_run=True)
    # Redirected by the inner shell rather than StandardOutput=append:, so it
    # does not depend on the systemd version on the device.
    assert argv[-3:-1] == ["bash", "-c"]
    assert "/repo/update.sh" in argv[-1]
    assert "> /log/update.log" in argv[-1]
    assert "2>&1" in argv[-1]


def test_update_output_is_timestamped_line_by_line():
    """update.log is raw shell output, so it goes through the logutil stamper —
    otherwise it is the one log that cannot be lined up against the others."""
    argv = webui._update_launch_command("/repo/update.sh", "/log/update.log",
                                        have_systemd_run=True)
    inner = argv[-1]
    assert "--stamp" in inner
    assert "logutil.py" in inner


def test_the_updates_exit_status_survives_the_stamper_pipeline():
    """Without pipefail the unit would report the stamper's success and mask a
    failed update."""
    argv = webui._update_launch_command("/repo/update.sh", "/log/update.log",
                                        have_systemd_run=True)
    assert "set -o pipefail" in argv[-1]


def test_paths_with_spaces_are_quoted_for_the_inner_shell():
    argv = webui._update_launch_command("/re po/update.sh", "/lo g/update.log",
                                        have_systemd_run=True)
    assert "'/re po/update.sh'" in argv[-1]
    assert "'/lo g/update.log'" in argv[-1]


def test_without_systemd_run_it_falls_back_to_a_bare_bash():
    argv = webui._update_launch_command("/repo/update.sh", "/log/update.log",
                                        have_systemd_run=False)
    assert argv == ["bash", "/repo/update.sh"]


# --- the e-ink sentinel ------------------------------------------------------

def test_marking_an_update_creates_the_sentinel_the_daemon_watches(tmp_path):
    marker = tmp_path / "rt" / "updating"
    assert webui._mark_updating(str(marker)) is True
    assert marker.exists()


def test_clearing_the_sentinel_removes_it(tmp_path):
    marker = tmp_path / "updating"
    webui._mark_updating(str(marker))
    webui._clear_updating(str(marker))
    assert not marker.exists()


def test_clearing_a_sentinel_that_is_not_there_is_not_an_error(tmp_path):
    webui._clear_updating(str(tmp_path / "nope"))


def test_a_sentinel_that_cannot_be_written_does_not_stop_the_update(tmp_path):
    # A file where the parent directory should be: makedirs fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    assert webui._mark_updating(str(blocker / "sub" / "updating")) is False


# --- dashboard ETA formatting ------------------------------------------------

def test_human_duration_matches_the_sync_log_format():
    """The dashboard and the sync log quote the same ETA, so they must format it
    the same way."""
    import sync_manager
    for seconds in (0, 45, 60, 3599, 3600, 30962):
        assert webui._human_duration(seconds) == sync_manager.fmt_duration(seconds)


def test_human_duration_is_blank_for_a_missing_value():
    assert webui._human_duration(None) == ""
    assert webui._human_duration("") == ""


# --- connectivity monitor -----------------------------------------------------
# The dashboard needs a constantly-refreshed internet state, but the probe blocks
# for seconds and the page polls every 5s, so the answer has to be cached.

def test_monitor_reports_unknown_before_the_first_probe():
    """None means "not checked yet" and must not be rendered as offline."""
    m = webui.ConnectivityMonitor()
    snap = m.snapshot()
    assert snap["online"] is None
    assert snap["age_seconds"] is None


def test_probe_caches_its_result(monkeypatch):
    m = webui.ConnectivityMonitor()
    monkeypatch.setattr(webui.netutil, "have_connectivity", lambda timeout=4: True)
    assert m.probe_once() is True
    snap = m.snapshot()
    assert snap["online"] is True
    assert snap["age_seconds"] == 0


def test_a_raising_probe_reads_as_offline_not_as_a_crash(monkeypatch):
    m = webui.ConnectivityMonitor()
    def boom(timeout=4):
        raise OSError("no route to host")
    monkeypatch.setattr(webui.netutil, "have_connectivity", boom)
    assert m.probe_once() is False
    assert m.snapshot()["online"] is False


def test_since_resets_only_when_the_state_actually_changes(monkeypatch):
    """The iPhone hotspot dropping mid-sync is exactly the transition this card
    exists to show, so the timer must restart on a change and not otherwise."""
    m = webui.ConnectivityMonitor()
    state = {"up": True}
    monkeypatch.setattr(webui.netutil, "have_connectivity", lambda timeout=4: state["up"])

    m.probe_once()
    first_change = m._changed_at
    m.probe_once()                      # still online
    assert m._changed_at == first_change

    state["up"] = False
    m.probe_once()                      # link dropped
    assert m._changed_at > first_change
    assert m.snapshot()["online"] is False


def test_snapshot_reports_the_probe_interval():
    assert webui.ConnectivityMonitor(interval=30).snapshot()["interval_seconds"] == 30


def test_ensure_started_is_idempotent(monkeypatch):
    monkeypatch.setattr(webui.netutil, "have_connectivity", lambda timeout=4: True)
    m = webui.ConnectivityMonitor(interval=3600)
    m.ensure_started()
    first = m._thread
    m.ensure_started()
    assert m._thread is first


def test_importing_the_module_starts_no_probe_thread():
    """Importing webui must not open sockets, or the suite would depend on the
    network; the loop starts on first request instead."""
    assert webui.connectivity._thread is None or webui.connectivity._thread.is_alive()


def _idle_monitor(monkeypatch, probe_result):
    """A monitor with its background loop stubbed out, so a test observes only
    the cache logic and never races the probe thread."""
    monkeypatch.setattr(webui.netutil, "have_connectivity",
                        lambda timeout=4: probe_result[0])
    m = webui.ConnectivityMonitor(interval=36000)
    monkeypatch.setattr(m, "ensure_started", lambda: None)
    return m


def test_health_answers_from_a_fresh_cache(monkeypatch):
    probe = [True]
    m = _idle_monitor(monkeypatch, probe)
    m.probe_once()               # cache now says online
    probe[0] = False             # the world changes, but the cache is still fresh
    assert webui._health_internet(m) is True


def test_health_reprobes_once_the_cache_is_stale(monkeypatch):
    probe = [True]
    m = _idle_monitor(monkeypatch, probe)
    m.probe_once()
    m._checked_at -= m.interval * 5      # age it past the freshness window
    probe[0] = False
    assert webui._health_internet(m) is False


def test_health_probes_when_nothing_is_cached_yet(monkeypatch):
    """A monitor that has never probed must not hand None to a caller that
    expects a boolean."""
    m = _idle_monitor(monkeypatch, [False])
    assert webui._health_internet(m) is False
