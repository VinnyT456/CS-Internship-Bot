# CS Internship Bot

A Discord bot that monitors [Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) on GitHub, stores listings in Supabase, and posts new internships to a Discord channel as rich embeds.

## How it works

```
GitHub repo (README tables + issues)
        │
        ▼
  github_internships.py  ── scrape, classify, dedupe
        │
        ▼
     Supabase DB  ── store internships, track sent status
        │
        ▼
      main.py  ── Discord bot posts new listings every 15 min
```

Every 15 minutes the bot:

1. Fetches internship tables from the repo README and open "New Internship" issues
2. Classifies roles (Software Engineering, AI / ML, Data Science, Quant, Product, Other)
3. Inserts only new listings into Supabase (deduped by company + title + URL)
4. Posts the oldest unsent internship to Discord, then marks it as sent

## Features

- **Rich Discord embeds** — company logo (Clearbit), location, category, apply link, and quick research links (Google Careers, LinkedIn)
- **Role classification** — auto-categorizes internships from job title keywords
- **Deduplication** — skips listings already in the database
- **Date filtering** — only includes internships posted on or after June 1, 2026
- **Health endpoint** — FastAPI server on `/` and `/health` for deployment platforms (e.g. Render, Railway)

## Requirements

- Python 3.14+
- A [Discord bot token](https://discord.com/developers/applications) with access to your target channel
- A [GitHub personal access token](https://github.com/settings/tokens) (for API rate limits)
- A [Supabase](https://supabase.com) project

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/<your-username>/CS-Internship-Bot.git
cd CS-Internship-Bot

# Using uv (recommended)
uv sync

# Or using pip
pip install -r requirements.txt
```

### 2. Create the Supabase table

Run the SQL in `admin.sql` in your Supabase SQL editor:

```sql
CREATE TABLE public.internships (
    id BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    job_url TEXT NOT NULL,
    job_location TEXT,
    job_type TEXT,
    job_posted_at TIMESTAMPTZ,
    source_repo TEXT,
    company_domain TEXT,
    company_logo TEXT,
    company_website TEXT,
    company_linkedin TEXT,
    sent_to_discord BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (company_name, job_title, job_url)
);
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_discord_bot_token
CHANNEL_ID=your_discord_channel_id
GITHUB_TOKEN=your_github_personal_access_token
SUPABASE_URL=your_supabase_project_url
SUPABASE_KEY=your_supabase_anon_or_service_key
PORT=10000
```

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token from the Developer Portal |
| `CHANNEL_ID` | Numeric ID of the channel to post internships to |
| `GITHUB_TOKEN` | GitHub PAT for fetching repo README and issues |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase API key |
| `PORT` | HTTP port for the health server (default: `10000`) |

### 4. Run the bot

```bash
python main.py
```

To scrape internships without starting the bot:

```bash
python github_internships.py
```

## Project structure

| File | Purpose |
|---|---|
| `main.py` | Discord bot, scheduled posting loop, FastAPI health server |
| `github_internships.py` | Scrapes GitHub repo README tables and issues, classifies roles |
| `database.py` | Supabase client — insert, query, mark-as-sent |
| `admin.sql` | Supabase table schema |
| `test.py` | Utility to check if a job posting URL is still live |

## Discord embed categories

| Category | Color |
|---|---|
| Software Engineering | Blue |
| AI / ML | Purple |
| Data Science | Green |
| Quant | Gold |
| Product | Orange |
| Other | Light grey |

## Logs

Runtime logs are written to the `logs/` directory:

- `logs/discord.log` — Discord bot activity
- `logs/github_internships.log` — scraping and parsing
- `logs/database.log` — Supabase operations

## License

MIT — see [LICENSE](LICENSE).
