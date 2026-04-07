#!/usr/bin/env python3
"""sync_crypto.py - Encrypt/decrypt remote sync credentials."""
import os, sys, json
import wg_crypto

ENC_FILE = os.getenv("SYNC_ENC_FILE", "/root/iosbackupmachine/sync.enc")

def encrypt_sync_config(config_dict, passphrase=None):
    if not passphrase:
        return False
    return wg_crypto._encrypt_dict(config_dict, passphrase, ENC_FILE)

def decrypt_sync_config(passphrase=None, config=None):
    pw = wg_crypto.resolve_passphrase(passphrase, config)
    if not pw:
        return None
    return wg_crypto._decrypt_file(pw, ENC_FILE)

if __name__ == "__main__":
    import argparse, getpass
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("decrypt")
    args = parser.parse_args()
    if args.cmd == "decrypt":
        pw = wg_crypto.resolve_passphrase() or getpass.getpass("Password: ")
        cfg = decrypt_sync_config(passphrase=pw)
        if cfg: print(json.dumps(cfg, indent=2))
        else: sys.exit(1)
    else:
        parser.print_help()
