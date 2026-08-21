#!/usr/bin/env python3
"""host_key.py - Optional SSH host key fingerprint pinning for remote sync.

Without a configured fingerprint the sync keeps its historical behaviour
(``StrictHostKeyChecking=accept-new``): the first key seen is trusted. With one,
the device refuses to connect unless the server presents exactly that key.

Verification is a two-step so the pin holds on the wire, not just at scan time:

1. ``ssh-keyscan`` collects the host keys the server offers, and ``ssh-keygen
   -lf`` fingerprints each one.
2. Only the key whose fingerprint matches is written to a throwaway
   ``known_hosts``, and ssh runs against that file with
   ``StrictHostKeyChecking=yes``. A different key mid-connection is rejected by
   ssh itself.

Import-safe: stdlib only, no hardware modules, so it unit-tests on any machine.
"""
import os
import re
import subprocess
import tempfile

# 43 chars of base64 — the length ssh-keygen prints for an unpadded SHA256 digest.
_B64 = r"[A-Za-z0-9+/]{43}"
_SHA256_TOKEN = re.compile(rf"SHA256:({_B64})=*", re.IGNORECASE)
_BARE_BODY = re.compile(rf"^({_B64})=*$")
# MD5 fingerprints (ssh-keygen -E md5) are colon-separated hex, 16 octets.
_MD5_FORM = re.compile(r"^(MD5:)?([0-9a-f]{2}:){15}[0-9a-f]{2}$", re.IGNORECASE)
# `256 SHA256:<body> host (ED25519)` — one line of `ssh-keygen -lf` output.
_KEYGEN_LINE = re.compile(rf"^\s*(\d+)\s+(SHA256:{_B64})=*\s*(.*)$")
_KEY_TYPE = re.compile(r"\(([A-Za-z0-9-]+)\)\s*$")


class HostKeyError(Exception):
    """The server's host keys could not be read (missing tool, timeout, refused)."""


def normalize_fingerprint(value):
    """Return ``value`` as a canonical ``SHA256:<body>`` string.

    Accepts what a user is likely to paste: the canonical form, a bare base64
    body, either with base64 padding, any case of the ``SHA256:`` prefix, or a
    whole ``ssh-keygen -lf`` line. Empty input means verification is off and
    returns ``""``. Anything else raises ``ValueError`` with a message meant to
    be shown to the user.
    """
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""

    m = _SHA256_TOKEN.search(text)
    if m:
        return f"SHA256:{m.group(1)}"

    m = _BARE_BODY.match(text)
    if m:
        return f"SHA256:{m.group(1)}"

    if _MD5_FORM.match(text):
        raise ValueError(
            "MD5 fingerprints are not supported. Use the SHA256 form shown by "
            "`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`."
        )
    raise ValueError(
        "Not a SHA256 host key fingerprint. Expected something like "
        "SHA256:AbCd... (43 base64 characters)."
    )


def parse_keygen_line(line):
    """Parse one line of ``ssh-keygen -lf`` output.

    Returns ``{"bits": int, "fingerprint": str, "type": str}``, or ``None`` for
    anything that is not a fingerprint line (blanks, ssh-keyscan's ``#``
    banners). ``type`` is ``""`` when the line carries no ``(TYPE)`` suffix.
    """
    if not line:
        return None
    m = _KEYGEN_LINE.match(str(line))
    if not m:
        return None
    bits, fingerprint, rest = m.group(1), m.group(2), m.group(3)
    type_match = _KEY_TYPE.search(rest)
    return {
        "bits": int(bits),
        "fingerprint": fingerprint,
        "type": type_match.group(1) if type_match else "",
    }


def find_match(entries, expected):
    """Return the entry whose fingerprint equals ``expected``, else ``None``.

    Both sides are normalized, so a loosely-pasted ``expected`` still matches.
    Raises ``ValueError`` if ``expected`` is not a usable fingerprint.
    """
    want = normalize_fingerprint(expected)
    if not want:
        return None
    for entry in entries or []:
        try:
            if normalize_fingerprint(entry.get("fingerprint")) == want:
                return entry
        except ValueError:
            continue
    return None


def scan_host_keys(host, port=22, timeout=10):
    """Return the host key lines ``ssh-keyscan`` reports for ``host``.

    Lines come back in ``known_hosts`` format and are used verbatim, which is
    what makes the ``[host]:port`` form correct for non-default ports without
    reconstructing it here. Raises ``HostKeyError`` if the scan cannot run.
    """
    cmd = ["ssh-keyscan", "-p", str(port), "-T", str(int(timeout)), str(host)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except FileNotFoundError:
        raise HostKeyError("ssh-keyscan not found. Install openssh-client.")
    except subprocess.TimeoutExpired:
        raise HostKeyError(f"Timed out reading the host key from {host}.")
    except Exception as e:
        raise HostKeyError(f"Cannot read the host key from {host}: {e}")

    lines = [ln.strip() for ln in (r.stdout or "").splitlines()]
    keys = [ln for ln in lines if ln and not ln.startswith("#")]
    if not keys:
        # ssh-keyscan is silent on a refused connection, so falling back to the
        # exit code would put a bare "exit 1" on the e-ink.
        err = (r.stderr or "").strip().splitlines()
        detail = err[-1][:160] if err else f"no response on port {port}"
        raise HostKeyError(f"Cannot read the host key from {host}: {detail}")
    return keys


def _fingerprint_key_line(key_line):
    """Fingerprint a single ``known_hosts`` line via ``ssh-keygen -lf``.

    One key per call rather than one file for all of them: correlating a
    fingerprint back to its key line by output order would break the moment
    ssh-keygen skipped or reordered a line, and the key count here is ~3.
    """
    fd, path = tempfile.mkstemp(prefix="sync_hostkey_", suffix=".pub")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key_line + "\n")
        try:
            r = subprocess.run(["ssh-keygen", "-lf", path],
                               capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            raise HostKeyError("ssh-keygen not found. Install openssh-client.")
        except subprocess.TimeoutExpired:
            raise HostKeyError("Timed out fingerprinting the host key.")
        for line in (r.stdout or "").splitlines():
            entry = parse_keygen_line(line)
            if entry:
                entry["key_line"] = key_line
                return entry
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def fetch_fingerprints(host, port=22, timeout=10):
    """Return ``[{bits, fingerprint, type, key_line}]`` for every key ``host`` offers.

    Raises ``HostKeyError`` if the keys cannot be read. This is the unverified
    view of the server, so it is only safe as something the user compares
    against a fingerprint obtained out-of-band.
    """
    entries = []
    for key_line in scan_host_keys(host, port, timeout):
        entry = _fingerprint_key_line(key_line)
        if entry:
            entries.append(entry)
    return entries


def write_known_hosts(key_line):
    """Write ``key_line`` to a new owner-only temp ``known_hosts`` and return its path."""
    fd, path = tempfile.mkstemp(prefix="sync_knownhosts_", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write(key_line + "\n")
    os.chmod(path, 0o600)
    return path


def strict_host_key_opts(known_hosts_path=None):
    """SSH ``-o`` options for host key checking, as a token list.

    With a verified ``known_hosts`` the check is strict and scoped to that file
    alone. Without one, the historical accept-new behaviour is kept so an
    unconfigured setup is unchanged.
    """
    if not known_hosts_path:
        return ["-o", "StrictHostKeyChecking=accept-new"]
    return ["-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={known_hosts_path}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            # The key is already pinned, so the IP cross-check adds nothing and
            # would only warn about a known_hosts it cannot append to.
            "-o", "CheckHostIP=no"]


def verify_and_write_known_hosts(host, port, expected, timeout=10):
    """Verify ``host``'s key against ``expected`` and pin it.

    Returns ``(known_hosts_path, None)`` when the fingerprint matches,
    ``(None, message)`` when it does not or the keys cannot be read, and
    ``(None, None)`` when ``expected`` is empty — verification is off and the
    caller should fall back to accept-new. Fails closed: any doubt is an error,
    never a silent pass.
    """
    try:
        want = normalize_fingerprint(expected)
    except ValueError as e:
        return None, f"Invalid host key fingerprint in the sync configuration: {e}"
    if not want:
        return None, None

    try:
        entries = fetch_fingerprints(host, port, timeout)
    except HostKeyError as e:
        return None, str(e)

    if not entries:
        return None, f"Cannot verify host key: {host} returned no host key."

    match = find_match(entries, want)
    if not match:
        offered = ", ".join(
            f"{e['fingerprint']} ({e['type']})" if e.get("type") else e["fingerprint"]
            for e in entries
        )
        return None, (
            f"Host key mismatch - refusing to connect. Expected {want}, "
            f"but {host} offered {offered}. Either the server's key changed or "
            "the connection is being intercepted."
        )
    return write_known_hosts(match["key_line"]), None
