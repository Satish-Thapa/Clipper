# Clipper UI audit

Audit of the web UI as of 16 Nov 2026, before the minimal redesign. Severity: **High** blocks a task or
breaks accessibility rules, **Medium** slows people down or causes mistakes, **Low** is polish.

## How the app is built (Phase 1)

| Area | What's there |
|---|---|
| Framework | FastAPI (Python) server, [clipper/app.py](clipper/app.py). The whole UI is one hand-written page, [clipper/static/index.html](clipper/static/index.html): plain HTML, CSS and JavaScript, about 1,400 lines. No build step and no `package.json`. |
| Styling | One `<style>` block with CSS variables (`--bg`, `--panel`, `--text`, `--dim`, `--hot`…). Dark theme only. No component library. |
| Rendering | Template strings rebuilt from `/api/state` every 2 s. Shared helpers are plain functions (`statusBadge`, `tplChecks`, `acctChecks`). |
| Storage | SQLite in `data/clipper.db` ([clipper/db.py](clipper/db.py)). Settings are now in the database too ([clipper/config.py](clipper/config.py)). |
| Tests | No lint, typecheck or test runner. Each module has a `python -m clipper.<module>` self-check. |

**Current flows**

- **Clip:** Create (paste URL or file → counts → platforms → Start) → Jobs (progress, log) → Clips (preview, upload per platform). Upload happens straight away when platforms were ticked on Create. There's no editing step: the AI picks the moments, and manual cuts are typed as text lines (`short 1:23 2:10 Title`) in Jobs.
- **Autopilot:** settings → it finds trending videos → clips wait as "scheduled" → posted on evenly spaced slots. Scheduling is slot-based; there's no "post at 18:30".
- **Post:** Posts → category → Make post (the AI writes it from a template) → edit → Approve (next slot) or Post now. The images are AI-generated cards; you can't upload your own image, and there are no carousels.

**Accounts and categories now**

- **Facebook Pages and Instagram:** stored in the `accounts` table (several of each). Added from a Meta token on Settings → Accounts.
- **Post categories:** already map to those accounts (`post_categories.accounts`).
- **Clips:** have no category. They upload to one Facebook Page and one Instagram account chosen in Settings.
- **YouTube:** exactly one account (one OAuth token file, `data/youtube_token.json`).
- **TikTok:** exactly one account (refresh token in Settings).
- **Tokens:** stored as **plain text** in SQLite. There's no secure storage to reuse.

## Findings

### High

1. **Primary buttons fail contrast:** white on the pink accent `#ff2e88` is 3.50:1 (AA needs 4.5:1). Used on every primary action. `index.html` `button {}`.
2. **The "faint" text colour fails contrast:** `#5f5f6e` is 2.92:1 on panels and 3.13:1 on the background. Used for hints, posting slots, job numbers and empty fieldset notes. `--faint` in `:root`.
3. **No visible keyboard focus:** inputs set `outline:none` (only the border changes colour), and there's no `:focus-visible` style anywhere (0 occurrences). The on/off switches hide the real checkbox (`opacity:0`), so they show no focus at all. `.switch input`, `input:focus`.
4. **It isn't clear which account a clip goes to.** Create, Topics and Autopilot list platform names only ("Facebook", "Instagram"). The actual Page and Instagram account is chosen far away in Settings, and YouTube and TikTok are whichever single account is connected. A clip card shows the account only after upload, and only as the platform name. `index.html` `fieldset.pf`, `clipper/worker.py` `upload_clip`.
5. **No light mode,** which the spec requires.

### Medium

6. **No path from raw video to "edit, then publish".** Create starts a fully automatic job. To change a cut you open Jobs → "Cut it yourself" → type timestamps as text. To publish to more platforms you go to Clips → one ↑ button per platform per clip. Best case it's 2 actions (URL + Start, with platforms preset). A clip that needs one manual cut and two more platforms takes about 8, across 3 pages.
7. **States are inconsistent.** Uploading a file shows "Uploading file…" with no percentage. Downloading and rendering show a text status, with no percentage or skeleton. Failed jobs show a red badge, but the reason is only in the log. Scheduled clips are a status word. Some platform errors are clear (the platform's own message on the clip card), but job failures aren't.
8. **No library view across clips and posts,** and no filtering by status, platform or category, or search. Clips (48 per page) and posts (per category) live on separate pages.
9. **No list of scheduled items.** The Autopilot shows today's slots as boxes, but not which clip goes on which slot. Post categories show counts only.
10. **Too many top-level places:** 8 Clipper tabs + Posts tabs + Settings tabs. "Jobs", "Topics" and "Autopilot" are three views of the same pipeline.
11. **Mobile:** the header tabs scroll sideways without any hint; the Autopilot activity tables overflow at 360 px; clip cards are fine. `nav`, `.act table`.
12. **Every list redraws every 2 s.** A text box keeps its content (drafts are kept), but hover and scroll positions inside redrawn areas can jump. `refresh()`.

### Low

13. **Three text sizes per screen are exceeded:** headings, labels, hints, badges and stats use 7+ sizes.
14. **Emoji status icons** (🟢, 📰, ⚙) mixed with text badges; platform identity is plain text, with no icon.
15. **Some confirm dialogs** use the browser's `confirm()`, which isn't styled and is easy to click through.

## What the spec needs that the backend can't do yet

These go beyond presentation. Each needs your go-ahead, because your rule is not to change processing, upload or
publishing logic unless a step says so.

| Spec item | Today | What it would take |
|---|---|---|
| Format chips IG Post / FB Post / YouTube (long) with real 1:1, 4:5 and 16:9 output | Every clip renders as 9:16 (the framing setting only changes the bars inside it) | Render one file per aspect ratio (`render.cut`) and long-form YouTube upload (`upload.upload` always sends a Short) |
| Per-platform caption overrides | One title, description and hashtag set per clip | A per-platform caption field on clips, passed to `social.upload` |
| "Schedule" at a chosen time | Slot-based autopilot only | A scheduled time per clip/post plus a check in the existing minute loop |
| Post creator with your own images and carousels | AI-made single cards only | Image upload, Instagram carousel containers and Facebook multi-photo posts (new publishing code) |
| Several YouTube and TikTok accounts | One of each | A token per account (`upload.py`, `social._tiktok_token`) |
| Categories for clips | Posts only | `category_id` + target accounts on jobs/clips; uploads use those accounts instead of the Settings choice |
| Encrypted tokens | Plain text in SQLite | Encrypt with the `cryptography` library (already installed via google-auth, but not a declared dependency) and a key file in `data/`, or the OS keychain (`keyring`, a new dependency) |
| Lint, typecheck, build, tests | None (no JS build; Python self-checks) | Keep `node --check` + `py_compile` + self-checks, or add ruff and pytest (new dev dependencies) |

## Open questions

1. **Accent colour:** your spec left `#XXXXXX` blank. The current pink `#ff2e88` can't carry white text at AA. Should it be a darker pink (`#d61f6f`, 4.6:1 with white) or another colour?
2. **Nepali text:** should the UI and the post cards support Devanagari? The cards use Arial Black and Georgia, which have no Devanagari glyphs; that would need a font such as Noto Sans Devanagari.
3. **Stack:** keep the single HTML file (no build, no new dependencies) or move to a framework? My recommendation is to keep it.
4. Which of the backend items in the table above should I build, and in which order?

---

# What was done (16 Nov 2026)

| Finding | Fix |
|---|---|
| 1. Primary buttons failed contrast | New palette: calm teal accent `#2d6a5e` with white text (6.3:1); dark mode `#7ec4b3` with dark text (8.9:1). Every text pair is AA in both modes (checked). Form-control borders are ≥3:1 (WCAG 1.4.11). |
| 2. Faint text failed contrast | Removed. The muted text is 5.6–7.3:1 in both modes. |
| 3. No visible focus | A 2px `:focus-visible` ring on everything. Switches are real checkboxes (`role="switch"`) styled in CSS, so they're focusable. Photo reordering works with the arrow keys. |
| 4. Unclear destinations | Every destination is shown as `[platform] account — format` before publishing (Create → Publish, post creator, Library details, Scheduled). Missing mappings and expired accounts are flagged with a fix link; publishing to an expired account is refused by the server too. |
| 5. No light mode | Light and dark mode (follows the system; a sidebar toggle cycles system / light / dark). |
| 6. No guided path | Create is one flow: Source → Clip → Format → Caption → Publish. Raw video to a clip published on 3 destinations: paste link, Find clips, pick a clip, Next, Next, Publish (6 actions on one page). |
| 7. Inconsistent states | Upload % (file), download %, a step list while processing, render % per clip, per-destination status after publishing, Retry failed, toasts, skeleton loaders, empty states with a next step. |
| 8. No library | Library: clips and posts together, filters (category, platform, status), search, details panel. |
| 9. No schedule view | Scheduled: by day, with time, thumbnail, category and target accounts. |
| 10. Too many places | Six sidebar items. Autopilot and Topics are under Create → Automate; Reports, Logs, cookies and templates are under Settings; Jobs is a Library tab. |
| 11. Mobile | Sidebar → icon rail (≤1100px) → bottom bar (≤720px); the side panel moves below the content; checked at 360, 768 and 1280px. |
| 12. Redraws every few seconds | Forms are built once per step; only status parts update, and lists skip redrawing while you're interacting with them. |
| 13–14. Too many sizes, emoji icons | Three text sizes (13/15/20px), one font stack (with Devanagari fonts), one radius, an 8px spacing scale, small monochrome SVG platform icons. |
| 15. Browser `confirm()` | Kept only for destructive actions (delete, stop everything). |

**New features**
- **Brands (pages):**
  - Each brand has its own accounts and categories. Demo channel comes by default and can be deleted once another brand exists; its items move to the brand you pick.
  - A sidebar dropdown appears when there's more than one brand.
- **Category → account routing:**
  - A mapping table with one default per platform; existing data is migrated and anything without a category goes to "Uncategorized".
  - Clips and posts store their own target accounts.
  - The category's description and keywords go into the AI prompts.
- **Accounts for every platform:**
  - Several YouTube channels (Google sign-in per channel), Facebook Pages and Instagram (via a Meta token), several TikTok accounts.
  - Each account shows Connected or Expired, with Reconnect.
- **Encrypted tokens and API keys:** Fernet, with the key in `data/secret.key`. Existing plain values are encrypted on start, and old token files are removed after migration.
- **Real 4:5, 1:1 and 16:9 output** (rendered on demand per destination) and **long-form YouTube**.
- **Per-platform captions**, **scheduling at a chosen time** (clips and posts), **your own photos and carousels** for posts.
- **Any OpenAI-compatible AI provider** (key, address, model names in Settings), a content-language setting, **Devanagari** on clips and cards.

**Checks run**
- Python compiles and all 9 module self-checks pass on an empty data folder.
- The UI script passes `node --check`.
- Backend end-to-end test (fakes for Meta/YouTube/TikTok):
  - two categories mapped to different accounts → each clip's destinations auto-filled correctly (Football → its YouTube, Facebook Page and TikTok; Tech tips → its own YouTube and Instagram; missing Instagram flagged);
  - partial failure plus Retry failed;
  - scheduling and the scheduled publish;
  - expired account refused;
  - carousel and text posts;
  - brands create / switch / delete with move.
- The UI's own auto-fill logic was run against the same two categories.
- Screenshots of every screen at 1280px and the key screens at 768 and 360px, light and dark.
- A real download plus render on this Mac.

There's no lint, typecheck or build step in this project (plain Python + one HTML file), and no JS test runner was added (no new dependencies).

# Open questions

1. **Instagram "IG Post" for a video** is published as a Reel shared to the feed (4:5). Instagram's API has no separate feed-video type any more. Is that what you expect?
2. **TikTok sign-in** needs a redirect to a website you own, so accounts are added by pasting a refresh token. A hosted callback page would make it one click.
3. **YouTube sign-in** opens the browser on the computer running Clipper; signing in from another device isn't supported.
4. **API limits:** Instagram 100 API posts per account per day; YouTube ~6 uploads a day on the default quota; TikTok posts private until the app is audited. These show as errors with a 24 h timer; there's no quota dashboard yet.
5. **Not built yet:**
   - a week calendar view for Scheduled (it's a day-grouped list);
   - views per post in the report;
   - per-destination crop adjustment (the frame is centred);
   - a login for using Clipper over a network.
6. **YouTube category:** uploads no longer set a hard-coded YouTube category (it was "Gaming"). Should it be a setting per category?
