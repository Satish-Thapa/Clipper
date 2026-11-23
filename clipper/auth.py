"""Login for the whole site: one user + password from .env (CLIPPER_USER / CLIPPER_PASSWORD), a JWT (HS256, made here
with the standard library) valid 24 hours, kept in an HttpOnly cookie. Every page, API call, video and file needs it
(app.py's middleware); only the login page, the login call and the privacy page are open.

- The signing key = a random secret (CLIPPER_JWT_SECRET in .env, else made once in data/jwt_secret) + a hash of the
  password: changing the password in .env logs every browser out.
- No CLIPPER_PASSWORD in .env = nobody can log in (the site stays locked; the login page says why).
- Wrong passwords: 5 per 10 minutes per address, then that address waits (the site is reachable through a tunnel)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from .config import DATA

COOKIE = "clipper_auth"
HOURS = 24
USER = os.getenv("CLIPPER_USER", "admin").strip() or "admin"
PASSWORD = os.getenv("CLIPPER_PASSWORD", "")
DISABLED = False  # tests only (set in code, never from .env)
MAX_FAILS, FAIL_WINDOW = 5, 600


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret():
    s = os.getenv("CLIPPER_JWT_SECRET", "")
    if s:
        return s.encode()
    f = DATA / "jwt_secret"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
    return f.read_text().strip().encode()


def _key():
    return _secret() + hashlib.sha256(PASSWORD.encode()).digest()


def make(user=USER, hours=HOURS):
    """A signed token for `user`, valid `hours`. -> (token, expiry time)."""
    now = int(time.time())
    exp = now + int(hours * 3600)
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"sub": user, "iat": now, "exp": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}", exp


def check(token):
    """The token's claims when it's ours, untampered and not expired; else None."""
    if DISABLED:
        return {"sub": USER}
    if not token or not PASSWORD or token.count(".") != 2:
        return None
    head, body, sig = token.split(".")
    try:
        if json.loads(_unb64(head)).get("alg") != "HS256":
            return None
        good = _b64(hmac.new(_key(), f"{head}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        claims = json.loads(_unb64(body))
    except Exception:
        return None
    if claims.get("sub") != USER or int(claims.get("exp", 0)) <= time.time():
        return None
    return claims


_fails, _lock = {}, threading.Lock()


def wait_for(ip):
    """Seconds this address must wait before trying again (0 = it may try)."""
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if t > now - FAIL_WINDOW]
        _fails[ip] = recent
        return int(recent[0] + FAIL_WINDOW - now) + 1 if len(recent) >= MAX_FAILS else 0


def login(user, password, ip):
    """-> (token, exp) or raises ValueError with the reason to show."""
    if not PASSWORD:
        raise ValueError("no password set: add CLIPPER_PASSWORD=... to .env and restart the server")
    w = wait_for(ip)
    if w:
        raise PermissionError(f"too many wrong tries: wait {w // 60 + 1} min")
    ok = hmac.compare_digest(str(user or "").strip().encode(), USER.encode()) & \
        hmac.compare_digest(str(password or "").encode(), PASSWORD.encode())
    if not ok:
        with _lock:
            _fails.setdefault(ip, []).append(time.time())
        raise ValueError("wrong user or password")
    with _lock:
        _fails.pop(ip, None)
    return make(USER)


if __name__ == "__main__":  # self-test: python -m clipper.auth (with CLIPPER_DATA=<tmp>)
    PASSWORD = "pw"
    t, e = make()
    assert check(t)["sub"] == USER and e - time.time() > 23.9 * 3600
    assert check(t[:-2] + ("AA" if t[-2:] != "AA" else "BB")) is None  # tampered signature
    h, b, s = t.split(".")
    forged = json.loads(_unb64(b)); forged["exp"] += 99999
    assert check(f"{h}.{_b64(json.dumps(forged).encode())}.{s}") is None  # changed claims, old signature
    assert check(make(hours=-1)[0]) is None  # expired
    PASSWORD = "other"
    assert check(t) is None  # password changed: old tokens die
    for _ in range(MAX_FAILS):
        try:
            login(USER, "nope", "1.2.3.4")
        except ValueError:
            pass
    try:
        login(USER, "other", "1.2.3.4")
        raise AssertionError("should wait")
    except PermissionError:
        pass
    assert login(USER, "other", "5.6.7.8")[0]
    print("auth ok")
