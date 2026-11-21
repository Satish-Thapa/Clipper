"""Every AI (LLM) call in the app goes through ask(): the providers (local Ollama server, DeepSeek, this PC), their
order and fallback, JSON mode, and the prompt log (data/logs/prompts/). No prompts live here: they sit next to the
code that uses them (scout.py finds videos, pick.py picks the clips, insights.py decides, news.py)."""
import json
import re
import threading
import time

import httpx
from openai import BadRequestError, OpenAI

from . import db
from .config import (DEEPSEEK_API_KEY, DEEPSEEK_MODEL, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, PC_LLM_BASE_URL,
                     PC_LLM_MODEL)

PROVIDERS = {"deepseek": "DeepSeek", "local": "Local (Ollama)", "pc": "This PC (Ollama)"}


def user_note(notes):
    """Your own instructions for the AI (Create / Topics / Auto "Tell the AI"), as a prompt block ('' when none)."""
    notes = str(notes or "").strip()[:1000]
    return ("\n\nEXTRA INSTRUCTIONS FROM THE CHANNEL OWNER. Follow them; where they conflict with the general guidance "
            "above, they win (but keep the JSON format and the length limits):\n" + notes + "\n") if notes else ""


def _missing(p):
    """Why provider p can't be used right now ('' = ready)."""
    if p == "deepseek":
        return "" if DEEPSEEK_API_KEY else "DEEPSEEK_API_KEY missing in .env"
    if p == "local":
        return "" if LLM_BASE_URL and _model("local") else "LLM_BASE_URL / LLM_MODEL missing in .env"
    if p == "pc":
        return "" if PC_LLM_BASE_URL and _model("pc") else "PC_LLM_BASE_URL / PC_LLM_MODEL missing in .env"
    return "unknown provider"


def _model(p):
    return {"deepseek": DEEPSEEK_MODEL,
            "local": db.get_setting("ai_model") or LLM_MODEL, "pc": db.get_setting("pc_model") or PC_LLM_MODEL}.get(p, "")


def ai_order():
    """Providers to try, first = main, the rest = fallbacks in order. Saved in Settings → AI models (setting ai_order);
    default: local first when LLM_BASE_URL is set, then DeepSeek, then the small model on this PC."""
    saved = [p for p in (db.get_setting("ai_order") or "").split(",") if p in PROVIDERS]
    return saved or (["local", "deepseek", "pc"] if LLM_BASE_URL else ["deepseek", "local", "pc"])


def ai_choice():
    """The main AI -> {"provider", "model", "order": [{provider, model, name, missing}]} (first ready one is main)."""
    order = [{"provider": p, "model": _model(p), "name": PROVIDERS[p], "missing": _missing(p)} for p in ai_order()]
    main = next((o for o in order if not o["missing"]), order[0] if order else {"provider": "deepseek", "model": ""})
    return {"provider": main["provider"], "model": main["model"], "order": order}


def ai_name():
    """The main AI as the logs name it, e.g. 'gpt-oss:20b (Local (Ollama))'."""
    ch = ai_choice()
    return f"{ch['model']} ({PROVIDERS.get(ch['provider'], ch['provider'])})"


_models = {}  # url -> (time, value)


def local_models(url=None):
    """Models on an Ollama/LM Studio server (default LLM_BASE_URL) -> {"models": [names]} or {"error": ...}.
    Cached 30s, never raises. Ollama's /api/tags first (every pulled model), then the OpenAI-style /models."""
    url = LLM_BASE_URL if url is None else url
    if not url:
        return {"error": "no server address in .env"}
    if time.time() - _models.get(url, (0, None))[0] < 30:
        return _models[url][1]
    try:
        try:
            r = httpx.get(re.sub(r"/v1$", "", url) + "/api/tags", timeout=5)
            r.raise_for_status()
            names = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            r = httpx.get(url + "/models", timeout=5,
                          headers={"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY and url == LLM_BASE_URL else {})
            r.raise_for_status()
            names = [m["id"] for m in r.json().get("data", [])]
        value = {"models": sorted(names)}
    except Exception as e:
        value = {"error": f"can't reach {url}: {e}"[:200]}
    _models[url] = (time.time(), value)
    return value


def set_ai(order, local_model="", pc_model=""):
    """Save the provider order (main first, then fallbacks) for every AI call; takes effect on the next call."""
    order = list(dict.fromkeys(p for p in order if p in PROVIDERS))
    if not order:
        raise ValueError("Pick at least one AI")
    db.set_setting("ai_order", ",".join(order))
    if local_model:
        db.set_setting("ai_model", local_model)
    if pc_model:
        db.set_setting("pc_model", pc_model)
    db.syslog("AI order: " + " -> ".join(f"{PROVIDERS[p]} ({_model(p)})" for p in order))


_balance = {"at": 0, "value": None}


def balance():
    """DeepSeek credit left, e.g. {"available": True, "balances": [{"currency": "USD", "total": "10.59", ...}]}.
    Cached 60s (the UI polls every 2s). Never raises: errors come back as {"error": ...}."""
    ch = ai_choice()
    if ch["provider"] != "deepseek":  # local model: no DeepSeek balance, just say which model is in use
        return {"model": ch["model"], "url": {"local": LLM_BASE_URL, "pc": PC_LLM_BASE_URL}.get(ch["provider"], "")}
    if time.time() - _balance["at"] < 60:
        return _balance["value"]
    try:
        if not DEEPSEEK_API_KEY:
            raise RuntimeError("DEEPSEEK_API_KEY missing in .env")
        r = httpx.get("https://api.deepseek.com/user/balance", timeout=10,
                      headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"})
        r.raise_for_status()
        d = r.json()
        value = {"available": d.get("is_available", False),
                 "balances": [{"currency": b["currency"], "total": b["total_balance"], "granted": b["granted_balance"],
                               "topped_up": b["topped_up_balance"]} for b in d.get("balance_infos", [])]}
    except Exception as e:
        value = {"error": str(e)[:200]}
    _balance.update(at=time.time(), value=value)
    return value


last_used = {"provider": "", "model": ""}  # which AI answered the latest call


def _client(p):
    if p in ("local", "pc"):
        # ponytail: local models can be slow on full transcripts -> 30 min read timeout, but a dead server fails in 10s
        return OpenAI(api_key=(LLM_API_KEY if p == "local" else "") or "none",
                      base_url=LLM_BASE_URL if p == "local" else PC_LLM_BASE_URL, timeout=httpx.Timeout(1800, connect=10),
                      max_retries=1)
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", max_retries=1)


def _ask_one(p, task, system, model=None):
    kw = dict(model=model or _model(p), messages=[{"role": "system", "content": system}, {"role": "user", "content": task}])
    if p == "pc":  # small model on a 4 GB GPU: skip its "thinking" (1s instead of 12s per answer, same JSON)
        kw["reasoning_effort"] = "none"
    client = _client(p)
    try:
        r = client.chat.completions.create(response_format={"type": "json_object"}, **kw)  # forces valid JSON
    except BadRequestError:  # provider/model without JSON mode: ask plainly, the prompts already demand JSON
        r = client.chat.completions.create(**kw)
    LAST.tokens = getattr(getattr(r, "usage", None), "completion_tokens", None)  # for the speed test
    text = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S)  # reasoning models
    if "{" in text:
        text = text[text.index("{"):text.rindex("}") + 1]  # drop ```json fences or chatter around the object
    return json.loads(text)


# ---- Settings → AI models: tests ----
SAMPLE = [  # a made-up GTA stream: one obvious moment (the heist going wrong at 40-75s), the rest filler
    (0, "alright chat we're back, let me grab some water real quick"), (8, "okay so today we're doing the bank job"),
    (16, "I got the van, Lester said the cops take like two minutes"), (24, "let me just check the map, yeah north side"),
    (32, "okay we're going in, masks on"), (40, "wait why is the alarm already going, BRO the alarm"),
    (47, "NO NO NO the cops are already here how are they here"), (54, "chat I swear I didn't touch anything"),
    (61, "I'm driving the van into the river, it's the only way"), (68, "WE'RE UNDERWATER WITH THE MONEY LMAO"),
    (75, "that's the worst heist in GTA history I'm crying"), (83, "okay anyway let me check the chat"),
    (91, "thanks for the subs guys, appreciate it"), (99, "let's go buy a new car now"),
    (107, "I'm just driving around the city"), (115, "nothing happening, nice sunset though")]


def _test_task():
    from . import pick
    tr = " ".join(f"[{t}] {line}" for t, line in SAMPLE)
    return pick.FULL_TASK.format(title="GTA 6 bank heist stream", n=120, pick=pick.pick_text({"short": 1, "long": 0}),
                                 signals=pick.NO_SIGNALS, transcript=tr), pick.SYSTEM


TESTS = {"ping": "Speed: one short answer", "json": "Follows the exact JSON format",
         "clip": "A real clip pick on a sample stream (the hardest test)"}


def test(p, kind="ping", model=""):
    """Test one AI (no fallback) -> {ok, seconds, tokens_per_s, checks: [{name, ok, note}], reply, model}.
    kind: ping / json / clip (see TESTS); model: try another model on that server before switching to it.
    ValueError when the AI isn't set up."""
    if p not in PROVIDERS:
        raise ValueError("unknown AI")
    if kind not in TESTS:
        raise ValueError("unknown test")
    if _missing(p) and not model:
        raise ValueError(_missing(p))
    model = model.strip() or _model(p)
    checks, LAST.tokens = [], None
    t = time.time()
    if kind == "ping":
        a = _ask_one(p, 'What model are you? One short sentence. Answer in JSON: {"reply": "your answer"}',
                     "You are a helpful assistant.", model)
        reply = a.get("reply", a) if isinstance(a, dict) else a
        checks.append({"name": "answered", "ok": bool(str(reply).strip()), "note": ""})
    elif kind == "json":
        a = _ask_one(p, 'Return exactly {"items": [{"n": 1, "word": "one"}, {"n": 2, "word": "two"}], "done": true} '
                        "with the same keys and types, nothing else.", "You only answer with JSON.", model)
        reply = a
        items = a.get("items") if isinstance(a, dict) else None
        checks += [{"name": "valid JSON object", "ok": isinstance(a, dict), "note": ""},
                   {"name": "keys and types kept", "ok": isinstance(items, list) and len(items) == 2
                    and all(isinstance(x, dict) and isinstance(x.get("n"), int) for x in items) and a.get("done") is True,
                    "note": ""}]
    else:
        task, system = _test_task()
        a = _ask_one(p, task, system, model)
        reply = a
        clips = (a.get("clips") if isinstance(a, dict) else None) or []
        c = clips[0] if clips and isinstance(clips[0], dict) else {}
        num = lambda v: float(v) if isinstance(v, (int, float)) or str(v).replace(".", "", 1).isdigit() else None
        s, e = num(c.get("start")), num(c.get("end"))
        sc = c.get("scores") if isinstance(c.get("scores"), dict) else {}
        checks += [{"name": "valid JSON with clips", "ok": bool(clips), "note": f"{len(clips)} clip(s)"},
                   {"name": "start / end inside the video", "ok": s is not None and e is not None and 0 <= s < e <= 125,
                    "note": f"{s}-{e}s"},
                   {"name": "all 5 scores", "ok": all(k in sc for k in ("hook", "flow", "emotion", "share", "trend")),
                    "note": ", ".join(f"{k} {v}" for k, v in sc.items())},
                   {"name": "found the heist going wrong (40-75s)", "ok": s is not None and e is not None and s <= 50 and e >= 65,
                    "note": str(c.get("title") or "")[:80]},
                   {"name": "title + headline + hashtags", "ok": bool(c.get("title") and c.get("headline") and c.get("hashtags")),
                    "note": str(c.get("headline") or "")}]
    secs = time.time() - t
    tps = round(LAST.tokens / secs, 1) if LAST.tokens and secs else None
    db.syslog(f"🤖 AI test '{kind}': {model} ({PROVIDERS[p]}) {secs:.1f}s, "
              f"{sum(c['ok'] for c in checks)}/{len(checks)} checks passed")
    return {"ok": all(c["ok"] for c in checks), "seconds": round(secs, 1), "tokens_per_s": tps, "checks": checks,
            "reply": json.dumps(reply, ensure_ascii=False)[:1500] if not isinstance(reply, str) else reply[:500],
            "model": model, "provider": p, "kind": kind}


PROMPT_KEEP_DAYS = 7  # saved prompts (data/logs/prompts/<day>/) older than this are deleted
PURPOSE = {"clips": "picking the clips", "queries": "writing search queries", "videos": "judging which videos clip well",
           "topics": "finding today's trends", "lines": "writing the voice-over", "reply": "a test",
           "decide": "today's decisions from the report", "post": "writing a news post", "picks": "picking news stories"}


def _save_prompt(system, task, key):
    """Every prompt goes to its own text file (system prompt + task, the reply is added after), linked from the log
    line, so you can read exactly what the AI was told. -> (file, its /media/ link)."""
    from .config import DATA
    root, now = DATA / "logs" / "prompts", time.localtime()
    day = time.strftime("%Y-%m-%d", now)
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{time.strftime('%H%M%S', now)}_{key}_{int(time.time() * 1000) % 100000:05d}.txt"
    f.write_text(f"=== SYSTEM PROMPT ===\n{system}\n\n=== TASK ===\n{task}\n", encoding="utf-8")
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - PROMPT_KEEP_DAYS * 86400))
    for old in root.iterdir():
        if old.is_dir() and old.name < cutoff:
            import shutil
            shutil.rmtree(old, ignore_errors=True)
    return f, f"/media/logs/prompts/{day}/{f.name}"


LAST = threading.local()  # .model / .link of the last answered call in THIS thread (a clip's "picked by" + prompt link)


def ask(task, system, key="clips", full=False):
    """Send one request in JSON mode; return the raw `key` list (full=True: the whole answer as a dict).
    Tries the AIs in the saved order (Settings → AI models): when one is down, errors or returns broken JSON, the
    next one gets the same request. The prompt is saved to data/logs/prompts/ and the log line links to it."""
    errors, failed = [], 0
    try:
        pf, link = _save_prompt(system, task, key)
        db.here(f"🧠 prompt for {PURPOSE.get(key, key)} → {ai_name()}: \"{' '.join(task.split())[:220]}…\" "
                f"({len(system) + len(task):,} chars{', with your instructions' if 'FROM THE CHANNEL OWNER' in task else ''}) "
                f"full prompt + reply: {link}")
    except OSError:
        pf, link = None, ""
    for p in ai_order():
        why = _missing(p)
        if why:
            errors.append(f"{PROVIDERS[p]}: {why}")
            continue
        t = time.time()
        try:
            answer = _ask_one(p, task, system)
        except Exception as e:
            errors.append(f"{PROVIDERS[p]}: {e}"[:300])
            failed += 1
            db.here(f"🤖 AI {_model(p)} ({PROVIDERS[p]}) failed after {time.time() - t:.1f}s, trying the next one: {e}"[:400])
            continue
        last_used.update(provider=p, model=_model(p))
        LAST.model, LAST.link = f"{_model(p)} ({PROVIDERS[p]})" + (" as a fallback" if failed else ""), link
        db.here(f"🤖 AI: {_model(p)} ({PROVIDERS[p]}) answered in {time.time() - t:.1f}s"
                + (" (fallback)" if failed else ""))
        if pf:
            try:
                with open(pf, "a", encoding="utf-8") as fh:
                    fh.write(f"\n=== REPLY from {_model(p)} ({PROVIDERS[p]}), {time.time() - t:.1f}s"
                             + (f", after: {' | '.join(errors)}" if errors else "") + " ===\n"
                             + json.dumps(answer, indent=1, ensure_ascii=False) + "\n")
            except OSError:
                pass
        if full:
            return answer if isinstance(answer, dict) else {}
        return answer.get(key, []) if isinstance(answer, dict) else []
    raise RuntimeError("Every AI failed: " + " | ".join(errors))
