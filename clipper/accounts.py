"""Social accounts for every platform: YouTube channels, Facebook Pages, Instagram accounts, TikTok accounts.

One row per account in the `accounts` table. `token` is always encrypted (crypto.py):
  facebook / instagram  the Page access token (Instagram uses its linked Page's token; page_id = that Page)
  youtube               the Google OAuth credentials JSON (refreshed tokens are written back)
  tiktok                {"refresh_token", "access_token", "expires_at"} (TikTok rotates the refresh token)
Status: "connected", or "expired" when the platform says the token no longer works (error holds its message).

The UI never sees a token: every function here returns rows without it unless asked with secret=True."""
import json
import re
import time

from . import config, crypto, db

PLATFORMS = ("youtube", "facebook", "instagram", "tiktok")
NAMES = {"youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok"}
# Meta's "this token no longer works" (OAuthException code 190); Google's invalid_grant; TikTok's invalid refresh.
# Rate limits are errors too, so match on the wording, not on "OAuthException".
TOKEN_RE = re.compile(r'"code":\s*190\b|Error validating access token|session has (?:expired|been invalidated)|'
                      r"access token has expired|invalid_grant|Token has been expired or revoked|"
                      r"refresh_token is invalid|access_token_invalid", re.I)


def _public(a):
    out = {k: v for k, v in a.items() if k != "token"}
    out["status"] = "expired" if a.get("error") else "connected"
    return out


def all(secret=False, platform=None, brand_id=None):
    """Every account (oldest first), or one platform's / one brand's. Without secret=True tokens are left out; with
    it they're decrypted."""
    where, args = [], []
    if platform:
        where.append("platform=?"); args.append(platform)
    if brand_id:
        where.append("brand_id=?"); args.append(brand_id)
    rows = list(reversed(db.all("accounts", " AND ".join(where) or "1", tuple(args))))
    if secret:
        return [{**a, "token": crypto.decrypt(a["token"])} for a in rows]
    return [_public(a) for a in rows]


def get(aid, secret=False):
    a = db.get("accounts", int(aid)) if aid else None
    if not a:
        return None
    return {**a, "token": crypto.decrypt(a["token"])} if secret else _public(a)


def save(platform, ext_id, token, name, page_id="", avatar=""):
    """Insert or (same platform + id) refresh an account. Returns its id. The token is encrypted here."""
    ext_id = str(ext_id).strip()
    dup = db.all("accounts", "platform=? AND ext_id=?", (platform, ext_id))
    row = dict(token=crypto.encrypt(token), name=name, page_id=str(page_id or ""), avatar=avatar or "",
               limited_until=0, error="")
    if dup:
        db.update("accounts", dup[0]["id"], **row)
        return dup[0]["id"]
    from . import brands
    # Never reuse an id: clips and posts remember destinations by account id, and SQLite would hand a deleted
    # account's id to the next one (an old "account 1, FB Reel" would then point at a new Instagram account).
    with db.conn() as c:
        top = c.execute("SELECT COALESCE(MAX(id), 0) FROM accounts").fetchone()[0]
    nxt = max(int(db.get_setting("accounts_next_id") or 0), top + 1)
    aid = db.insert("accounts", id=nxt, platform=platform, ext_id=ext_id, brand_id=brands.current(), created=time.time(), **row)
    db.set_setting("accounts_next_id", str(nxt + 1))
    db.syslog(f"accounts: added {platform} {name} ({ext_id})")
    return aid


def set_token(aid, token):
    """A platform gave us a refreshed token (YouTube / TikTok): store it, encrypted."""
    db.update("accounts", aid, token=crypto.encrypt(token), error="")


def token_error(aid, msg):
    """If `msg` says the token is dead, mark the account expired (the UI asks to reconnect; publishing skips it)."""
    if not aid or not TOKEN_RE.search(msg or ""):
        return False
    db.update("accounts", aid, error=f"token no longer works: {msg[:300]}")
    db.syslog(f"accounts: account {aid} needs reconnecting: {msg[:200]}")
    return True


def limited(a, now=None):
    return (a.get("limited_until") or 0) > (now or time.time())


def remove(aid):
    db.delete("accounts", aid)
    with db.conn() as c:
        c.execute("DELETE FROM category_accounts WHERE account_id=?", (aid,))
    for key in ("CLIP_FB_ACCOUNT", "CLIP_IG_ACCOUNT"):
        if config.get(key) == aid:
            config.save({key: None})


def default_for(platform, brand_id=None):
    """The account things go to when no category says (Uncategorized): Settings' choice if it belongs to that brand,
    else the brand's first account on that platform."""
    key = {"facebook": "CLIP_FB_ACCOUNT", "instagram": "CLIP_IG_ACCOUNT"}.get(platform)
    if key and config.get(key):
        a = get(config.get(key))
        if a and (not brand_id or a.get("brand_id") == brand_id):
            return a
    rows = all(platform=platform, brand_id=brand_id)
    return rows[0] if rows else None


# ---- adding accounts ----

def add_meta(platform, ext_id, token, page_id="", verify=True):
    """A Facebook Page or Instagram account, checked with Meta (name) unless verify=False."""
    from . import social
    ext_id, page_id, token = str(ext_id).strip(), str(page_id or "").strip(), token.strip()
    if verify:
        token = social.meta_long_lived(token)  # a short Explorer token would die within the hour
    if platform == "facebook":
        page_id = ext_id  # Instagram: page_id is its linked Page (optional: without one, Reels work but photos don't)
    if not verify:
        name = ext_id
    elif platform == "facebook":
        if social.is_ig_token(token):
            raise ValueError("That's an Instagram token (IGAA…): Facebook Pages need a Facebook token (EAA…)")
        try:  # only Pages have a category: an Instagram account id (or a person) is refused here
            page = social._ok(social.httpx.get(f"{social.graph()}/{ext_id}", timeout=social.TIMEOUT,
                                               params={"fields": "name,category,access_token", "access_token": token}))
        except Exception:
            page = {}
        if not page.get("category"):
            raise ValueError(f"{ext_id} isn't a Facebook Page this token can manage (is it an Instagram account id?). "
                             "Find your Page id in the Page's About section, or use Connect with a Meta token.")
        name = page.get("name") or ext_id
        # A user token can read the Page but not post videos to it (#200): keep the Page's own token (from a
        # long-lived user token it never expires). Pages reached through a Business portfolio aren't in
        # /me/accounts, but this field still returns their token.
        token = page.get("access_token") or token
    else:
        name = "@" + social.meta_name(ext_id, token, "username")
    return save(platform, ext_id, token, name, page_id)


def add_youtube():
    """Google sign-in in a browser tab on this computer (blocks until done) -> the channel as an account."""
    from . import upload
    creds_json, channel = upload.connect()
    return save("youtube", channel["id"], creds_json, channel["title"], avatar=channel.get("thumb", ""))


def add_tiktok(refresh_token):
    """A TikTok account from a refresh token (TikTok's OAuth gives you one; its redirect must be your own site)."""
    from . import social
    tok = social.tiktok_refresh(refresh_token.strip())
    info = social.tiktok_user(tok["access_token"])
    return save("tiktok", tok.get("open_id") or info.get("open_id") or refresh_token[-12:], json.dumps(tok),
                info.get("display_name") or "TikTok account", avatar=info.get("avatar_url", ""))


def check(aid=None):
    """Ask each platform whether the account's token still works. Only token errors mark an account expired (a
    network blip doesn't); a token that works again clears the mark. Names are refreshed on the way."""
    from . import social, upload
    for a in ([get(aid, secret=True)] if aid else all(secret=True)):
        if not a:
            continue
        try:
            if a["platform"] == "facebook":
                name = social.meta_name(a["ext_id"], a["token"])
            elif a["platform"] == "instagram":
                name = "@" + social.meta_name(a["ext_id"], a["token"], "username")
                if social.is_ig_token(a["token"]):  # Instagram Login tokens last 60 days: renew while it works
                    new = social.ig_refresh(a["token"])
                    if new:
                        set_token(a["id"], new)
            elif a["platform"] == "youtube":
                name = upload.channel(a)["title"]
            else:
                name = social.tiktok_user(social.tiktok_access(a)).get("display_name") or a["name"]
            db.update("accounts", a["id"], error="", name=name)
        except Exception as e:
            token_error(a["id"], str(e))


def migrate():
    """Once: encrypt tokens saved before encryption, and turn the old single YouTube / TikTok logins (token files
    and settings) into accounts. The plain-text token files are deleted afterwards."""
    for a in db.all("accounts"):
        if a["token"] and not crypto.is_encrypted(a["token"]):
            db.update("accounts", a["id"], token=crypto.encrypt(a["token"]))
    if db.get_setting("accounts_migrated"):
        return
    from .config import TIKTOK_TOKEN, YT_TOKEN
    if YT_TOKEN.exists():
        try:
            from . import upload
            creds = YT_TOKEN.read_text()
            try:
                ch = upload.channel({"token": creds, "id": None})
            except Exception:
                ch = {"id": "youtube-1", "title": "YouTube channel"}
            save("youtube", ch["id"], creds, ch["title"], avatar=ch.get("thumb", ""))
            YT_TOKEN.unlink()
        except Exception as e:
            db.syslog(f"accounts: couldn't bring the YouTube login over: {e!r}")
    tok = json.loads(TIKTOK_TOKEN.read_text()) if TIKTOK_TOKEN.exists() else None
    refresh = (tok or {}).get("refresh_token") or config.TIKTOK_REFRESH_TOKEN
    if refresh:
        save("tiktok", "tiktok-1", json.dumps(tok or {"refresh_token": refresh}), "TikTok account")
        TIKTOK_TOKEN.unlink(missing_ok=True)
        config.save({"TIKTOK_REFRESH_TOKEN": None})
    db.set_setting("accounts_migrated", "1")
