"""Tests for the sync log's content: output-line classification, the rolling
rate/ETA estimator, the throttled progress writer, command redaction and the
structured failure payload that goes to MQTT / webhook.

All pure — no rsync, no SSH, no device. The rsync output samples are verbatim
from rsync 3.4.1 run with the production flag set, not invented: the itemize
strings in particular (">f+++++++++", "cd+++++++++", "cL+++++++++") are what
decides whether an entry becomes a [FILE] line.
"""
import sync_manager


# ---------------------------------------------------------------------------
# fmt helpers
# ---------------------------------------------------------------------------

def test_fmt_bytes_matches_the_display_daemon_format():
    assert sync_manager.fmt_bytes(0) == "0 B"
    assert sync_manager.fmt_bytes(512) == "512 B"
    assert sync_manager.fmt_bytes(1536) == "1.5 KB"
    assert sync_manager.fmt_bytes(64 * 1024**3) == "64.0 GB"


def test_fmt_duration_is_compact_and_scales():
    assert sync_manager.fmt_duration(0) == "0s"
    assert sync_manager.fmt_duration(45) == "45s"
    assert sync_manager.fmt_duration(60) == "1m"
    assert sync_manager.fmt_duration(3599) == "59m"
    assert sync_manager.fmt_duration(3600) == "1h00m"
    assert sync_manager.fmt_duration(7523) == "2h05m"


# ---------------------------------------------------------------------------
# classify_output_line — the three-way split on rsync's merged pipe
# ---------------------------------------------------------------------------

def test_progress_lines_are_classified_as_progress():
    kind, value = sync_manager.classify_output_line(
        "  1,234,567  45%    1.20MB/s    0:01:23")
    assert kind == "progress"
    assert value is None


def test_sentinel_lines_are_classified_as_file_with_size_and_name():
    kind, value = sync_manager.classify_output_line(
        "@f >f+++++++++ 1288490188 00008030-ABC/Snapshot/4f/4fa8c2e1")
    assert kind == "file"
    assert value == {"name": "00008030-ABC/Snapshot/4f/4fa8c2e1", "size": 1288490188}


def test_file_names_may_contain_spaces():
    kind, value = sync_manager.classify_output_line("@f >f+++++++++ 42 dir/my file name.plist")
    assert kind == "file"
    assert value == {"name": "dir/my file name.plist", "size": 42}


def test_file_line_with_unparsable_size_still_yields_the_name():
    kind, value = sync_manager.classify_output_line("@f >f+++++++++ ? weird/entry")
    assert kind == "file"
    assert value == {"name": "weird/entry", "size": 0}


# The itemize strings below are verbatim from rsync 3.4.1 run with the production
# flag set; rsync reports directories and symlinks through --out-format too, and
# either would displace the real file in the [FILE] line if it were not skipped.

def test_directory_entries_are_skipped():
    assert sync_manager.classify_output_line("@f cd+++++++++ 4096 sub/")[0] == "skip"
    assert sync_manager.classify_output_line("@f .d..t...... 4096 ./")[0] == "skip"


def test_symlink_entries_are_skipped():
    assert sync_manager.classify_output_line("@f cL+++++++++ 8 link_to_big1")[0] == "skip"


def test_device_and_special_entries_are_skipped():
    assert sync_manager.classify_output_line("@f cD+++++++++ 0 dev/null")[0] == "skip"
    assert sync_manager.classify_output_line("@f cS+++++++++ 0 run/sock")[0] == "skip"


def test_a_malformed_sentinel_line_is_skipped_rather_than_raising():
    assert sync_manager.classify_output_line("@f ")[0] == "skip"
    assert sync_manager.classify_output_line("@f x")[0] == "skip"


def test_a_bare_at_f_is_not_a_sentinel_line():
    """Only the sentinel with its trailing space marks rsync's own name output;
    a message that merely starts with those two characters is still a message."""
    assert sync_manager.classify_output_line("@f")[0] == "message"


def test_rsync_errors_are_classified_as_messages():
    line = "rsync: [sender] send_files failed to open \"/media/x\": Permission denied (13)"
    kind, value = sync_manager.classify_output_line(line)
    assert kind == "message"
    assert value == line


def test_stats_block_lines_are_messages_so_they_reach_the_log():
    kind, value = sync_manager.classify_output_line("Number of files: 104,382")
    assert kind == "message"
    assert value == "Number of files: 104,382"


def test_sentinel_is_checked_before_progress():
    """A file whose *name* looks like a progress sample must not be swallowed."""
    kind, value = sync_manager.classify_output_line(
        "@f >f+++++++++ 10 odd/1,234 45% 1.2MB/s")
    assert kind == "file"
    assert value["name"] == "odd/1,234 45% 1.2MB/s"


def test_blank_lines_are_not_messages():
    assert sync_manager.classify_output_line("")[0] == "blank"
    assert sync_manager.classify_output_line("   ")[0] == "blank"


# ---------------------------------------------------------------------------
# RateWindow — ETA from our own samples, never from a single rsync reading
# ---------------------------------------------------------------------------

def test_rate_window_averages_over_the_window():
    w = sync_manager.RateWindow(window_sec=600)
    w.add(0.0, 0)
    w.add(100.0, 100 * 1024 * 1024)      # 100 MB in 100s -> 1 MB/s
    assert abs(w.rate() - 1024 * 1024) < 1024


def test_rate_window_drops_samples_older_than_the_window():
    w = sync_manager.RateWindow(window_sec=60)
    w.add(0.0, 0)
    w.add(10.0, 10 * 1024 * 1024)        # fast early burst
    w.add(600.0, 10 * 1024 * 1024)       # then nothing for 10 min
    w.add(660.0, 10 * 1024 * 1024)
    # The burst is far outside the window; the recent rate is zero.
    assert w.rate() == 0.0


def test_rate_window_with_a_single_sample_has_no_rate():
    w = sync_manager.RateWindow()
    w.add(0.0, 1234)
    assert w.rate() == 0.0


def test_eta_is_none_when_nothing_is_moving():
    """A stalled transfer must not be given a fabricated completion time."""
    w = sync_manager.RateWindow(window_sec=600)
    w.add(0.0, 5000)
    w.add(300.0, 5000)
    assert w.eta(remaining_bytes=1000) is None


def test_eta_divides_remaining_by_the_windowed_rate():
    w = sync_manager.RateWindow(window_sec=600)
    w.add(0.0, 0)
    w.add(100.0, 100 * 1024 * 1024)      # 1 MB/s
    eta = w.eta(remaining_bytes=60 * 1024 * 1024)
    assert eta is not None and abs(eta - 60) < 1


def test_eta_is_none_for_a_non_positive_remainder():
    w = sync_manager.RateWindow(window_sec=600)
    w.add(0.0, 0)
    w.add(100.0, 100 * 1024 * 1024)
    assert w.eta(remaining_bytes=0) is None


# ---------------------------------------------------------------------------
# SyncLogWriter — throttling and line content
# ---------------------------------------------------------------------------

class FakeLog:
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text.rstrip("\n"))


def _writer(**kw):
    log = FakeLog()
    return log, sync_manager.SyncLogWriter(log, **kw)


def test_progress_line_carries_percent_bytes_speed_elapsed_eta_and_delta():
    log, w = _writer(progress_interval=60.0)
    w.progress(now=0.0, started=0.0, pct=0, bytes_=0, total=1000 * 1024**2, speed="")
    w.progress(now=60.0, started=0.0, pct=10,
               bytes_=100 * 1024**2, total=1000 * 1024**2, speed="1.7MB/s")
    line = log.lines[-1]
    assert "[SYNC] 10%" in line
    assert "100.0 MB / 1000.0 MB" in line
    assert "1.7MB/s" in line
    assert "elapsed 1m" in line
    assert "ETA" in line
    assert "+100.0 MB/60s" in line


def test_progress_says_eta_unknown_rather_than_inventing_one():
    log, w = _writer(progress_interval=30.0)
    w.progress(now=0.0, started=0.0, pct=6, bytes_=500, total=100000, speed="0.00kB/s")
    w.progress(now=600.0, started=0.0, pct=6, bytes_=500, total=100000, speed="0.00kB/s")
    assert "ETA unknown" in log.lines[-1]
    assert "+0 B/600s" in log.lines[-1]


def test_progress_is_throttled_to_the_interval_when_percent_does_not_change():
    log, w = _writer(progress_interval=60.0)
    for i in range(0, 50, 10):           # t = 0,10,20,30,40 — same percent
        w.progress(now=float(i), started=0.0, pct=6, bytes_=i, total=100, speed="")
    assert len(log.lines) == 1           # only the first


def test_a_percent_change_emits_immediately_despite_the_interval():
    log, w = _writer(progress_interval=60.0)
    w.progress(now=0.0, started=0.0, pct=6, bytes_=1, total=100, speed="")
    w.progress(now=5.0, started=0.0, pct=7, bytes_=2, total=100, speed="")
    assert len(log.lines) == 2
    assert "[SYNC] 7%" in log.lines[-1]


def test_progress_emits_again_once_the_interval_has_passed():
    log, w = _writer(progress_interval=60.0)
    w.progress(now=0.0, started=0.0, pct=6, bytes_=1, total=100, speed="")
    w.progress(now=61.0, started=0.0, pct=6, bytes_=2, total=100, speed="")
    assert len(log.lines) == 2


def test_total_of_zero_omits_the_denominator_and_the_eta():
    log, w = _writer()
    w.progress(now=0.0, started=0.0, pct=0, bytes_=4096, total=0, speed="1.0MB/s")
    line = log.lines[-1]
    assert "4.0 KB" in line
    assert "/ 0 B" not in line
    assert "ETA" not in line


# ---- the sampled [FILE] line ----------------------------------------------

def test_current_file_is_logged_at_most_once_per_interval():
    log, w = _writer(progress_interval=60.0, file_interval=60.0)
    w.note_file("a/one", 100)
    w.progress(now=0.0, started=0.0, pct=1, bytes_=1, total=100, speed="")
    w.note_file("a/two", 200)
    w.progress(now=10.0, started=0.0, pct=2, bytes_=2, total=100, speed="")
    files = [ln for ln in log.lines if ln.startswith("[FILE]")]
    assert len(files) == 1
    assert "a/one" in files[0]


def test_current_file_is_logged_again_after_the_interval():
    log, w = _writer(progress_interval=60.0, file_interval=60.0)
    w.note_file("a/one", 100)
    w.progress(now=0.0, started=0.0, pct=1, bytes_=1, total=100, speed="")
    w.note_file("a/two", 2048)
    w.progress(now=61.0, started=0.0, pct=1, bytes_=2, total=100, speed="")
    files = [ln for ln in log.lines if ln.startswith("[FILE]")]
    assert len(files) == 2
    assert "a/two" in files[1]
    assert "2.0 KB" in files[1]


def test_an_unchanged_file_is_not_relogged():
    log, w = _writer(progress_interval=60.0, file_interval=60.0)
    w.note_file("a/one", 100)
    w.progress(now=0.0, started=0.0, pct=1, bytes_=1, total=100, speed="")
    w.progress(now=120.0, started=0.0, pct=1, bytes_=2, total=100, speed="")
    files = [ln for ln in log.lines if ln.startswith("[FILE]")]
    assert len(files) == 1


def test_last_file_is_retained_for_the_post_mortem():
    _, w = _writer()
    w.note_file("a/one", 1)
    w.note_file("b/two", 2)
    assert w.last_file == "b/two"


def test_writer_without_a_log_file_is_a_no_op():
    w = sync_manager.SyncLogWriter(None)
    w.note_file("a", 1)
    w.progress(now=0.0, started=0.0, pct=1, bytes_=1, total=2, speed="")
    w.write("hello")
    assert w.last_file == "a"


# ---------------------------------------------------------------------------
# redact_cmd — the [CMD] line must never publish an SSH password
# ---------------------------------------------------------------------------

def test_redact_cmd_hides_the_sshpass_password():
    cmd = ["sshpass", "-p", "hunter2", "/usr/bin/rsync", "-a", "src", "dst"]
    out = sync_manager.redact_cmd(cmd)
    assert "hunter2" not in out
    assert "sshpass -p ***" in out


def test_redact_cmd_leaves_a_key_auth_command_untouched():
    cmd = ["/usr/bin/rsync", "-a", "-e", "ssh -i /tmp/sync_key_x.pem", "src", "dst"]
    assert sync_manager.redact_cmd(cmd) == " ".join(cmd)


def test_redact_cmd_survives_a_trailing_p_flag():
    assert "***" not in sync_manager.redact_cmd(["sshpass", "-p"])


# ---------------------------------------------------------------------------
# Structured failure payload — what MQTT / webhook receive
# ---------------------------------------------------------------------------

def test_failure_payload_has_a_stable_reason_code_and_a_precise_message():
    f = sync_manager.build_failure(
        "ssh_connection_failed", exit_code=255, pct=40,
        bytes_transferred=20 * 1024**3, bytes_total=50 * 1024**3,
        duration=7523, last_file="Snapshot/9c/9c11ab04",
        diagnostics=["remote 192.168.1.50:22 unreachable (timed out after 5s)"])
    assert f["reason_code"] == "ssh_connection_failed"
    assert f["exit_code"] == 255
    assert f["percent"] == 40
    assert f["bytes_transferred"] == 20 * 1024**3
    assert f["bytes_total"] == 50 * 1024**3
    assert f["duration_seconds"] == 7523
    assert f["last_file"] == "Snapshot/9c/9c11ab04"
    assert f["diagnostics"] == ["remote 192.168.1.50:22 unreachable (timed out after 5s)"]
    msg = f["message"]
    assert "40%" in msg
    assert "20.0 GB of 50.0 GB" in msg
    assert "2h05m" in msg
    assert "SSH/connection error" in msg
    assert "exit 255" in msg


def test_every_reason_code_has_a_human_summary():
    for code in sync_manager.SYNC_REASONS:
        assert sync_manager.SYNC_REASONS[code]
        f = sync_manager.build_failure(code)
        assert f["message"]
        assert f["reason_code"] == code


def test_failure_message_omits_progress_it_does_not_have():
    f = sync_manager.build_failure("credentials_unavailable")
    assert "%" not in f["message"]
    assert f["percent"] is None
    assert f["exit_code"] is None


def test_failure_payload_is_json_serialisable():
    import json
    f = sync_manager.build_failure("stall_timeout", pct=13, duration=1800)
    json.loads(json.dumps(f))


def test_reason_for_exit_maps_known_rsync_codes():
    assert sync_manager.reason_for_exit(255) == "ssh_connection_failed"
    assert sync_manager.reason_for_exit(12) == "rsync_protocol_error"
    assert sync_manager.reason_for_exit(11) == "file_io_error"
    assert sync_manager.reason_for_exit(23) == "partial_transfer"
    assert sync_manager.reason_for_exit(24) == "partial_transfer"
    assert sync_manager.reason_for_exit(30) == "remote_timeout"
    assert sync_manager.reason_for_exit(22) == "out_of_memory"


def test_reason_for_exit_handles_signals_and_a_missing_status():
    assert sync_manager.reason_for_exit(-9) == "killed_by_signal"
    assert sync_manager.reason_for_exit(None) == "no_exit_status"


def test_reason_for_exit_falls_back_for_an_unknown_code():
    assert sync_manager.reason_for_exit(77) == "rsync_error"


def test_signal_failure_message_names_the_signal():
    f = sync_manager.build_failure("killed_by_signal", exit_code=-9)
    assert "signal 9" in f["message"] or "SIGKILL" in f["message"]


# ---------------------------------------------------------------------------
# collect_postmortem — the probes that run after a failed transfer
# ---------------------------------------------------------------------------

def test_postmortem_runs_every_probe_in_order(monkeypatch):
    monkeypatch.setattr(sync_manager, "_probe_oom", lambda: "oom line")
    monkeypatch.setattr(sync_manager, "_probe_remote", lambda h, p, t: "remote line")
    monkeypatch.setattr(sync_manager, "_probe_wireguard", lambda: "wg line")
    monkeypatch.setattr(sync_manager, "_probe_disk", lambda p: "disk line")
    out = sync_manager.collect_postmortem({"host": "h", "port": 22, "backup_dir": "/d"})
    assert out == ["oom line", "remote line", "wg line", "disk line"]


def test_postmortem_skips_probes_that_have_nothing_to_say(monkeypatch):
    monkeypatch.setattr(sync_manager, "_probe_oom", lambda: "oom line")
    monkeypatch.setattr(sync_manager, "_probe_remote", lambda h, p, t: None)
    monkeypatch.setattr(sync_manager, "_probe_wireguard", lambda: None)
    monkeypatch.setattr(sync_manager, "_probe_disk", lambda p: None)
    assert sync_manager.collect_postmortem({}) == ["oom line"]


def test_a_raising_probe_cannot_take_down_the_post_mortem(monkeypatch):
    """A failing diagnostic must never replace the failure it is diagnosing."""
    def boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(sync_manager, "_probe_oom", boom)
    monkeypatch.setattr(sync_manager, "_probe_remote", lambda h, p, t: "remote line")
    monkeypatch.setattr(sync_manager, "_probe_wireguard", lambda: None)
    monkeypatch.setattr(sync_manager, "_probe_disk", lambda p: None)
    assert sync_manager.collect_postmortem({}) == ["remote line"]


def test_postmortem_tolerates_no_context():
    """Called with nothing known about the remote, it still returns a list."""
    assert isinstance(sync_manager.collect_postmortem(), list)


def test_remote_probe_reports_an_unreachable_host():
    # 203.0.113.0/24 is TEST-NET-3: reserved, never routed.
    line = sync_manager._probe_remote("203.0.113.1", 22, timeout=0.2)
    assert "203.0.113.1:22" in line
    assert "unreachable" in line


def test_remote_probe_says_nothing_without_a_host():
    assert sync_manager._probe_remote("", 22) is None


def test_wireguard_probe_is_silent_when_the_vpn_is_off(monkeypatch):
    monkeypatch.setattr(sync_manager, "_load_config", lambda: {"wireguard": {"enabled": False}})
    assert sync_manager._probe_wireguard() is None


def test_disk_probe_is_silent_without_a_path():
    assert sync_manager._probe_disk(None) is None


def test_notification_payload_drops_internal_bookkeeping():
    """`logged` is our own flag for whether the log already has the error; a
    webhook or MQTT subscriber has no use for it."""
    f = sync_manager.build_failure("stall_timeout", pct=13)
    f["logged"] = True
    payload = sync_manager.notification_payload(f)
    assert "logged" not in payload
    assert payload["reason_code"] == "stall_timeout"
    assert payload["percent"] == 13


def test_notification_payload_keeps_everything_actionable():
    f = sync_manager.build_failure("ssh_connection_failed", exit_code=255, pct=81,
                                   bytes_transferred=1, bytes_total=2, duration=30,
                                   last_file="x", diagnostics=["d"])
    payload = sync_manager.notification_payload(f)
    for key in ("reason_code", "message", "summary", "exit_code", "percent",
                "bytes_transferred", "bytes_total", "duration_seconds",
                "last_file", "diagnostics"):
        assert key in payload


def test_notification_payload_of_nothing_is_empty():
    assert sync_manager.notification_payload(None) == {}
