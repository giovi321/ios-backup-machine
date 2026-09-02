"""Tests for notification delivery surviving process exit.

The bug these cover: send_notification dispatches on daemon threads and returns
immediately, and backup-sync.py calls sys.exit(0) right after reporting its
result. Python terminates daemon threads at interpreter exit, so the webhook
POST was killed mid-request and every notification a manual sync produced was
lost with no error anywhere. flush() waits for the in-flight deliveries.
"""
import subprocess
import sys
import threading
import time

import notifications


def setup_function():
    with notifications._pending_lock:
        notifications._pending[:] = []


def test_flush_waits_for_an_in_flight_delivery():
    done = []

    def slow():
        time.sleep(0.4)
        done.append(True)

    notifications._spawn(slow, ())
    assert notifications.flush(timeout=5) is True
    assert done == [True]


def test_flush_returns_false_when_a_delivery_outlasts_the_timeout():
    stop = threading.Event()
    notifications._spawn(lambda: stop.wait(30), ())
    try:
        assert notifications.flush(timeout=0.2) is False
    finally:
        stop.set()


def test_flush_with_nothing_pending_returns_immediately():
    started = time.time()
    assert notifications.flush(timeout=5) is True
    assert time.time() - started < 0.5


def test_flush_waits_for_several_deliveries():
    done = []
    for _ in range(3):
        notifications._spawn(lambda: (time.sleep(0.2), done.append(True)), ())
    assert notifications.flush(timeout=5) is True
    assert len(done) == 3


def test_the_pending_list_does_not_grow_without_bound():
    """Finished threads are reaped on the next spawn, so a long-running daemon
    sending thousands of notifications does not leak thread objects."""
    for _ in range(20):
        notifications._spawn(lambda: None, ())
        time.sleep(0.01)
    notifications.flush(timeout=5)
    with notifications._pending_lock:
        assert len(notifications._pending) == 0


def test_delivery_threads_stay_daemonic():
    """A wedged broker must never be able to hold the process open forever;
    flush() bounds the wait instead."""
    stop = threading.Event()
    t = notifications._spawn(lambda: stop.wait(30), ())
    try:
        assert t.daemon is True
    finally:
        stop.set()


# --- the real thing: a process that exits immediately after notifying --------

_EXIT_SCRIPT = """
import sys, time
sys.path.insert(0, {app!r})
import notifications

RESULT = {result!r}

def slow_send(*a):
    time.sleep(0.5)
    open(RESULT, "w").write("delivered")

notifications._spawn(slow_send, ())
{flush}
sys.exit(0)
"""


def _run_exit_script(tmp_path, flush_line):
    import os
    app = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
    result = str(tmp_path / "result.txt")
    script = tmp_path / "exiter.py"
    script.write_text(_EXIT_SCRIPT.format(app=app, result=result, flush=flush_line))
    subprocess.run([sys.executable, str(script)], timeout=30, check=True)
    try:
        return open(result).read()
    except FileNotFoundError:
        return ""


def test_atexit_saves_a_caller_that_forgets_to_flush(tmp_path):
    """flush() is registered with atexit, which runs before daemon threads are
    killed — so even a caller that exits without flushing still delivers."""
    assert _run_exit_script(tmp_path, "") == "delivered"


def test_an_explicit_flush_delivers_too(tmp_path):
    assert _run_exit_script(tmp_path, "notifications.flush()") == "delivered"


def test_without_the_fix_the_delivery_would_be_lost(tmp_path):
    """Pins the actual failure mode: an untracked daemon thread is killed at
    exit. If this ever starts passing, daemon threads are no longer being
    terminated and the reasoning behind flush() needs revisiting."""
    import os
    app = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
    result = str(tmp_path / "lost.txt")
    script = tmp_path / "unpatched.py"
    script.write_text(
        "import sys, threading, time\n"
        f"sys.path.insert(0, {app!r})\n"
        "def slow():\n"
        "    time.sleep(0.5)\n"
        f"    open({result!r}, 'w').write('delivered')\n"
        "threading.Thread(target=slow, daemon=True).start()\n"
        "sys.exit(0)\n"
    )
    subprocess.run([sys.executable, str(script)], timeout=30, check=True)
    assert not os.path.exists(result)
