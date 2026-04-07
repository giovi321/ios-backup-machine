#!/usr/bin/env python3
"""
wg_crypto.py - Encrypt/decrypt WireGuard and other credentials.

Supports two passphrase modes:
- "udid": uses iPhone UDID (auto-decrypt when iPhone connected)
- "custom": uses a user-chosen password (manual entry required)
"""
import os, sys, json, hashlib, base64, subprocess

ENC_FILE = os.getenv("WG_ENC_FILE", "/root/iosbackupmachine/wireguard.enc")
SALT = b"iosbackupmachine-credential-salt-v2"

def get_iphone_udid():
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
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), SALT, 100000)

def _xor_bytes(data, key):
    key_stream = (key * ((len(data) // len(key)) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key_stream))

def _encrypt_dict(config_dict, passphrase, enc_file):
    if not passphrase:
        return False
    key = derive_key(passphrase)
    plaintext = json.dumps(config_dict).encode("utf-8")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
        payload = {"method": "aes-gcm", "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(ciphertext).decode()}
    except ImportError:
        nonce = os.urandom(16)
        payload = {"method": "xor-pbkdf2", "nonce": base64.b64encode(nonce).decode(), "data": base64.b64encode(_xor_bytes(plaintext, key + nonce)).decode()}
    with open(enc_file, "w") as f:
        json.dump(payload, f, indent=2)
    return True

def _decrypt_file(passphrase, enc_file):
    if not passphrase or not os.path.exists(enc_file):
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
            plaintext = AESGCM(key).decrypt(nonce, data, None)
        except Exception:
            return None
    elif method == "xor-pbkdf2":
        plaintext = _xor_bytes(data, key + nonce)
    else:
        return None
    try:
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None

def resolve_passphrase(passphrase=None, config=None):
    if passphrase:
        return passphrase
    if config is None:
        try:
            import yaml
            cfg_path = os.getenv("IOSBACKUP_CONFIG", "/root/iosbackupmachine/config.yaml")
            with open(cfg_path, "r") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            config = {}
    mode = config.get("credential_encryption", {}).get("passphrase_mode", "udid")
    if mode == "udid":
        return get_iphone_udid()
    return None

def encrypt_wg_config(wg_config_dict, passphrase=None):
    if not passphrase:
        return False
    return _encrypt_dict(wg_config_dict, passphrase, ENC_FILE)

def decrypt_wg_config(passphrase=None, config=None):
    pw = resolve_passphrase(passphrase, config)
    if not pw:
        return None
    return _decrypt_file(pw, ENC_FILE)

if __name__ == "__main__":
    import argparse, getpass
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("decrypt")
    enc_p = sub.add_parser("encrypt")
    enc_p.add_argument("config_file")
    args = parser.parse_args()
    if args.cmd == "decrypt":
        pw = resolve_passphrase() or getpass.getpass("Password: ")
        cfg = decrypt_wg_config(passphrase=pw)
        if cfg: print(json.dumps(cfg, indent=2))
        else: sys.exit(1)
    elif args.cmd == "encrypt":
        pw = resolve_passphrase() or getpass.getpass("Password: ")
        with open(args.config_file, "r") as f: content = f.read()
        if not encrypt_wg_config({"wg_conf": content}, passphrase=pw): sys.exit(1)
    else:
        parser.print_help()
