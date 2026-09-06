# CS Internship Bot

A Discord bot for CS students hunting internships. It scrapes internship and new-grad
postings from multiple aggregators into Supabase and posts them as rich embeds, then
layers an **AI career assistant** (a four-agent résumé pipeline, match scoring, résumé
tailoring, GitHub project analysis) and a full **LeetCode interview-prep platform** (daily
problems, a visual DS&A roadmap, guided lessons, mock timers, streaks, and a
leaderboard) on top.

> **Voice:** the bot speaks in a **neutral, professional tone by default**. An optional
> in-character persona (**Silver Wolf**, from *Honkai: Star Rail*) can be switched on —
> when enabled, user-facing messages are written in-character while generated
> *artifacts* (tailored résumé bullets, solution code) always stay clean and
> professional regardless. Anti-fabrication and artifact-cleanliness are enforced in
> code, so they hold in either mode.
>
> Enable the persona server-wide with `SILVER_WOLF_PERSONA=1`, or flip it live with
> `/test persona on|off|default` (admin-only). See
> [Configuration](#3-configure-environment-variables).

---

## Features

### 📋 Job posting pipeline
- **Multi-source scraping** — pulls from several aggregators (Jobright, Simplify) for
  both internships and new-grad roles ([sources](#data-sources)).
- **Cross-aggregator dedup** — the same role posted under different URLs is collapsed
  to one entry (keyed by company + normalized title + location). Normalization strips
  legal suffixes (`Inc`/`LLC`), title/wording variants (`SWE` ↔ `Software Engineer`,
  years, `co-op`), and canonicalizes locations; a role with an unknown location folds
  into the same role that has a real city, so one posting never shows up twice.
- **Detail enrichment** — each posting's aggregator page is scraped for the summary,
  responsibilities, **required vs preferred** qualifications (kept separate), skills,
  and compensation. *(Jobright now sits behind a Cloudflare Turnstile bot-wall, so its
  detail pages can't be read; those rows post with README-level data and their résumé
  tools are disabled — see below.)*
- **US-only filtering** — non-US roles are dropped before insert; ambiguous locations
  are kept.
- **Rich embeds** — company logo, an "About the role" summary, comp, work model,
  Required/Preferred sections, skills, apply link, and quick research links, posted a
  few at a time on a 15-minute loop. Score/Tailor are disabled on postings with no
  usable detail (a Jobright row with no requirements) so the résumé tools never run
  with nothing to match against.
- **Closed-role detection** — posted roles are re-checked and edited to a "closed"
  embed when they go down.

### 🤖 AI career assistant
- **`/resume analyze`** — a **four-agent résumé pipeline** in one evolving message:
  1. **Diagnoser** — an ATS'-eye read: a parse-readiness score, the lines that get you
     screened out, and the single highest-leverage fix.
  2. **Recruiter** — extracts the job's keywords and honestly matches your résumé,
     flagging real must-have gaps (and which of those your **GitHub** projects can cover).
  3. **Rewriter** — rewrites weak bullets with the XYZ formula, leaving a literal
     `[NUMBER?]` wherever a metric is owed (it never invents your numbers), and can
     compile the result to a PDF.
  4. **Hiring Manager** — an 8-question skeptical mock interview that grades each answer.
  Résumé text is extracted **mechanically** (no vision model), so the agents can't
  hallucinate content that isn't on the page.
- **`/match`** — scores how well your résumé fits a posting (0–100) with a
  reproducible two-stage scorer.
- **`/tailor`** (button on a posting) — jumps straight into the analyze pipeline seeded
  with that posting as the job description, then compiles a tailored one-page PDF (via a
  companion LaTeX microservice).
- **`/resume github`** — scans your public GitHub repos, ranks them against a job
  description, and surfaces the projects best-suited to a role (plus an improvement
  plan and a new-project idea).
- **GitHub-aware analysis** — when it runs the Recruiter stage, `/resume analyze` folds
  your real GitHub projects in as honest evidence: a JD must-have missing from the
  résumé but proven by a repo is flagged as *coverable by project X* rather than a hard
  gap, and that project's tech feeds the Rewriter.

### 🧩 LeetCode interview prep
- **Daily problem** — auto-posts the LeetCode daily each day at reset, with examples,
  constraints, and a spoiler-safe breakdown.
- **Visual DS&A roadmap** (`/leetcode roadmap`) — a rendered node-graph of 25 patterns
  across two tracks (Data Structures, Algorithms & Techniques), showing what you've
  learned, what's ready now, and what to study next along a difficulty-graded path.
- **Guided lessons** (`/leetcode learn`) — shape-first explanations of each pattern,
  grounded in a curated knowledge base.
- **Practice tooling** — company tags, pattern/topic filters, hint ladders, a mock
  timer, an explain-my-code helper, spaced-repetition review, streaks, and a
  server leaderboard.

### 📢 Announcements
- Silver Wolf patch-notes are posted to an announcement channel when new features
  ship (idempotent — a restart never double-posts).

---

## How it works

```
   Aggregators (Jobright, Simplify)
              │  scrape → dedup → US-filter
              ▼
          Supabase  ──────────────┐
              │                    │
   main.py (Discord bot)      AI subsystem (Gemini/Gemma)
   ├─ post new roles (15 min)   ├─ match scoring (two-stage)
   ├─ re-check closed roles     ├─ résumé tailoring → LaTeX PDF service
   ├─ post LeetCode daily       ├─ résumé + GitHub analysis
   └─ FastAPI health server     └─ Silver Wolf lesson / persona text
```

`main.py` runs one `discord.py` bot plus a FastAPI health server concurrently. Each
command module registers itself via a uniform `register(bot, ...)` call, and recurring
jobs run on `@tasks.loop` timers.

---

## Data sources

Postings are scraped from these public aggregators. The same role appearing in more
than one source is deduplicated on insert.

| Type | Source | Where |
|---|---|---|
| Internships | [`jobright-ai/2026-Software-Engineer-Internship`](https://github.com/jobright-ai/2026-Software-Engineer-Internship) | GitHub README |
| Internships | [`SimplifyJobs/Summer2026-Internships`](https://github.com/SimplifyJobs/Summer2026-Internships) | GitHub README |
| Internships | [jobright.ai SWE intern minisite](https://jobright.ai/minisites-jobs/intern/us/swe) | JSON feed |
| New grad | [`jobright-ai/2026-Software-Engineer-New-Grad`](https://github.com/jobright-ai/2026-Software-Engineer-New-Grad) | GitHub README |
| New grad | [`SimplifyJobs/New-Grad-Positions`](https://github.com/SimplifyJobs/New-Grad-Positions) | GitHub README |

The active sources are listed in the `INTERNSHIP_SCRAPERS` / `NEW_GRAD_SCRAPERS` tuples
in `main.py`. LeetCode data comes from a public LeetCode API (no self-hosting).

---

## Commands

Most commands are grouped to keep the slash-command list small.

| Command | What it does |
|---|---|
| `/resume upload` · `view` · `delete` | Manage your uploaded résumé (PDF) |
| `/resume analyze` | Four-agent pipeline: diagnose → match → rewrite → mock interview |
| `/resume github` | Analyze your GitHub repos against a job description |
| `/match` | Score your résumé against a posting |
| `/tailor` (posting button) | Tailor your résumé for a posting → PDF (seeds `/resume analyze`) |
| `/subscribe` · `/unsubscribe` | Keyword / category job alerts |
| `/saved` · `/search` · `/latest` | Browse saved and recent postings |

### `/leetcode` group

| Subcommand | What it does |
|---|---|
| `problem` · `random` | Fetch a specific / random problem |
| `roadmap` | Visual DS&A learning roadmap (two rendered tracks) |
| `learn` · `pattern` | Guided pattern lessons |
| `company` | Company-tagged problems |
| `hint` · `explaincode` | Hint ladder / explain your code |
| `mock` | Timed mock session |
| `streak` · `leaderboard` | Progress + server ranking |
| `weakspots` · `review` | Struggle log + spaced-repetition review |

Admin-only dev/preview commands live under the `/test` group.

---

## Setup

### Requirements
- Python **3.11+** (the code runs on 3.11 locally; `render.yaml` pins 3.14 in prod)
- A [Discord bot token](https://discord.com/developers/applications)
- A [Supabase](https://supabase.com) project
- A [Google AI Studio](https://aistudio.google.com/) API key (Gemini/Gemma)
- Optional: a [GitHub PAT](https://github.com/settings/tokens) (raises API rate limits
  for scraping + `/resume github`)

### 1. Install dependencies

`requirements.txt` is the source of truth for dependencies.

```bash
git clone https://github.com/<your-username>/CS-Internship-Bot.git
cd CS-Internship-Bot

# Using uv (recommended)
uv venv
uv pip install -r requirements.txt

# Or using pip
pip install -r requirements.txt
```

### 2. Set up the database

Run the SQL in [`admin.sql`](admin.sql) in your Supabase SQL editor. It creates every
table the bot needs (internships, new grads, users, résumés, subscriptions, LeetCode
progress, roadmap queue, etc.).

> Migrations are **not** automated — when you pull changes that add a table or column,
> re-run the new SQL in `admin.sql` manually.

### 3. Configure environment variables

Create a `.env` file in the project root:

```env
# Required
DISCORD_TOKEN=your_discord_bot_token
SUPABASE_URL=your_supabase_project_url
SUPABASE_KEY=your_supabase_service_key
GEMINI_API_KEY=your_google_ai_studio_key

# Channel IDs — these three are required (the bot won't start without them)
INTERNSHIPS_CHANNEL_ID=...
NEW_GRADS_CHANNEL_ID=...
WELCOME_CHANNEL_ID=...

# Channel IDs — optional; a missing one silently disables that feature
LEETCODE_CHANNEL_ID=...
ANNOUNCE_CHANNEL_ID=...
COMMANDS_CHANNEL_ID=...

# Optional
GITHUB_TOKEN=your_github_pat
```

**Required:** `DISCORD_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`, `GEMINI_API_KEY`, and
`INTERNSHIPS_CHANNEL_ID` / `NEW_GRADS_CHANNEL_ID` / `WELCOME_CHANNEL_ID`. The LeetCode,
announcement, and commands channels are optional — leave them unset to skip that
feature.

**Common tunables** (all have sane defaults):

| Variable | Purpose |
|---|---|
| `SILVER_WOLF_PERSONA` | `0` (default) uses a neutral professional voice; `1` (or `on`/`true`) enables the Silver Wolf persona. `/test persona` overrides this live |
| `GEMINI_API_KEYS` | Comma-separated key pool (each a separate Google project) to multiply free-tier quota |
| `GEMINI_MAX_CONCURRENCY` | Cap on concurrent AI calls (default 4) |
| `SCORE_TWO_STAGE` | `1` (default) uses the two-stage match scorer; `0` reverts to a single call |
| `LEETCODE_POST_HOUR_UTC` | Hour (UTC) to post the LeetCode daily (default `0` = LeetCode reset) |
| `MAX_POSTS_PER_CYCLE` | Max postings posted per scrape cycle |
| `RESUME_BUILD_URL` / `BUILD_TOKEN` | Endpoint + token for the résumé-PDF microservice |

### 4. Run

```bash
python main.py
```

This starts the Discord bot and a FastAPI health server (on `/` and `/health`).

---

## Project structure

| Path | Purpose |
|---|---|
| `main.py` | Bot orchestrator — command wiring, scheduled loops, health server |
| `commands/` | Slash commands, AI subsystem, persona, LeetCode + résumé logic |
| `internships/` · `new_grads/` | Per-aggregator scrapers |
| `details/` | Enriches a posting by scraping its detail page |
| `database/` | Supabase client — the single insert chokepoint + all queries |
| `githubscan/` | GitHub repo analyzer for `/resume github` and Tailor |
| `leetcode/` | LeetCode API client + vendored roadmap / knowledge JSON |
| `resume_service/` | Companion FastAPI + LaTeX service that compiles tailored résumés |
| `assets/` | Bundled fonts (for the roadmap renderer) |
| `admin.sql` | Full Supabase schema |

---

## Deployment

The bot deploys on [Render](https://render.com) per [`render.yaml`](render.yaml): the
Discord bot as a native Python service and the résumé-PDF builder as a separate Docker
service. The FastAPI health endpoint keeps the always-on process alive.

---

## Credits

This project stands on public data sources and open-source projects:

**Job posting data**
- [`jobright-ai/2026-Software-Engineer-Internship`](https://github.com/jobright-ai/2026-Software-Engineer-Internship) — internship listings
- [`jobright-ai/2026-Software-Engineer-New-Grad`](https://github.com/jobright-ai/2026-Software-Engineer-New-Grad) — new-grad listings
- [`SimplifyJobs/Summer2026-Internships`](https://github.com/SimplifyJobs/Summer2026-Internships) — internship listings
- [`SimplifyJobs/New-Grad-Positions`](https://github.com/SimplifyJobs/New-Grad-Positions) — new-grad listings
- [Jobright.ai](https://jobright.ai) & [Simplify.jobs](https://simplify.jobs) — the aggregator posting pages the detail scraper enriches from

**LeetCode data**
- [`noworneverev/leetcode-api`](https://github.com/noworneverev/leetcode-api) — the public FastAPI LeetCode wrapper (`leetcode-api-pied.vercel.app`) the bot queries
- [LeetCode](https://leetcode.com) — problem content, the daily problem, and company tags

**Résumé PDF builder**
- [`yaml-resume-builder`](https://github.com/husayni/yaml-resume-builder) — renders the résumé YAML into a LaTeX résumé template
- [Tectonic](https://tectonic-typesetting.github.io) — compiles that LaTeX to a one-page PDF (a single ~50 MB engine, no full TeX Live)

**Tech**
- [discord.py](https://github.com/Rapptz/discord.py) · [Supabase](https://supabase.com) · [Google Gemini / Gemma](https://ai.google.dev) · [PyMuPDF](https://github.com/pymupdf/PyMuPDF) · [FastAPI](https://github.com/fastapi/fastapi)

**Persona**
- The optional **Silver Wolf** persona is a character from *Honkai: Star Rail* by [HoYoverse](https://www.hoyoverse.com). Used non-commercially as a stylistic voice; all rights to the character belong to HoYoverse.

Job data is used under each source's terms for personal, non-commercial use. This project is not affiliated with or endorsed by any of the above.

---

## License

MIT — see [LICENSE](LICENSE).
