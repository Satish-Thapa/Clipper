"""Encryption for secrets kept in the database: account tokens and secret settings (API keys).

Fernet (AES-128 + HMAC, from the `cryptography` package) with a key file next to the database: data/secret.key,
created on first use and readable only by you. The database alone is useless without that file, so copying or
leaking clipper.db doesn't leak tokens. Keep the key file with the database when you move or back up data/
(losing it means re-adding accounts and re-entering keys).

Encrypted values look like "enc1:<token>". decrypt() passes plain text through, so values saved before encryption
existed keep working until migrate() rewrites them."""
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA

KEY_FILE = DATA / "secret.key"
PREFIX = "enc1:"
_lock = threading.Lock()
_fernet = []


def _f():
    if not _fernet:
        with _lock:
            if not _fernet:
                if not KEY_FILE.exists():
                    KEY_FILE.write_bytes(Fernet.generate_key())
                    try:
                        os.chmod(KEY_FILE, 0o600)  # owner only (no effect on Windows, where data/ is per-user anyway)
                    except OSError:
                        pass
                _fernet.append(Fernet(KEY_FILE.read_bytes().strip()))
    return _fernet[0]


def encrypt(value):
    """Plain text -> "enc1:..." (empty stays empty; already encrypted stays as it is)."""
    if not value or str(value).startswith(PREFIX):
        return value or ""
    return PREFIX + _f().encrypt(str(value).encode()).decode()


def decrypt(value):
    """"enc1:..." -> plain text. Plain text (from before encryption) is returned unchanged."""
    if not value or not str(value).startswith(PREFIX):
        return value or ""
    try:
        return _f().decrypt(str(value)[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise RuntimeError("can't decrypt a saved secret: data/secret.key is missing or different. "
                           "Re-add the account / re-enter the key in Settings.") from None


def is_encrypted(value):
    return bool(value) and str(value).startswith(PREFIX)


if __name__ == "__main__":
    e = encrypt("hello")
    assert e.startswith(PREFIX) and decrypt(e) == "hello" and decrypt("plain") == "plain" and encrypt("") == ""
    assert encrypt(e) == e
    print("crypto ok")
