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
    assert "> /log/update.log 2>&1" in argv[-1]


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
