#!/usr/bin/env python3
"""
sync_crypto.py - Encrypt/decrypt remote sync credentials.

Reuses the encryption primitives from wg_crypto.py.
Credentials are encrypted using a user-chosen master password.
"""
import os, sys, json

import wg_crypto

ENC_FILE = os.getenv("SYNC_ENC_FILE", "/root/iosbackupmachine/sync.enc")


def encrypt_sync_config(config_dict, passphrase=None, udid=None):
    """Encrypt sync config. Accepts passphrase (preferred) or udid (legacy)."""
    pw = passphrase or udid
    if not pw:
        print("[SYNC_CRYPTO] No passphrase provided.", file=sys.stderr)
        return False
    ok = wg_crypto._encrypt_dict(config_dict, pw, ENC_FILE)
    if ok:
        print(f"[SYNC_CRYPTO] Sync config encrypted and saved to {ENC_FILE}")
    return ok


def decrypt_sync_config(passphrase=None, udid=None):
    """Decrypt sync config. Accepts passphrase (preferred) or udid (legacy)."""
    pw = passphrase or udid
    if not pw:
        print("[SYNC_CRYPTO] No passphrase provided.", file=sys.stderr)
        return None
    return wg_crypto._decrypt_file(pw, ENC_FILE)


# --- CLI interface ---
if __name__ == "__main__":
    import argparse, getpass
    parser = argparse.ArgumentParser(description="Sync config encryption tool")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("decrypt", help="Decrypt and print sync config")
    args = parser.parse_args()

    if args.cmd == "decrypt":
        pw = getpass.getpass("Master password: ")
        cfg = decrypt_sync_config(passphrase=pw)
        if cfg:
            print(json.dumps(cfg, indent=2))
        else:
            sys.exit(1)
    else:
        parser.print_help()
