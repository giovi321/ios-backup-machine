"""Structural invariants of install.sh.

These are text assertions over the script, not behavioural tests: nothing here
runs install.sh, which needs root, systemd and a package manager. They exist to
pin invariants that were violated in production and that no other test guards,
so they are deliberately narrow. A reformat of install.sh can break them without
anything being wrong, in which case update the test, not the script.
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL_SH = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
DAEMON_PY = open(os.path.join(ROOT, "app", "iosbackupmachine.py"), encoding="utf-8").read()


def _top_level_block(text, opener):
    """Lines of the top-level `if` block introduced by `opener`, up to its `fi`."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if opener in l)
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start:end])


def _trap_handler(text, signal):
    """Name of the single function bound to `signal`, asserting there is one."""
    names = re.findall(r"^trap\s+(\w+)\s+" + signal + r"\s*$", text, re.M)
    assert len(names) == 1, f"expected exactly one {signal} trap, found {names}"
    return names[0]


def _function_body(text, name):
    """Body of a top-level shell function, up to its closing brace at column 0."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(name + "()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end])


def _bare_reboot_lines(text):
    """Indices of lines whose whole content is the `reboot` command."""
    return [i for i, l in enumerate(text.splitlines()) if l.strip() == "reboot"]


def test_the_reboot_epoch_is_recorded_only_when_the_health_checks_passed():
    # An install that failed its health checks must not claim its reboot epoch:
    # the next update compares against it and would skip a reboot that is owed.
    # .installed_version is already guarded this way; the epoch was not.
    block = _top_level_block(INSTALL_SH, '[ "${HEALTH_OK}" = true ]')
    assert ".reboot_epoch" in block


def test_there_is_exactly_one_exit_trap():
    # bash keeps one handler per signal, so a second `trap ... EXIT` silently
    # replaces the first and its cleanup never runs. Giving the sentinel its own
    # trap is the obvious implementation and it is the broken one.
    assert len(re.findall(r"^trap\s+\w+\s+EXIT\s*$", INSTALL_SH, re.M)) == 1


def test_the_exit_trap_clears_the_updating_sentinel():
    # set -euo pipefail means a failure at any step exits before the end of the
    # script, and that is exactly the path that strands the e-ink on "Updating".
    # Only the exit handler covers it, so the removal has to live in that
    # function rather than in one of its own.
    body = _function_body(INSTALL_SH, _trap_handler(INSTALL_SH, "EXIT"))
    assert "UPDATING_FILE" in body
    assert "REBOOTING" in body


def test_the_sentinel_survives_an_installer_reboot():
    # The panel is deliberately left showing "Updating" as the last painted
    # frame through the reboot, so the cleanup must stand down when rebooting.
    lines = INSTALL_SH.splitlines()
    reboots = _bare_reboot_lines(INSTALL_SH)
    assert reboots, "no bare `reboot` call found; the guard below is checking nothing"
    for i in reboots:
        preceding = [l.strip() for l in lines[max(0, i - 3):i] if l.strip()]
        assert any(l == "REBOOTING=1" for l in preceding), (
            f"line {i + 1}: `reboot` is not preceded by REBOOTING=1")


def test_the_installer_and_the_daemon_agree_on_the_sentinel_path():
    # Two files, one path. If they drift, the installer clears a file nobody
    # reads and the daemon keeps painting "Updating".
    assert 'UPDATING_FILE="${RUNTIME_DIR}/updating"' in INSTALL_SH
    assert 'UPDATING_FILE = os.path.join(RUNTIME_DIR, "updating")' in DAEMON_PY
