"""Category rules: the {CATEGORY_MODULE} part of the two big prompts. Each category has one block for FINDING
videos (scout.py: which uploads are worth clipping) and one for CUTTING them (pick.py: which moments become clips).

A topic's category is chosen by the AI when it writes the search queries (scout.plan_queries) and saved on the
topic and its jobs. A job made by hand has none: then the cutter gets every block and uses the one that fits."""

CATEGORIES = {
    "gaming": {
        "scout": """GAMING:
- Best sources: long streams and stream VODs (a facecam over gameplay is ideal), first playthroughs, "first
  reaction" / first look videos, RP (roleplay) streams, challenge and funny-moments videos, big creators reacting.
- A launch / reveal / big update week (GTA 6 trailer, release, DLC, patch) beats everything: prefer the
  first-hours uploads of it.
- Weak: official trailers and studio uploads (nothing to cut, high Content ID risk), lore explainers, tier lists,
  guides and walkthroughs, slideshow "news" videos, already-edited compilations of someone else's clips.""",
        "cut": """GAMING:
- The best clips are the CREATOR REACTING to the GAME: the game moment is the hook, the reaction is the payoff.
- Pop: fails and near-misses, physics glitches, NPCs doing something insane, police chases going off the rails,
  heist / mission fails, clutch plays, trolling, RP characters staying in role in absurd situations, "you can do
  THAT?" mechanics, hidden details and easter eggs, rage at the game, a big first-look reveal.
- moment_type is usually reaction, fail, glitch, rage, hype, hidden_detail or reveal.
- framing: gameplay_facecam when there is a facecam, else center_crop.""",
    },
    "podcast": {
        "scout": """PODCAST / TALK:
- Best sources: full episodes with a big-name guest, a hot topic this week, or drama; reaction / commentary
  streams; debates and panels.
- Weak: solo monologue reading notes, ad-heavy episodes, audio-only uploads with a still picture, clip channels
  re-uploading someone else's show.""",
        "cut": """PODCAST / TALK:
- Pop: hot takes and bold claims, a story with a twist, confessions, a guest saying something surprising about a
  famous person, an argument or disagreement, a question that catches someone off guard, real advice in one
  breath, a big laugh.
- The hook is the most quotable line; open on it, never on the host setting it up.
- Name who is talking in the title or headline when the guest is famous.
- moment_type is usually hot_take, story, debate, advice, laugh, emotional or prediction.
- framing: speaker_track for one speaker, split_screen when two people go back and forth.""",
    },
    "sports": {
        "scout": """SPORTS:
- Best sources: talk shows and podcasts about the game, press conferences, athlete interviews, fan reaction and
  watch-along streams, analysis shows (a show segment of 4+ minutes is enough).
- Never: official league / broadcaster game footage (NBA, NFL, ESPN, FIFA, UFC, team channels' highlights):
  high Content ID risk, set copyright_risk high.
- A big game, trade, injury or drama this week is the hook: prefer uploads from the last 1-3 days.""",
        "cut": """SPORTS:
- Pop: hot takes about a player or team, predictions, a heated debate, an athlete's candid answer, a funny or
  awkward press-conference moment, a fan's raw reaction to a big play.
- Never build a clip from broadcast game footage alone; the talk or the reaction is the clip.
- moment_type is usually hot_take, debate, prediction, reaction, laugh or story.
- framing: speaker_track, or split_screen for a two-person debate.""",
    },
}
NAMES = tuple(CATEGORIES)


def clean(category):
    """A category name as the AI or the database gave it -> one of NAMES, or '' (unknown)."""
    c = str(category or "").strip().lower()
    return c if c in CATEGORIES else ""


def module(category, part):
    """The rules block for `part` ("scout" or "cut"): the category's own, or all of them when it is unknown."""
    c = clean(category)
    if c:
        return CATEGORIES[c][part]
    return ("The category isn't known: work out which of these fits the video and follow that one.\n\n"
            + "\n\n".join(v[part] for v in CATEGORIES.values()))


if __name__ == "__main__":
    assert clean(" Gaming ") == "gaming" and clean("cooking") == "" and clean(None) == ""
    assert module("sports", "cut").startswith("SPORTS") and "GAMING" in module("", "scout")
    print("categories ok")
