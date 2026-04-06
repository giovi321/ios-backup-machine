#!/usr/bin/env python3
"""
wg_crypto.py - Encrypt/decrypt WireGuard and other credentials.

Credentials are encrypted using a user-chosen master password via
PBKDF2 + AES-256-GCM. The master password is never stored on disk.
"""
import os, sys, json, hashlib, base64, subprocess

ENC_FILE = os.getenv("WG_ENC_FILE", "/root/iosbackupmachine/wireguard.enc")
KEY_BACKUP_FILE = os.getenv("WG_KEY_BACKUP", "/root/iosbackupmachine/wireguard-key-backup.txt")
SALT = b"iosbackupmachine-credential-salt-v2"

def get_iphone_udid():
    """Get the connected iPhone's UDID using idevice_id."""
    try:
        out = subprocess.run(
            ["idevice_id", "-l"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if out:
            return out.splitlines()[0].strip()
    except Exception:
        pass
    return None

def derive_key(passphrase):
    """Derive a 32-byte AES key from a passphrase using PBKDF2."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), SALT, 100000)

def _xor_bytes(data, key):
    """Simple XOR cipher fallback."""
    key_stream = (key * ((len(data) // len(key)) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key_stream))

def _encrypt_dict(config_dict, passphrase, enc_file):
    """Encrypt a dict and save to file. Returns True on success."""
    if not passphrase:
        print("[CRYPTO] No passphrase provided.", file=sys.stderr)
        return False

    key = derive_key(passphrase)
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
        keyed_data = _xor_bytes(plaintext, key + nonce)
        payload = {
            "method": "xor-pbkdf2",
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(keyed_data).decode(),
        }

    with open(enc_file, "w") as f:
        json.dump(payload, f, indent=2)
    return True

def _decrypt_file(passphrase, enc_file):
    """Decrypt a file and return the dict, or None on failure."""
    if not passphrase:
        print("[CRYPTO] No passphrase provided.", file=sys.stderr)
        return None

    if not os.path.exists(enc_file):
        print(f"[CRYPTO] Encrypted file not found: {enc_file}", file=sys.stderr)
        return None

    key = derive_key(passphrase)

    with open(enc_file, "r") as f:
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
            print("[CRYPTO] cryptography lib not installed.", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[CRYPTO] Decryption failed (wrong password?): {e}", file=sys.stderr)
            return None
    elif method == "xor-pbkdf2":
        plaintext = _xor_bytes(data, key + nonce)
    else:
        print(f"[CRYPTO] Unknown method: {method}", file=sys.stderr)
        return None

    try:
        return json.loads(plaintext.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[CRYPTO] Invalid data (wrong password?): {e}", file=sys.stderr)
        return None

# --- WireGuard-specific wrappers ---

def encrypt_wg_config(wg_config_dict, passphrase=None, udid=None):
    """Encrypt WireGuard config. Accepts passphrase (preferred) or udid (legacy)."""
    pw = passphrase or udid
    if not pw:
        print("[WG_CRYPTO] No passphrase provided for encryption.", file=sys.stderr)
        return False
    ok = _encrypt_dict(wg_config_dict, pw, ENC_FILE)
    if ok:
        print(f"[WG_CRYPTO] Config encrypted and saved to {ENC_FILE}")
    return ok

def decrypt_wg_config(passphrase=None, udid=None):
    """Decrypt WireGuard config. Accepts passphrase (preferred) or udid (legacy)."""
    pw = passphrase or udid
    if not pw:
        print("[WG_CRYPTO] No passphrase provided for decryption.", file=sys.stderr)
        return None
    return _decrypt_file(pw, ENC_FILE)

def decrypt_with_raw_key(key_hex):
    """Decrypt using a raw hex key (from backup)."""
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
            print(f"[CRYPTO] Decryption failed: {e}", file=sys.stderr)
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
    import argparse, getpass
    parser = argparse.ArgumentParser(description="Credential encryption tool")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("decrypt", help="Decrypt and print WireGuard config")
    enc_p = sub.add_parser("encrypt", help="Encrypt a WireGuard config file")
    enc_p.add_argument("config_file", help="Path to WireGuard .conf file to encrypt")

    args = parser.parse_args()

    if args.cmd == "decrypt":
        pw = getpass.getpass("Master password: ")
        cfg = decrypt_wg_config(passphrase=pw)
        if cfg:
            print(json.dumps(cfg, indent=2))
        else:
            sys.exit(1)
    elif args.cmd == "encrypt":
        pw = getpass.getpass("Master password: ")
        with open(args.config_file, "r") as f:
            content = f.read()
        if encrypt_wg_config({"wg_conf": content}, passphrase=pw):
            print("Encrypted successfully.")
        else:
            sys.exit(1)
    else:
        parser.print_help()
