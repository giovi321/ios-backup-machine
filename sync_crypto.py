#!/usr/bin/env python3
"""
sync_crypto.py - Encrypt/decrypt remote sync credentials using iPhone UUID as key.

Reuses the same PBKDF2 + AES-256-GCM scheme as wg_crypto.py.
The encrypted sync config is stored in /root/sync.enc.
"""
import os, sys, json, base64

import wg_crypto

ENC_FILE = os.getenv("SYNC_ENC_FILE", "/root/iosbackupmachine/sync.enc")
KEY_BACKUP_FILE = os.getenv("SYNC_KEY_BACKUP", "/root/iosbackupmachine/sync-key-backup.txt")


def encrypt_sync_config(config_dict, udid=None):
    """
    Encrypt a sync config dict and save to ENC_FILE.
    If udid is not provided, tries to read from connected iPhone.
    Returns True on success.
    """
    if udid is None:
        udid = wg_crypto.get_iphone_udid()
    if not udid:
        print("[SYNC_CRYPTO] No iPhone UDID available for encryption.", file=sys.stderr)
        return False

    key = wg_crypto.derive_key(udid)
    plaintext = json.dumps(config_dict).encode("utf-8")

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
        nonce = os.urandom(16)
        keyed_data = wg_crypto._xor_bytes(plaintext, key + nonce)
        payload = {
            "method": "xor-pbkdf2",
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(keyed_data).decode(),
        }

    with open(ENC_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"[SYNC_CRYPTO] Sync config encrypted and saved to {ENC_FILE}")
    return True


def decrypt_sync_config(udid=None):
    """
    Decrypt the sync config from ENC_FILE.
    Returns the config dict, or None on failure.
    """
    if udid is None:
        udid = wg_crypto.get_iphone_udid()
    if not udid:
        print("[SYNC_CRYPTO] No iPhone UDID available for decryption.", file=sys.stderr)
        return None

    if not os.path.exists(ENC_FILE):
        print(f"[SYNC_CRYPTO] Encrypted file not found: {ENC_FILE}", file=sys.stderr)
        return None

    key = wg_crypto.derive_key(udid)

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
            print("[SYNC_CRYPTO] cryptography lib not installed, cannot decrypt AES-GCM.", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[SYNC_CRYPTO] Decryption failed (wrong key?): {e}", file=sys.stderr)
            return None
    elif method == "xor-pbkdf2":
        plaintext = wg_crypto._xor_bytes(data, key + nonce)
    else:
        print(f"[SYNC_CRYPTO] Unknown encryption method: {method}", file=sys.stderr)
        return None

    try:
        return json.loads(plaintext.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[SYNC_CRYPTO] Decryption produced invalid data (wrong key?): {e}", file=sys.stderr)
        return None


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
            print(f"[SYNC_CRYPTO] Decryption failed: {e}", file=sys.stderr)
            return None
    elif method == "xor-pbkdf2":
        plaintext = wg_crypto._xor_bytes(data, key + nonce)
    else:
        return None

    try:
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None


def backup_encryption_key(udid=None):
    """Save the derived encryption key (hex-encoded) to a backup file."""
    if udid is None:
        udid = wg_crypto.get_iphone_udid()
    if not udid:
        print("[SYNC_CRYPTO] No iPhone UDID available.", file=sys.stderr)
        return None

    key = wg_crypto.derive_key(udid)
    key_hex = key.hex()

    with open(KEY_BACKUP_FILE, "w") as f:
        f.write("# iOS Backup Machine - Sync encryption key backup\n")
        f.write(f"# iPhone UDID: {udid}\n")
        f.write("# Keep this file safe!\n")
        f.write(f"key: {key_hex}\n")

    os.chmod(KEY_BACKUP_FILE, 0o600)
    print(f"[SYNC_CRYPTO] Key backed up to {KEY_BACKUP_FILE}")
    return key_hex


# --- CLI interface ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sync config encryption tool")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("backup-key", help="Backup the encryption key to file")
    sub.add_parser("show-key", help="Print the encryption key to stdout")
    sub.add_parser("decrypt", help="Decrypt and print sync config")

    args = parser.parse_args()

    if args.cmd == "backup-key":
        k = backup_encryption_key()
        if k:
            print(f"Key: {k}")
        else:
            sys.exit(1)
    elif args.cmd == "show-key":
        udid = wg_crypto.get_iphone_udid()
        if udid:
            print(wg_crypto.derive_key(udid).hex())
        else:
            print("No iPhone connected.", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "decrypt":
        cfg = decrypt_sync_config()
        if cfg:
            print(json.dumps(cfg, indent=2))
        else:
            sys.exit(1)
    else:
        parser.print_help()
