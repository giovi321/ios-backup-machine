#!/usr/bin/env python3
"""
wg_crypto.py - Encrypt/decrypt WireGuard settings using iPhone UUID as key.

The iPhone's UDID (unique device identifier) is used to derive an AES-256 key
via PBKDF2. The encrypted WireGuard config is stored in /root/wireguard.enc.

The encryption key can be backed up via the web UI or CLI.
"""
import os, sys, json, hashlib, base64, subprocess

ENC_FILE = os.getenv("WG_ENC_FILE", "/root/iosbackupmachine/wireguard.enc")
KEY_BACKUP_FILE = os.getenv("WG_KEY_BACKUP", "/root/iosbackupmachine/wireguard-key-backup.txt")
SALT = b"iosbackupmachine-wireguard-salt-v1"

def get_iphone_udid():
    """Get the connected iPhone's UDID using idevice_id."""
    try:
        out = subprocess.run(
            ["idevice_id", "-l"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if out:
            # Return the first UDID (one per line)
            return out.splitlines()[0].strip()
    except Exception:
        pass
    return None

def derive_key(udid):
    """Derive a 32-byte AES key from iPhone UDID using PBKDF2."""
    return hashlib.pbkdf2_hmac("sha256", udid.encode("utf-8"), SALT, 100000)

def _xor_bytes(data, key):
    """Simple XOR cipher for environments without cryptography lib.
    Uses the derived key repeated to match data length."""
    key_stream = (key * ((len(data) // len(key)) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key_stream))

def encrypt_wg_config(wg_config_dict, udid=None):
    """
    Encrypt a WireGuard config dict and save to ENC_FILE.
    If udid is not provided, tries to read from connected iPhone.
    Returns True on success.
    """
    if udid is None:
        udid = get_iphone_udid()
    if not udid:
        print("[WG_CRYPTO] No iPhone UDID available for encryption.", file=sys.stderr)
        return False

    key = derive_key(udid)
    plaintext = json.dumps(wg_config_dict).encode("utf-8")

    # Try to use cryptography lib (AES-GCM), fallback to XOR
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        payload = {
            "method": "aes-gcm",
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(ciphertext).decode(),
        }
    except ImportError:
        # Fallback: XOR with derived key (weaker but no extra deps)
        nonce = os.urandom(16)
        keyed_data = _xor_bytes(plaintext, key + nonce)
        payload = {
            "method": "xor-pbkdf2",
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(keyed_data).decode(),
        }

    with open(ENC_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"[WG_CRYPTO] WireGuard config encrypted and saved to {ENC_FILE}")
    return True

def decrypt_wg_config(udid=None):
    """
    Decrypt the WireGuard config from ENC_FILE.
    Returns the config dict, or None on failure.
    """
    if udid is None:
        udid = get_iphone_udid()
    if not udid:
        print("[WG_CRYPTO] No iPhone UDID available for decryption.", file=sys.stderr)
        return None

    if not os.path.exists(ENC_FILE):
        print(f"[WG_CRYPTO] Encrypted file not found: {ENC_FILE}", file=sys.stderr)
        return None

    key = derive_key(udid)

    with open(ENC_FILE, "r") as f:
        payload = json.load(f)

    method = payload.get("method", "")
    nonce = base64.b64decode(payload["nonce"])
    data = base64.b64decode(payload["data"])

    if method == "aes-gcm":
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, data, None)
        except ImportError:
            print("[WG_CRYPTO] cryptography lib not installed, cannot decrypt AES-GCM.", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[WG_CRYPTO] Decryption failed (wrong key?): {e}", file=sys.stderr)
            return None
    elif method == "xor-pbkdf2":
        plaintext = _xor_bytes(data, key + nonce)
    else:
        print(f"[WG_CRYPTO] Unknown encryption method: {method}", file=sys.stderr)
        return None

    try:
        return json.loads(plaintext.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[WG_CRYPTO] Decryption produced invalid data (wrong key?): {e}", file=sys.stderr)
        return None

def backup_encryption_key(udid=None):
    """
    Save the derived encryption key (hex-encoded) to a backup file.
    This allows recovery even without the iPhone.
    """
    if udid is None:
        udid = get_iphone_udid()
    if not udid:
        print("[WG_CRYPTO] No iPhone UDID available.", file=sys.stderr)
        return None

    key = derive_key(udid)
    key_hex = key.hex()

    with open(KEY_BACKUP_FILE, "w") as f:
        f.write(f"# iOS Backup Machine - WireGuard encryption key backup\n")
        f.write(f"# iPhone UDID: {udid}\n")
        f.write(f"# Keep this file safe!\n")
        f.write(f"key: {key_hex}\n")

    os.chmod(KEY_BACKUP_FILE, 0o600)
    print(f"[WG_CRYPTO] Key backed up to {KEY_BACKUP_FILE}")
    return key_hex

def decrypt_with_raw_key(key_hex):
    """Decrypt using a raw hex key (from backup) instead of iPhone UDID."""
    if not os.path.exists(ENC_FILE):
        return None

    key = bytes.fromhex(key_hex)

    with open(ENC_FILE, "r") as f:
        payload = json.load(f)

    method = payload.get("method", "")
    nonce = base64.b64decode(payload["nonce"])
    data = base64.b64decode(payload["data"])

    if method == "aes-gcm":
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, data, None)
        except Exception as e:
            print(f"[WG_CRYPTO] Decryption failed: {e}", file=sys.stderr)
            return None
    elif method == "xor-pbkdf2":
        plaintext = _xor_bytes(data, key + nonce)
    else:
        return None

    try:
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None

# --- CLI interface ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="WireGuard config encryption tool")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("backup-key", help="Backup the encryption key to file")
    sub.add_parser("show-key", help="Print the encryption key to stdout")
    sub.add_parser("decrypt", help="Decrypt and print WireGuard config")

    enc_p = sub.add_parser("encrypt", help="Encrypt a WireGuard config file")
    enc_p.add_argument("config_file", help="Path to WireGuard .conf file to encrypt")

    args = parser.parse_args()

    if args.cmd == "backup-key":
        k = backup_encryption_key()
        if k:
            print(f"Key: {k}")
        else:
            sys.exit(1)

    elif args.cmd == "show-key":
        udid = get_iphone_udid()
        if udid:
            print(derive_key(udid).hex())
        else:
            print("No iPhone connected.", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "decrypt":
        cfg = decrypt_wg_config()
        if cfg:
            print(json.dumps(cfg, indent=2))
        else:
            sys.exit(1)

    elif args.cmd == "encrypt":
        with open(args.config_file, "r") as f:
            content = f.read()
        wg_dict = {"wg_conf": content}
        if encrypt_wg_config(wg_dict):
            print("Encrypted successfully.")
        else:
            sys.exit(1)

    else:
        parser.print_help()
