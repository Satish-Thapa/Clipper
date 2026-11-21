"""Every setting you change in the app (Settings page), validated in one place and stored in the database (setting
"app", JSON). .env keeps only secrets, logins and paths.

Each field's `description` is the hover help in the UI (/api/settings/schema). The first time the app runs, a
setting that used to live in .env (SHORT_MIN, TOPIC_MIN_MINUTES, ...) starts from that .env value, so nothing tuned
there is lost; after that the Settings page is the only place to change it.

get() is cheap (cached, re-read after save()), so code reads settings at the moment it needs them; nothing copies
them at import time."""
import json
import os
import re
import threading
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import db

Platform = Literal["youtube", "facebook", "instagram", "tiktok"]
Category = Literal["gaming", "podcast", "sports"]
SHORT_HARD_MAX = 180  # YouTube Shorts can't be longer than 3 min: no SHORT ever exceeds it (longs: your limit)
LONG_LIMIT = 3600  # the longest "long" you can set (an hour); over 180s it posts to YouTube as a normal video
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _env(name, default, kind=float):
    """The old .env value as the first default (see the module doc)."""
    v = os.getenv(name)
    if v in (None, ""):
        return default
    try:
        return kind(v) if kind is not bool else v.lower() in ("1", "true", "yes")
    except ValueError:
        return default


class AutoRow(BaseModel):
    """One of up to three things Auto mode clips every day."""
    category: Category = Field("gaming", description="Gaming, podcast or sports: the rules the AI uses to find and cut videos.")
    topic: str = Field("", max_length=100, description="What to search, e.g. \"GTA 6\". Empty = the AI picks what is "
                                                         "trending in this category today.")
    videos: int = Field(1, ge=1, le=10, description="How many videos to clip for this topic each day.")
    short_slots: int = Field(1, ge=0, le=24, description="How many of the day's short slots this topic fills.")
    long_slots: int = Field(0, ge=0, le=12, description="How many of the day's long slots this topic fills.")

    @model_validator(mode="after")
    def _some(self):
        if self.short_slots + self.long_slots == 0:
            raise ValueError("each Auto topic needs at least one short or long slot")
        return self


class Settings(BaseModel):
    # ---- the daily quota: slots ----
    shorts_per_day: int = Field(3, ge=0, le=24, description="Short clips posted per day, at most. Every platform "
                                "follows this: nothing (manual or automatic) posts more shorts in a day.")
    longs_per_day: int = Field(1, ge=0, le=12, description="Long clips (90s-3min) posted per day, at most.")
    short_times: list[str] = Field(["10:00", "14:00", "18:00"], description="When each short posts (HH:MM, this "
                                   "computer's clock). One time per short.")
    long_times: list[str] = Field(["20:00"], description="When each long posts (HH:MM). One time per long.")
    weekend_days: list[int] = Field([4, 5], description="Weekend days get +1 short and +1 long (0 = Monday ... 6 = "
                                    "Sunday). Only kinds you post at all get the extra one.")
    weekend_short_time: str = Field("21:00", description="When the extra weekend short posts.")
    weekend_long_time: str = Field("22:00", description="When the extra weekend long posts.")
    platforms: list[Platform] = Field(["youtube"], description="Where SHORTS post. One slot = one clip, posted to each "
                                      "of these at that time.")
    long_platforms: list[Platform] = Field(["youtube"], description="Where LONGS post (can differ from the shorts, e.g. "
                                           "longs only to YouTube).")
    # ---- clip shape ----
    short_min: int = Field(_env("SHORT_MIN", 20, int), ge=5, le=SHORT_HARD_MAX, description="Shortest short (s).")
    short_max: int = Field(_env("SHORT_MAX", 90, int), ge=10, le=SHORT_HARD_MAX, description="Longest short the AI aims for (s).")
    long_min: int = Field(_env("LONG_MIN", 90, int), ge=20, le=LONG_LIMIT, description="Shortest long (s).")
    long_max: int = Field(_env("LONG_MAX", 180, int), ge=30, le=LONG_LIMIT, description="Longest long (s), your call (up to an hour). Over 180s it posts to YouTube as a normal video (still vertical) instead of a Short.")
    stretch: int = Field(_env("STRETCH_SHORT", 10, int), ge=0, le=60, description="Seconds a clip may run over its "
                         "max when cutting there would lose the setup or the payoff (a short never past 180s).")
    pad_before: float = Field(_env("CLIP_PAD_BEFORE", 0.8), ge=0, le=5, description="Lead-in before the first word (s).")
    pad_after: float = Field(_env("CLIP_PAD_AFTER", 1.5), ge=0, le=5, description="Room after the last word for the reaction to land (s).")
    snap_reach: float = Field(_env("SNAP_REACH", 8.0), ge=0, le=30, description="How far (s) a clip's edge may move "
                              "to reach the start / end of a sentence.")
    # ---- sources ----
    long_video_minutes: float = Field(_env("LONG_VIDEO_MINUTES", 60.0), ge=0, le=1000, description="Videos at least "
                                      "this long are clipped in pieces, like a stream (0 = never).")
    live_chunk_minutes: int = Field(round(_env("LIVE_CHUNK_SECONDS", 600, int) / 60) or 10, ge=1, le=120,
                                    description="Live mode: minutes per recorded piece (each piece is clipped while the next records).")
    keep_sources: bool = Field(_env("KEEP_SOURCES", False, bool), description="Keep the downloaded video after "
                               "clipping (0.5-2 GB each). Off = deleted; clips and their master copies stay.")
    ig_max_seconds: float = Field(_env("IG_MAX_SECONDS", 30.0), ge=0, le=LONG_LIMIT, description="Instagram's "
                                  "API often rejects longer Reels: longer clips skip Instagram (0 = no limit).")
    # ---- the researcher (Auto mode) ----
    topic_min_minutes: float = Field(_env("TOPIC_MIN_MINUTES", 8.0), ge=1, le=600, description="Auto only clips "
                                     "videos at least this long (sports show segments: 4 min is enough).")
    topic_max_minutes: float = Field(min(_env("TOPIC_MAX_MINUTES", 60.0), 60.0), ge=5, le=60, description="... and at "
                                     "most this long (an hour at most: longer videos are never picked).")
    topic_max_days: float = Field(_env("TOPIC_MAX_DAYS", 14.0), ge=0, le=365, description="... uploaded in the last "
                                  "N days (0 = any age; a game's launch week uses 7).")
    search_region: str = Field(os.getenv("SEARCH_REGION", "US") or "US", pattern=r"^[A-Z]{2}$",
                               description="Country whose YouTube and news results are searched (what's trending there).")
    # ---- defaults for new clips (each video can change them) ----
    layout: Literal["full", "cam", "split"] = Field("full", description="Full = the video fills the phone screen. "
                                                    "Streamer on top = the facecam above the gameplay. Split = the clip on top, your background video below.")
    cam_corner: Literal["auto", "tl", "tr", "bl", "br"] = Field("auto", description="Where the streamer's facecam is "
                                                                "in the video. Auto = found by face detection.")
    filler: str = Field("", max_length=200, description="Split layout: the background video (Settings → Background videos).")
    subtitles: bool = Field(True, description="Burn word-by-word subtitles in, the word being said in yellow.")
    header: bool = Field(True, description="Burn the clip's topic in big letters (e.g. \"SPEED FAILS THE HEIST 😭\"): in the middle when the streamer is on top, else at the top.")
    logo: bool = Field(True, description="Your logo (clipper/assets/logo.png), small, in the top-right corner of every clip.")
    notes: str = Field("", max_length=1000, description="Tell the AI: your own instructions for picking and writing, "
                       "used by every mode (a video can add its own).")
    # ---- Auto mode ----
    auto_enabled: bool = Field(False, description="Every day at the research time: find videos for each topic below, "
                               "clip them, and fill each topic's slots with its best clips.")
    research_time: str = Field("09:00", description="When the daily research starts (HH:MM). It refreshes the reports "
                               "first, so today's decisions use yesterday's views.")
    auto_rows: list[AutoRow] = Field([AutoRow()], max_length=3, description="Up to three topics.")
    face_check: bool = Field(True, description="Skip videos that are mostly a face on camera (talking heads, close-up podcasts).")
    # ---- switched off for now ----
    news_enabled: bool = Field(False, description="News & facts posts (switched off for now).")

    @field_validator("short_times", "long_times")
    @classmethod
    def _times(cls, v):
        bad = [t for t in v if not HHMM.match(t)]
        if bad:
            raise ValueError(f"not a time (HH:MM): {', '.join(bad)}")
        if len(set(v)) != len(v):
            raise ValueError("two slots of the same kind at the same time")
        return sorted(v)

    @field_validator("weekend_short_time", "weekend_long_time", "research_time")
    @classmethod
    def _time(cls, v):
        if not HHMM.match(v):
            raise ValueError(f"not a time (HH:MM): {v}")
        return v

    @field_validator("weekend_days")
    @classmethod
    def _days(cls, v):
        if any(d not in range(7) for d in v):
            raise ValueError("weekend days are 0 (Monday) to 6 (Sunday)")
        return sorted(set(v))

    @field_validator("platforms", "long_platforms")
    @classmethod
    def _plats(cls, v):
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _check(self):
        errs = []
        if len(self.short_times) != self.shorts_per_day:
            errs.append(f"{self.shorts_per_day} shorts a day need {self.shorts_per_day} short time(s), "
                        f"{len(self.short_times)} given")
        if len(self.long_times) != self.longs_per_day:
            errs.append(f"{self.longs_per_day} longs a day need {self.longs_per_day} long time(s), {len(self.long_times)} given")
        if self.weekend_days and self.shorts_per_day and self.weekend_short_time in self.short_times:
            errs.append("the weekend short time is already a short slot")
        if self.weekend_days and self.longs_per_day and self.weekend_long_time in self.long_times:
            errs.append("the weekend long time is already a long slot")
        if self.short_min >= self.short_max:
            errs.append("short min must be below short max")
        if self.long_min >= self.long_max:
            errs.append("long min must be below long max")
        if self.topic_min_minutes >= self.topic_max_minutes:
            errs.append("research: min video length must be below the max")
        if sum(r.short_slots for r in self.auto_rows) > self.shorts_per_day:
            errs.append(f"Auto topics take {sum(r.short_slots for r in self.auto_rows)} short slots, "
                        f"but there are {self.shorts_per_day} a day")
        if sum(r.long_slots for r in self.auto_rows) > self.longs_per_day:
            errs.append(f"Auto topics take {sum(r.long_slots for r in self.auto_rows)} long slots, "
                        f"but there are {self.longs_per_day} a day")
        if self.layout == "split" and not self.filler:
            errs.append("the split layout needs a background video")
        if self.auto_enabled and not self.auto_rows:
            errs.append("Auto needs at least one topic")
        if self.auto_enabled and not (self.platforms or self.long_platforms):
            errs.append("Auto needs at least one platform to post to")
        if errs:
            raise ValueError("; ".join(errs))
        return self


def platforms_for(kind, s=None):
    """Where a clip of this kind posts (Settings: shorts / longs)."""
    s = s or get()
    return s.long_platforms if kind == "long" else s.platforms


_cache, _lock = {}, threading.Lock()


def get():
    """The current settings (a Settings object)."""
    with _lock:
        if "s" not in _cache:
            raw = db.get_setting("app")
            try:
                _cache["s"] = Settings(**(json.loads(raw) if raw else {}))
            except (ValidationError, ValueError) as e:  # saved by an older version: start from the defaults
                db.syslog(f"settings: saved settings didn't validate ({e}); using the defaults")
                _cache["s"] = Settings()
        return _cache["s"]


def save(changes):
    """Merge `changes` into the settings, validate everything together, store. -> the new Settings.
    Raises ValueError with every problem in one message."""
    try:
        new = Settings(**{**get().model_dump(), **changes})
    except ValidationError as e:
        raise ValueError("; ".join(_say(x) for x in e.errors())) from None
    with _lock:
        db.set_setting("app", new.model_dump_json())
        _cache["s"] = new
    db.syslog("settings saved: " + ", ".join(f"{k}={v}" for k, v in changes.items()
                                             if k not in ("auto_rows",))[:400])
    return new


def _say(err):
    where = ".".join(str(x) for x in err.get("loc") or ())
    msg = str(err.get("msg", "")).removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


def schema():
    """Field -> {description, default, type info} for the UI's hover help and form building."""
    return Settings.model_json_schema()


def reset_cache():
    with _lock:
        _cache.clear()


if __name__ == "__main__":
    if "CLIPPER_DATA" not in os.environ:
        raise SystemExit("run as: CLIPPER_DATA=<empty temp dir> python -m clipper.settings")
    db.init()
    s = get()
    assert s.shorts_per_day == len(s.short_times) and s.layout == "full"

    def bad(**kw):
        try:
            save(kw)
        except ValueError as e:
            return str(e)
        raise AssertionError(f"accepted {kw}")
    assert "need 4 short time(s)" in bad(shorts_per_day=4)
    assert "not a time" in bad(short_times=["10:00", "25:00", "12:00"])
    assert "same time" in bad(short_times=["10:00", "10:00", "12:00"])
    assert "take 2 long slots" in bad(auto_rows=[{"long_slots": 2}])
    assert "at least one short or long slot" in bad(auto_rows=[{"short_slots": 0}])
    assert "background video" in bad(layout="split")
    assert "below short max" in bad(short_min=90)
    assert "already a short slot" in bad(weekend_short_time="14:00")
    new = save({"shorts_per_day": 2, "short_times": ["18:00", "09:30"],
                "auto_rows": [{"category": "sports", "short_slots": 2, "videos": 2}]})
    assert new.short_times == ["09:30", "18:00"] and get().auto_rows[0].category == "sports"
    reset_cache()
    assert get().short_times == ["09:30", "18:00"]  # survives a restart
    assert "auto_rows" in schema()["properties"]
    print("settings ok")
