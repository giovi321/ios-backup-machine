"""Unit tests for host_key.py — SSH host key fingerprint pinning.

Everything here is pure or filesystem-only: the subprocess entry points
(``scan_host_keys`` / ``fetch_fingerprints``) are monkeypatched, so the tests
run on any machine with no ssh tooling and no network.
"""
import os
import stat

import pytest

import host_key

# 43 chars of base64 — the length ssh-keygen prints for an unpadded SHA256 digest.
FP_BODY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
FP = f"SHA256:{FP_BODY}"
OTHER_FP = "SHA256:ZZZdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"

ED_LINE = "[server.example.com]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample"
RSA_LINE = "[server.example.com]:2222 ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQExample"


# --- normalize_fingerprint --------------------------------------------------

def test_canonical_fingerprint_passes_through_unchanged():
    assert host_key.normalize_fingerprint(FP) == FP


def test_bare_base64_body_gains_the_sha256_prefix():
    assert host_key.normalize_fingerprint(FP_BODY) == FP


def test_surrounding_whitespace_and_base64_padding_are_stripped():
    assert host_key.normalize_fingerprint(f"  {FP}=  ") == FP


def test_lowercase_prefix_is_canonicalized_without_touching_the_body():
    assert host_key.normalize_fingerprint(f"sha256:{FP_BODY}") == FP


def test_fingerprint_is_extracted_from_a_full_ssh_keygen_line():
    line = f"256 {FP} server.example.com (ED25519)"
    assert host_key.normalize_fingerprint(line) == FP


def test_empty_input_means_verification_is_off():
    assert host_key.normalize_fingerprint("") == ""
    assert host_key.normalize_fingerprint("   ") == ""
    assert host_key.normalize_fingerprint(None) == ""


def test_md5_fingerprint_is_rejected_and_the_message_names_sha256():
    with pytest.raises(ValueError) as exc:
        host_key.normalize_fingerprint("MD5:1f:0e:6a:9b:44:2c:3d:5e:7f:80:91:a2:b3:c4:d5:e6")
    assert "SHA256" in str(exc.value)


def test_bare_colon_hex_fingerprint_is_rejected():
    with pytest.raises(ValueError):
        host_key.normalize_fingerprint("1f:0e:6a:9b:44:2c:3d:5e:7f:80:91:a2:b3:c4:d5:e6")


def test_garbage_is_rejected():
    with pytest.raises(ValueError):
        host_key.normalize_fingerprint("not a fingerprint")


def test_truncated_base64_is_rejected():
    with pytest.raises(ValueError):
        host_key.normalize_fingerprint("SHA256:tooshort")


# --- parse_keygen_line ------------------------------------------------------

def test_keygen_line_yields_bits_fingerprint_and_type():
    entry = host_key.parse_keygen_line(f"256 {FP} server.example.com (ED25519)")
    assert entry == {"bits": 256, "fingerprint": FP, "type": "ED25519"}


def test_keygen_line_without_a_type_suffix_still_parses():
    entry = host_key.parse_keygen_line(f"3072 {FP} no-type-here")
    assert entry["bits"] == 3072
    assert entry["fingerprint"] == FP
    assert entry["type"] == ""


def test_non_fingerprint_output_is_not_a_keygen_line():
    assert host_key.parse_keygen_line("") is None
    assert host_key.parse_keygen_line("# server.example.com SSH-2.0-OpenSSH_9.2p1") is None
    assert host_key.parse_keygen_line(None) is None


# --- scan_host_keys ---------------------------------------------------------

class _Ran:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_a_silent_refusal_names_the_port_instead_of_an_exit_code(monkeypatch):
    # ssh-keyscan says nothing on stderr for a refused connection, so "exit 1"
    # would be all the user saw on the e-ink.
    monkeypatch.setattr(host_key.subprocess, "run",
                        lambda *a, **kw: _Ran(returncode=1))
    with pytest.raises(host_key.HostKeyError) as exc:
        host_key.scan_host_keys("server.example.com", 2222)
    assert "2222" in str(exc.value)
    assert "exit" not in str(exc.value).lower()


def test_a_stderr_reason_is_passed_through(monkeypatch):
    monkeypatch.setattr(host_key.subprocess, "run",
                        lambda *a, **kw: _Ran(stderr="getaddrinfo: Name or service not known",
                                              returncode=1))
    with pytest.raises(host_key.HostKeyError) as exc:
        host_key.scan_host_keys("no-such-host.invalid", 22)
    assert "Name or service not known" in str(exc.value)


def test_keyscan_banner_comments_are_not_host_keys(monkeypatch):
    monkeypatch.setattr(host_key.subprocess, "run", lambda *a, **kw: _Ran(
        stdout=f"# server.example.com:2222 SSH-2.0-OpenSSH_9.2p1\n{ED_LINE}\n\n"))
    assert host_key.scan_host_keys("server.example.com", 2222) == [ED_LINE]


def test_a_missing_keyscan_binary_is_reported_as_such(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("ssh-keyscan")
    monkeypatch.setattr(host_key.subprocess, "run", boom)
    with pytest.raises(host_key.HostKeyError) as exc:
        host_key.scan_host_keys("server.example.com", 22)
    assert "ssh-keyscan not found" in str(exc.value)


# --- find_match -------------------------------------------------------------

def test_match_is_found_among_several_host_keys():
    entries = [
        {"fingerprint": OTHER_FP, "type": "RSA", "key_line": RSA_LINE},
        {"fingerprint": FP, "type": "ED25519", "key_line": ED_LINE},
    ]
    assert host_key.find_match(entries, FP)["key_line"] == ED_LINE


def test_match_normalizes_the_expected_value_before_comparing():
    entries = [{"fingerprint": FP, "type": "ED25519", "key_line": ED_LINE}]
    assert host_key.find_match(entries, f"  {FP_BODY}  ")["key_line"] == ED_LINE


def test_no_match_returns_none():
    entries = [{"fingerprint": OTHER_FP, "type": "RSA", "key_line": RSA_LINE}]
    assert host_key.find_match(entries, FP) is None


# --- write_known_hosts ------------------------------------------------------

def test_known_hosts_holds_only_the_pinned_key_and_is_owner_only():
    path = host_key.write_known_hosts(ED_LINE)
    try:
        with open(path) as f:
            assert f.read() == ED_LINE + "\n"
        if os.name == "posix":  # Windows chmod only toggles the read-only bit
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.remove(path)


# --- strict_host_key_opts ---------------------------------------------------

def test_without_a_pin_the_historical_accept_new_behaviour_is_kept():
    assert host_key.strict_host_key_opts(None) == ["-o", "StrictHostKeyChecking=accept-new"]
    assert host_key.strict_host_key_opts("") == ["-o", "StrictHostKeyChecking=accept-new"]


def test_a_pin_forces_strict_checking_against_only_that_file():
    # CheckHostIP is off because the key itself is already pinned: the IP
    # cross-check adds nothing and would only warn about a read-only file.
    opts = host_key.strict_host_key_opts("/tmp/kh")
    assert opts == ["-o", "StrictHostKeyChecking=yes",
                    "-o", "UserKnownHostsFile=/tmp/kh",
                    "-o", "GlobalKnownHostsFile=/dev/null",
                    "-o", "CheckHostIP=no"]


# --- verify_and_write_known_hosts ------------------------------------------

def _stub_fetch(monkeypatch, entries=None, error=None):
    def fake(host, port, timeout=10):
        if error:
            raise host_key.HostKeyError(error)
        return entries or []
    monkeypatch.setattr(host_key, "fetch_fingerprints", fake)


def test_matching_key_is_pinned_to_a_known_hosts_file(monkeypatch):
    _stub_fetch(monkeypatch, [
        {"fingerprint": OTHER_FP, "type": "RSA", "key_line": RSA_LINE},
        {"fingerprint": FP, "type": "ED25519", "key_line": ED_LINE},
    ])
    path, error = host_key.verify_and_write_known_hosts("server.example.com", 2222, FP)
    assert error is None
    try:
        with open(path) as f:
            assert f.read() == ED_LINE + "\n"
    finally:
        os.remove(path)


def test_no_configured_fingerprint_means_no_pin_and_no_error(monkeypatch):
    # (None, None) is the "verification off" contract: callers fall back to
    # accept-new rather than treating it as a failure.
    def unreachable(host, port, timeout=10):
        raise AssertionError("must not scan when no fingerprint is configured")
    monkeypatch.setattr(host_key, "fetch_fingerprints", unreachable)
    assert host_key.verify_and_write_known_hosts("server.example.com", 22, "") == (None, None)


def test_mismatch_fails_closed_with_an_interception_warning(monkeypatch):
    _stub_fetch(monkeypatch, [{"fingerprint": OTHER_FP, "type": "RSA", "key_line": RSA_LINE}])
    path, error = host_key.verify_and_write_known_hosts("server.example.com", 2222, FP)
    assert path is None
    assert "mismatch" in error.lower()


def test_a_server_with_no_host_keys_is_an_error_not_a_pass(monkeypatch):
    _stub_fetch(monkeypatch, [])
    path, error = host_key.verify_and_write_known_hosts("server.example.com", 2222, FP)
    assert path is None
    assert "no host key" in error.lower()


def test_scan_failure_is_reported_as_the_error(monkeypatch):
    _stub_fetch(monkeypatch, error="ssh-keyscan not found.")
    path, error = host_key.verify_and_write_known_hosts("server.example.com", 2222, FP)
    assert path is None
    assert error == "ssh-keyscan not found."


def test_an_unparseable_expected_fingerprint_is_reported_not_raised(monkeypatch):
    _stub_fetch(monkeypatch, [{"fingerprint": FP, "type": "ED25519", "key_line": ED_LINE}])
    path, error = host_key.verify_and_write_known_hosts("server.example.com", 2222, "bogus")
    assert path is None
    assert "fingerprint" in error.lower()
