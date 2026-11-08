"""Encrypts saved API keys before they're stored.

The encryption key is derived from SETTINGS_ENCRYPTION_KEY if set, otherwise from the
Supabase secret key the server already holds, so the database alone never reveals API keys.
Running locally without Supabase, keys stay unencrypted in data/ on this machine.
"""
import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken

import store


def _fernet():
    secret = os.environ.get("SETTINGS_ENCRYPTION_KEY") or store.SB_KEY
    if not secret:
        return None
    key = hashlib.sha256(b"kidsong-settings:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(secrets):
    f = _fernet()
    if not f:
        return {"plain": secrets}
    return {"enc": f.encrypt(json.dumps(secrets).encode()).decode()}


def unseal(blob):
    """Returns (secrets, readable). readable is False if the encryption key has changed."""
    if not blob:
        return {}, True
    if "plain" in blob:
        return blob["plain"], True
    f = _fernet()
    if not f:
        return {}, False
    try:
        return json.loads(f.decrypt(blob["enc"].encode())), True
    except InvalidToken:
        return {}, False
