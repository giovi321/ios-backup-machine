"""Unit tests for rsync --info=progress2 parsing and the local size walk."""
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


def test_multi_sample_chunk_uses_newest():
    """One 1024-byte read holds a whole burst of \r-separated samples."""
    chunk = ("1,000  10%  1.00MB/s 0:00:01\r"
             "2,000  20%  1.10MB/s 0:00:02\r"
             "3,000  30%  1.20MB/s 0:00:03")
    info = sync_manager.parse_progress_line(chunk)
    assert info["bytes"] == 3000
    assert info["pct"] == 30
    assert info["speed"] == "1.20MB/s"


def test_chunk_cut_mid_line_ignores_the_partial_tail():
    chunk = "1,000  10%  1.00MB/s 0:00:01\r2,000  2"
    assert sync_manager.parse_progress_line(chunk)["bytes"] == 1000


def test_local_tree_size_sums_nested_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)
    assert sync_manager.local_tree_size(str(tmp_path)) == 350


def test_local_tree_size_skips_destination_only_names(tmp_path):
    (tmp_path / "keep.bin").write_bytes(b"x" * 10)
    (tmp_path / ".stignore").write_bytes(b"y" * 999)
    (tmp_path / "~syncthing~tmpfile.tmp").write_bytes(b"y" * 999)
    lost = tmp_path / "lost+found"
    lost.mkdir()
    (lost / "junk.bin").write_bytes(b"y" * 999)
    stf = tmp_path / ".stfolder"
    stf.mkdir()
    (stf / "marker").write_bytes(b"y" * 999)
    assert sync_manager.local_tree_size(str(tmp_path)) == 10


def test_local_tree_size_unreadable_root_is_zero(tmp_path):
    assert sync_manager.local_tree_size(str(tmp_path / "nope")) == 0
