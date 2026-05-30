"""Unit tests for rsync --info=progress2 parsing (sync_manager.parse_progress_line)."""
import sync_manager


def test_basic_progress():
    info = sync_manager.parse_progress_line("   1,234,567  45%  1.20MB/s    0:00:12")
    assert info["bytes"] == 1234567
    assert info["pct"] == 45
    assert info["speed"] == "1.20MB/s"
    assert info["total"] == int(1234567 * 100 / 45)


def test_kb_speed_and_xfr_suffix():
    info = sync_manager.parse_progress_line("32,768 100% 512.00kB/s 0:00:00 (xfr#1, to-chk=0/3)")
    assert info["bytes"] == 32768
    assert info["pct"] == 100
    assert info["speed"] == "512.00kB/s"
    assert info["total"] == 32768


def test_gb_speed():
    info = sync_manager.parse_progress_line("9,999,999,999  73%  1.05GB/s   0:01:00")
    assert info["pct"] == 73
    assert info["speed"].endswith("GB/s")


def test_zero_percent_total_is_zero():
    info = sync_manager.parse_progress_line("0   0%    0.00kB/s    0:00:00")
    assert info["pct"] == 0
    assert info["bytes"] == 0
    assert info["total"] == 0


def test_no_match_returns_none():
    assert sync_manager.parse_progress_line("sending incremental file list") is None
    assert sync_manager.parse_progress_line("") is None
    assert sync_manager.parse_progress_line(None) is None
