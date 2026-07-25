import asyncio
import gc
import logging
import logging.handlers
import os
import random
import threading
from urllib.parse import quote
import time
from datetime import datetime, timezone

import discord
import uvicorn
from discord.ext import commands, tasks
from dotenv import load_dotenv
from fastapi import FastAPI

import details
from database.database import SupabaseDatabase
from internships.jobright_internships import JobrightInternships
from internships.simplify_internships import SimplifyInternships
from new_grads.jobright_new_grad import JobrightNewGrad
from new_grads.simplify_new_grad import SimplifyNewGrad
from commands import testwelcome as testwelcome_cmd
from commands import clearinternships as clearinternships_cmd
from commands import refreshembeds as refreshembeds_cmd


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID"))
INTERNSHIPS_CHANNEL_ID = int(os.getenv("INTERNSHIPS_CHANNEL_ID"))
#TEST_INTERNSHIPS_CHANNEL_ID = int(os.getenv("TEST_INTERNSHIPS_CHANNEL_ID"))
NEW_GRADS_CHANNEL_ID = int(os.getenv("NEW_GRADS_CHANNEL_ID"))

CATEGORY_COLORS = {
    "Software Engineering": discord.Color.blue(),
    "AI / ML": discord.Color.purple(),
    "Data Science": discord.Color.green(),
    "Quant": discord.Color.gold(),
    "Product": discord.Color.orange(),
    "Other": discord.Color.light_grey(),
}

MESSAGE_SEND_DELAY_SECONDS = 1.2
# Cap posts per 15-min cycle so a first run against a full table (hundreds of
# rows) can't post for longer than the loop interval and overlap the next tick.
# ~1.2s/post × 400 ≈ 8 min, comfortably under 15. Remaining rows stay unsent
# and carry to the next cycle.
MAX_POSTS_PER_CYCLE = 400

# Detail-page fetches run concurrently. Kept low: each worker holds a response
# body, and Render's instance is small. 4 workers ≈ 1 posting/sec end to end,
# well inside the 15-minute cycle even at the 400-post cap.
ENRICH_WORKERS = int(os.getenv("ENRICH_WORKERS", "4"))
# Concurrent message edits during /refreshembeds. Discord's edit rate is ~5/s
# per channel; discord.py backs off on 429 on its own, so this just caps how
# many edits are in flight at once.
REFRESH_CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "5"))

CHANNEL_CACHE = {}
SEARCH_URL_CACHE = {}
BOT_AVATAR_URL = None
COMMANDS_SYNCED = False


log_formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

file_handler = logging.handlers.RotatingFileHandler(
    filename="logs/discord.log",
    encoding="utf-8",
    maxBytes=1_000_000,
    backupCount=3,
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

discord_logger = logging.getLogger("discord")
discord_logger.setLevel(logging.INFO)

if not discord_logger.handlers:
    discord_logger.addHandler(file_handler)
    discord_logger.addHandler(console_handler)

logger = logging.getLogger("cs_internship_bot")
logger.setLevel(logging.INFO)

if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


intents = discord.Intents.default()
intents.members = True  # required for on_member_join

# max_messages=None disables discord.py's 1000-message cache — the bot only
# posts, never reads messages back, and Render's instance has 512 MB
bot = commands.Bot(command_prefix="/", intents=intents, max_messages=None)
app = FastAPI()


@app.get("/")
def home():
    return {"status": "Discord bot running"}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok"}


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)


# Singletons: creating these per task tick leaks httpx connection pools and
# re-loads clients every 10-15 minutes — a slow memory creep that eventually
# OOMs Render's 512 MB instance. One instance each, reused forever.
_scrapers = {}
_supabase_db = None


def _get(cls):
    inst = _scrapers.get(cls)
    if inst is None:
        inst = _scrapers[cls] = cls()
    return inst


def get_db():
    global _supabase_db
    if _supabase_db is None:
        _supabase_db = SupabaseDatabase()
    return _supabase_db


def _run_scraper(scraper):
    # Isolated so one repo failing doesn't sink the others
    try:
        scraper.insert_internships()
    except Exception:
        logger.exception("Scraper %s failed", scraper.__class__.__name__)


INTERNSHIP_SCRAPERS = (JobrightInternships, SimplifyInternships)
NEW_GRAD_SCRAPERS = (JobrightNewGrad, SimplifyNewGrad)


def _scrape_all(scraper_classes):
    # Sequential, not concurrent: each repo's README soup balloons to ~25 MB
    # during parsing. Running them back-to-back keeps only one soup alive at
    # a time and frees it (gc) before the next — running them all at once
    # tripled the peak and OOM'd Render's 512 MB instance.
    for cls in scraper_classes:
        _run_scraper(_get(cls))
        gc.collect()


def fetch_new_internships():
    _scrape_all(INTERNSHIP_SCRAPERS)
    return get_db().get_unsent_internships("internships") or []


def fetch_new_grads():
    _scrape_all(NEW_GRAD_SCRAPERS)
    return get_db().get_unsent_internships("new_grads") or []


def enrich_rows(rows, table):
    """Fetch each posting's detail page and persist what comes back.

    Runs on a worker thread (called via asyncio.to_thread) because the
    enrichers are blocking httpx. Rows whose lookup fails come back
    unchanged, so a dead detail page only costs us the extra fields.
    """
    if not rows:
        return []

    try:
        enriched = details.enrich_jobs(rows, max_workers=ENRICH_WORKERS)
    except Exception:
        logger.exception("Detail enrichment failed; posting unenriched rows")
        return rows
    finally:
        # Drop pooled sockets between cycles rather than holding them open
        # for the 15 minutes until the next one.
        details.close_http_clients()
        gc.collect()

    db = get_db()
    for original, row in zip(rows, enriched):
        if row is original:
            continue
        db.update_job_details(row["id"], row, table)

    return enriched


def mark_internship_as_sent(internship_id, table):
    get_db().mark_as_sent(internship_id, table)


def update_internship_message_id(internship_id, message_id, table):
    get_db().update_internship_message_id(internship_id, message_id, table)


async def get_cached_channel(cache_key, channel_id):
    channel = CHANNEL_CACHE.get(cache_key)

    if channel is not None:
        return channel

    channel = bot.get_channel(channel_id)

    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    CHANNEL_CACHE[cache_key] = channel
    return channel


async def cache_channels():
    await get_cached_channel("welcome", WELCOME_CHANNEL_ID)
    await get_cached_channel("internships", INTERNSHIPS_CHANNEL_ID)
    #await get_cached_channel("test_internships", TEST_INTERNSHIPS_CHANNEL_ID)
    await get_cached_channel("new_grads", NEW_GRADS_CHANNEL_ID)


def normalize_company_info(company_info):
    if isinstance(company_info, list):
        return company_info[0] if company_info else {}

    if isinstance(company_info, dict):
        return company_info

    return {}


def get_fallback_search_urls(company):
    if company not in SEARCH_URL_CACHE:
        SEARCH_URL_CACHE[company] = {
            "website": (
                "https://www.google.com/search?q="
                f"{quote(company + ' careers')}"
            ),
            "linkedin": (
                "https://www.linkedin.com/search/results/companies/?keywords="
                f"{quote(company)}"
            ),
        }

    return SEARCH_URL_CACHE[company]


def truncate_embed_value(value, max_length=1024):
    value = str(value or "Unknown")

    if len(value) <= max_length:
        return value

    return value[: max_length - 1] + "…"


def format_location(location):
    location = location or "Unknown"
    location_list = [loc.strip() for loc in location.split(" | ") if loc.strip()]

    if not location_list:
        return "Unknown"

    if len(location_list) > 5:
        location_formatted = "\n".join(f"• {loc}" for loc in location_list[:5])
        location_formatted += f"\n• +{len(location_list) - 5} more"
    else:
        location_formatted = "\n".join(f"• {loc}" for loc in location_list)

    return truncate_embed_value(location_formatted)


def format_bullets(items, limit=3, max_item_len=180):
    """Top `limit` list entries as bullets, with a '+N more' tail. Each entry
    is trimmed so one long paragraph can't eat the whole 1024-char field."""
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return None

    shown = items[:limit]
    lines = [f"• {truncate_embed_value(i, max_item_len)}" for i in shown]
    if len(items) > limit:
        lines.append(f"• +{len(items) - limit} more")

    return truncate_embed_value("\n".join(lines))


def format_pay(internship):
    """Prefer the human string the detail page gave; else build one from the
    annualized-thousands comp range."""
    pay = internship.get("salary_desc")
    if pay:
        return pay
    low = internship.get("comp_min")
    if not low:
        return None
    high = internship.get("comp_max") or low
    return f"${low}k/yr" if low == high else f"${low}k - ${high}k/yr"


def build_internship_embed(internship, type="internship"):
    """Build the posting embed from a row. Shared by the live post path and
    the /refreshembeds command so both render identically. Every enrichment
    block is conditional — a row missing detail columns degrades to the
    compact form rather than showing blank fields. A row flagged is_closed
    gets the closed treatment instead."""
    if internship.get("is_closed"):
        return build_closed_embed(internship)

    company_info = normalize_company_info(internship.get("company_info"))

    company = company_info.get("company_name") or internship.get("company_name") or "Unknown"
    company_website = company_info.get("company_website")
    company_linkedin = company_info.get("company_linkedin")
    company_logo = company_info.get("company_logo")

    title = internship.get("job_title") or "Untitled Internship"
    location = internship.get("job_location") or "Unknown"
    category = internship.get("job_type") or "Other"
    url = internship.get("job_url")
    posted = internship.get("job_posted_at") or "Unknown"

    # Title is the role; the company + its context sit in the author line so
    # the two read as a clean two-tier header rather than repeating the name.
    embed = discord.Embed(
        title=title,
        url=url,
        color=CATEGORY_COLORS.get(category, discord.Color.blurple()),
        timestamp=discord.utils.utcnow(),
    )

    # Author subtitle packs the taxonomy (category · type) next to the name so
    # those don't each need their own field below.
    author_bits = [category]
    if type:
        author_bits.append(str(type).title())
    embed.set_author(
        name=f"{company}  •  {'  ·  '.join(author_bits)}",
        icon_url=company_logo or BOT_AVATAR_URL,
    )

    # Enrichment is best-effort — every block below no-ops when the detail
    # lookup came back empty, so the embed degrades gracefully.
    summary = internship.get("job_summary")
    if summary:
        embed.description = truncate_embed_value(summary, 600)

    # --- Compensation leads: it's the field applicants scan for. Highlighted
    # as a full-width line so it reads before the rest of the grid.
    pay = format_pay(internship)
    if pay:
        embed.add_field(name="💰 Compensation", value=f"**{pay}**", inline=False)

    # --- Stat grid: only the facts we actually have. Discord lays inline
    # fields out three per row, so we collect the present ones and pad the
    # last row to a multiple of three instead of hand-placing spacers.
    stats = [
        ("🏢 Work Model", internship.get("work_model")),
        ("📈 Level", internship.get("seniority")),
        ("📍 Location", format_location(location)),
        ("🗓️ Posted", posted),
    ]
    present = [(name, value) for name, value in stats if value]
    for name, value in present:
        embed.add_field(
            name=name,
            value=truncate_embed_value(str(value), 1018),
            inline=True,
        )
    for _ in range((3 - len(present) % 3) % 3):
        embed.add_field(name="​", value="​", inline=True)

    # --- The richest new payload: what the role does and what it needs.
    responsibilities = format_bullets(internship.get("job_responsibilities"))
    if responsibilities:
        embed.add_field(
            name="📋 What you'll do",
            value=responsibilities,
            inline=False,
        )

    requirements = format_bullets(internship.get("job_requirements"))
    if requirements:
        embed.add_field(
            name="✅ Requirements",
            value=requirements,
            inline=False,
        )

    tags = internship.get("job_tags")
    if tags:
        embed.add_field(
            name="🏷️ Skills",
            value=truncate_embed_value(
                " • ".join(f"`{t}`" for t in tags[:8]), 1018
            ),
            inline=False,
        )

    if url:
        embed.add_field(
            name="🟢 Apply",
            value=f"**[Open Internship ↗]({url})**",
            inline=False,
        )

    # --- Links compressed onto one line instead of three separate fields.
    fallback_urls = get_fallback_search_urls(company)
    website = company_website or fallback_urls["website"]
    linkedin = company_linkedin or fallback_urls["linkedin"]
    source_url = internship.get("source_repo")

    links = [f"[Website]({website})", f"[LinkedIn]({linkedin})"]
    if source_url:
        links.append(f"[Source]({source_url})")

    embed.add_field(
        name="🔗 Links",
        value=" • ".join(links),
        inline=False,
    )

    embed.set_footer(text="CS Internship Bot", icon_url=BOT_AVATAR_URL)

    return embed


def build_closed_embed(internship):
    """A muted embed for postings the detail page reported as closed, so an
    edited-in-place message reads as dead rather than sending members to a
    broken link."""
    company_info = normalize_company_info(internship.get("company_info"))
    company = (
        company_info.get("company_name")
        or internship.get("company_name")
        or "Unknown"
    )
    title = internship.get("job_title") or "Untitled Internship"

    embed = discord.Embed(
        title=f"🔒 {title}",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=company, icon_url=BOT_AVATAR_URL)
    embed.description = (
        f"## {company}\n"
        "⚠️ **This posting has closed** — it's no longer accepting applicants."
    )
    posted = internship.get("job_posted_at")
    if posted:
        embed.add_field(name="🗓️ Was posted", value=str(posted), inline=True)
    location = internship.get("job_location")
    if location:
        embed.add_field(
            name="📍 Location", value=format_location(location), inline=True
        )
    embed.set_footer(text="CS Internship Bot • Posting closed")
    return embed


async def send_internship(internship, channel, table, type="internship"):
    embed = build_internship_embed(internship, type)

    message = await channel.send(embed=embed)

    await asyncio.to_thread(
        update_internship_message_id, internship["id"], message.id, table
    )


async def _post_batch(rows, channel, kind, table):
    logger.info("Found %s unsent %s", len(rows), kind)

    if len(rows) > MAX_POSTS_PER_CYCLE:
        logger.info(
            "Capping this cycle to %d of %d %s; the rest post next cycle",
            MAX_POSTS_PER_CYCLE, len(rows), kind,
        )
        rows = rows[:MAX_POSTS_PER_CYCLE]

    # Enrich after the cap so we only pay for detail pages we're about to post.
    rows = await asyncio.to_thread(enrich_rows, rows, table)

    for internship in rows:
        internship_id = internship.get("id", "unknown")

        try:
            # The detail page said the posting is gone. Burn the row rather
            # than send members to a dead link.
            if internship.get("is_closed"):
                logger.info("Skipping closed %s %s", kind, internship_id)
                await asyncio.to_thread(
                    mark_internship_as_sent, internship["id"], table
                )
                continue

            await send_internship(internship, channel, table, kind)

            await asyncio.to_thread(
                mark_internship_as_sent, internship["id"], table
            )

            await asyncio.sleep(MESSAGE_SEND_DELAY_SECONDS)

        except discord.HTTPException as exc:
            logger.exception(
                "Discord API error while sending %s %s: %s",
                kind,
                internship_id,
                exc,
            )
            await asyncio.sleep(10)

        except Exception:
            logger.exception("Failed sending %s %s", kind, internship_id)


# (table -> channel cache key, channel id) for the refresh command.
REFRESH_TARGETS = (
    ("internships", "internships", INTERNSHIPS_CHANNEL_ID),
    ("new_grads", "new_grads", NEW_GRADS_CHANNEL_ID),
)


async def refresh_posted_embeds(kind="internship", progress=None, limit=None,
                                only_table=None):
    """Re-render already-posted messages in place with the current (enriched)
    embed. Fetches each row's stored discord_message_id, rebuilds the embed,
    and edits the message. Missing messages (deleted by hand) are skipped and
    their message_id cleared so we don't retry them next run.

    limit: cap the TOTAL messages touched across both tables (None = all).
        Lets you test on a handful before committing to the full ~1300-edit,
        ~26-minute run.
    only_table: restrict to "internships" or "new_grads" (None = both).
    progress: optional async callable(done, total, table) for live updates.
    Returns a stats dict.
    """
    stats = {"edited": 0, "skipped_missing": 0, "failed": 0, "total": 0}

    db = get_db()
    plan = []
    remaining = limit
    for table, cache_key, channel_id in REFRESH_TARGETS:
        if only_table and table != only_table:
            continue
        if remaining is not None and remaining <= 0:
            break
        rows = await asyncio.to_thread(db.get_posted_internships, table, True, remaining)
        channel = await get_cached_channel(cache_key, channel_id)
        plan.append((table, channel, rows or []))
        stats["total"] += len(rows or [])
        if remaining is not None:
            remaining -= len(rows or [])

    # Concurrency cap: discord.py handles per-route 429 backoff internally, so
    # instead of a fixed sleep we fan out and let this bound in-flight edits.
    # ~5 concurrent stays under Discord's edit rate and cuts a 1300-message run
    # from ~26 min to ~5-6 min.
    semaphore = asyncio.Semaphore(REFRESH_CONCURRENCY)
    counter = {"done": 0}

    async def handle(table, channel, row):
        message_id = row.get("discord_message_id")
        async with semaphore:
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                stats["skipped_missing"] += 1
                await asyncio.to_thread(
                    update_internship_message_id, row["id"], None, table
                )
                return
            except discord.HTTPException:
                logger.exception("Fetch failed for %s msg %s", table, message_id)
                stats["failed"] += 1
                return

            try:
                embed = build_internship_embed(row, kind)
                await message.edit(embed=embed)
                stats["edited"] += 1
            except discord.HTTPException as exc:
                logger.exception("Edit failed for %s msg %s: %s", table, message_id, exc)
                stats["failed"] += 1
            except Exception:
                logger.exception("Rebuild failed for %s row %s", table, row.get("id"))
                stats["failed"] += 1
            finally:
                counter["done"] += 1
                if progress and counter["done"] % 50 == 0:
                    await progress(counter["done"], stats["total"], table)

    tasks_ = [
        handle(table, channel, row)
        for table, channel, rows in plan
        for row in rows
    ]
    await asyncio.gather(*tasks_)

    return stats


@tasks.loop(minutes=15)
async def check_new_internships():
    start = time.perf_counter()

    logger.info("=" * 60)
    logger.info("🔍 Starting internship scrape")
    logger.info("Time (UTC): %s", datetime.now(timezone.utc).isoformat())

    try:
        rows = await asyncio.to_thread(fetch_new_internships)

        logger.info("Fetched %d new internship(s)", len(rows))

    except Exception:
        logger.exception("❌ Failed fetching internships")
        return

    channel = await get_cached_channel("internships", INTERNSHIPS_CHANNEL_ID)

    await _post_batch(rows, channel, "internships", "internships")

    elapsed = time.perf_counter() - start
    logger.info("✅ Scrape completed in %.2f seconds", elapsed)

    # Return scrape-cycle allocations
    gc.collect()


@check_new_internships.before_loop
async def before_check():
    await bot.wait_until_ready()
    logger.info("Waiting to start internship check")

@tasks.loop(minutes=15)
async def check_new_grads():
    start = time.perf_counter()

    logger.info("=" * 60)
    logger.info("🔍 Starting new grad scrape")
    logger.info("Time (UTC): %s", datetime.now(timezone.utc).isoformat())

    try:
        rows = await asyncio.to_thread(fetch_new_grads)

        logger.info("Fetched %d new grad(s)", len(rows))

    except Exception:
        logger.exception("❌ Failed fetching new grads")
        return

    channel = await get_cached_channel("new_grads", NEW_GRADS_CHANNEL_ID)

    await _post_batch(rows, channel, "new_grads", "new_grads")

    elapsed = time.perf_counter() - start
    logger.info("✅ Scrape completed in %.2f seconds", elapsed)

    # Return scrape-cycle allocations
    gc.collect()

@check_new_grads.before_loop
async def before_new_grad_check():
    await bot.wait_until_ready()
    logger.info("Waiting to start new grads check")
    # Offset from the internship loop so the two scrape batches never run
    # (and hold their README soups) at the same time.
    await asyncio.sleep(90)


# Silver Wolf sees the universe as one big immersive sim — internship
# hunting is just another quest line to her. Greeting variants are index-
# aligned across languages so the language toggle keeps the same variant.
WELCOME_L10N = {
    "en": {
        "author": "Silver Wolf",
        "title": "👾 New Player Detected",
        "side_quest_name": "⚔️ Side Quest",
        "side_quest": (
            "Build more projects. Recruiters read commit history, not intentions — "
            "every repo you ship is EXP. Grind it."
        ),
        "quest_log_name": "🗺️ Quest Log",
        "quest_log": (
            f"**Main quest:** <#{INTERNSHIPS_CHANNEL_ID}> — fresh internship drops every 15 min\n"
            f"**New Game+:** <#{NEW_GRADS_CHANNEL_ID}> — new grad roles, for when you beat the campaign"
        ),
        "footer": "Aether Editing complete • CS Internship Bot",
        "button": "🌐 中文",
        "greetings": [
            (
                "Oh? A new player just spawned. Welcome, {mention}.\n\n"
                "Tutorial's short: grind LeetCode, farm referrals, clear the interview "
                "gauntlet. Difficulty: **Inferno**. ...Relax, you'll manage. Probably."
            ),
            (
                "Huh. My scanner picked up a fresh signal — {mention} just logged in.\n\n"
                "Pro tip from someone who's cracked tougher systems: recruiter ghosting "
                "isn't a bug, it's a feature. The internship feed here auto-updates, so "
                "camp it like a rare spawn."
            ),
            (
                "New character unlocked: {mention}.\n"
                "**Class:** Intern Hopeful. **Starting gear:** one résumé, zero replies.\n\n"
                "Don't sweat it — this server's basically a cheat code. Fresh postings "
                "drop every 15 minutes. You're welcome."
            ),
            (
                "{mention} has entered the game.\n\n"
                "I already ran a scan on your data. Projects could use a patch, but "
                "nothing Aether Editing can't fix. Check the postings channel and start "
                "queueing applications — it's a numbers game. Spam the attack button."
            ),
            (
                "Another one joins the run. Hey, {mention}.\n\n"
                "The universe is one big simulation, and internship season is its "
                "worst-designed side quest. Lucky for you, the walkthrough gets posted "
                "here automatically. Offer letter = your ultimate. Go build charge."
            ),
        ],
    },
    "zh": {
        "author": "银狼",
        "title": "👾 检测到新玩家",
        "side_quest_name": "⚔️ 支线任务",
        "side_quest": (
            "多做点项目。招聘官看的是 commit 记录，不是你的决心——"
            "每发布一个 repo 都是经验值。肝就完事了。"
        ),
        "quest_log_name": "🗺️ 任务日志",
        "quest_log": (
            f"**主线任务：**<#{INTERNSHIPS_CHANNEL_ID}> ——新实习岗位每 15 分钟刷新\n"
            f"**二周目：**<#{NEW_GRADS_CHANNEL_ID}> ——全职 New Grad 岗位，通关后解锁"
        ),
        "footer": "以太编辑完成 • CS Internship Bot",
        "button": "🌐 English",
        "greetings": [
            (
                "哦？新玩家刷新了。欢迎，{mention}。\n\n"
                "新手教程很短：刷 LeetCode，攒内推，通关面试连战。"
                "难度：**炼狱**。……放轻松，你能行的。大概吧。"
            ),
            (
                "嗯？我的扫描器捕捉到新信号——{mention} 刚上线。\n\n"
                "来自破解过更硬系统的人的忠告：HR 已读不回不是 bug，是特性。"
                "这里的实习频道会自动更新，像蹲稀有怪一样蹲着吧。"
            ),
            (
                "新角色解锁：{mention}。\n"
                "**职业：**实习候补。**初始装备：**一份简历，零回复。\n\n"
                "别慌——这个服务器基本算开挂。新岗位每 15 分钟刷新一次。不用谢。"
            ),
            (
                "{mention} 已进入游戏。\n\n"
                "你的数据我已经扫过了。项目经历需要打个补丁，"
                "不过没有以太编辑修不好的东西。去岗位频道排队投递吧——"
                "这是数量游戏，狂点攻击键。"
            ),
            (
                "又一个加入本局。嘿，{mention}。\n\n"
                "宇宙就是一场大型模拟游戏，而实习季是它设计最烂的支线任务。"
                "算你走运，攻略会自动发在这里。Offer = 你的终结技。去攒能量吧。"
            ),
        ],
    },
}


def build_welcome_embed(member, lang, greeting_idx):
    loc = WELCOME_L10N[lang]

    embed = discord.Embed(
        title=loc["title"],
        description=loc["greetings"][greeting_idx].format(mention=member.mention),
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow(),
    )

    embed.set_author(
        name=loc["author"],
        icon_url=BOT_AVATAR_URL,
    )

    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name=loc["side_quest_name"],
        value=loc["side_quest"],
        inline=False,
    )

    embed.add_field(
        name=loc["quest_log_name"],
        value=loc["quest_log"],
        inline=False,
    )

    embed.set_footer(
        text=loc["footer"],
        icon_url=BOT_AVATAR_URL,
    )

    return embed


async def send_welcome(member):
    channel = await get_cached_channel("welcome", WELCOME_CHANNEL_ID)

    greeting_idx = random.randrange(len(WELCOME_L10N["zh"]["greetings"]))
    embed = build_welcome_embed(member, "zh", greeting_idx)

    message = await channel.send(embed=embed)

    # Silver Wolf's reaction to a new spawn.
    for emoji in ("👾", "🎮", "💜"):
        await message.add_reaction(emoji)
        await asyncio.sleep(0.35)


@bot.event
async def on_member_join(member):
    try:
        await send_welcome(member)
    except Exception:
        logger.exception("Failed sending welcome message for member %s", member.id)


# Discord's own "X joined the server" system line is posted separately from
# on_member_join (in the guild's system channel), so we catch it here and
# delete it — Silver Wolf's embed is the only welcome we want to show.
@bot.event
async def on_message(message):
    if message.type is discord.MessageType.new_member:
        try:
            await message.delete()
        except discord.Forbidden:
            logger.warning(
                "Missing Manage Messages to delete system join line in #%s",
                getattr(message.channel, "name", message.channel.id),
            )
        except discord.HTTPException:
            logger.exception("Failed deleting system join message")
        return

    # Keep prefix/other message handling working (slash commands don't need
    # this, but drop-through is the safe default if any get added later).
    await bot.process_commands(message)


# Slash commands live in the commands/ package; register them here with the
# deps each needs from this module.
testwelcome_cmd.register(bot, send_welcome=send_welcome, logger=logger)
clearinternships_cmd.register(bot, logger=logger)
refreshembeds_cmd.register(
    bot, refresh_posted_embeds=refresh_posted_embeds, logger=logger
)


@bot.event
async def on_ready():
    global BOT_AVATAR_URL
    global COMMANDS_SYNCED

    BOT_AVATAR_URL = bot.user.display_avatar.url

    try:
        await cache_channels()
    except Exception:
        logger.exception("Failed caching Discord channels")

    # Sync commands once per process. Re-syncing on every reconnect can trigger
    # Discord rate limits quickly.
    #
    # Guild-scoped only: guild sync is instant, and registering the same
    # commands both globally and per-guild makes Discord list them twice.
    # The empty global sync below removes previously-registered global copies.
    if not COMMANDS_SYNCED:
        try:
            for guild in bot.guilds:
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)

            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            COMMANDS_SYNCED = True
        except Exception:
            logger.exception("Failed syncing slash commands")

    if not check_new_internships.is_running():
        check_new_internships.start()

    if not check_new_grads.is_running():
        check_new_grads.start()

    logger.info("Bot ready as %s", bot.user)


if __name__ == "__main__":
    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    bot.run(TOKEN, log_handler=None, reconnect=True)