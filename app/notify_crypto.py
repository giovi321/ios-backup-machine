#!/usr/bin/env python3
"""
notify_crypto.py - Encrypt/decrypt notification credentials (currently the
webhook auth header value), reusing the shared AES-256-GCM credential store in
wg_crypto. Same passphrase model as WireGuard / remote sync: iPhone UDID
(auto-decrypt when connected) or a custom master password.

Stored blob (notify.enc): {"header": "<name>", "value": "<secret>"}.
"""
import os

import wg_crypto

ENC_FILE = os.getenv("NOTIFY_ENC_FILE", "/root/iosbackupmachine/notify.enc")


def encrypt_notify_config(config_dict, passphrase):
    if not passphrase:
        return False
    return wg_crypto._encrypt_dict(config_dict, passphrase, ENC_FILE)


def decrypt_notify_config(passphrase=None, config=None):
    pw = wg_crypto.resolve_passphrase(passphrase, config)
    if not pw:
        return None
    return wg_crypto._decrypt_file(pw, ENC_FILE)
