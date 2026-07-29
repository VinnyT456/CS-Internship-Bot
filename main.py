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
from commands import (
    ai_commands as ai_cmd,
    clearinternships as clearinternships_cmd,
    commands_board,
    help_command as help_cmd,
    job_ai,
    latest as latest_cmd,
    profile as profile_cmd,
    refreshembeds as refreshembeds_cmd,
    resume as resume_cmd,
    saved as saved_cmd,
    search as search_cmd,
    stats as stats_cmd,
    subscribe as subscribe_cmd,
    testwelcome as testwelcome_cmd,
)


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID"))
INTERNSHIPS_CHANNEL_ID = int(os.getenv("INTERNSHIPS_CHANNEL_ID"))
#TEST_INTERNSHIPS_CHANNEL_ID = int(os.getenv("TEST_INTERNSHIPS_CHANNEL_ID"))
NEW_GRADS_CHANNEL_ID = int(os.getenv("NEW_GRADS_CHANNEL_ID"))
# Optional: channel for the persistent command-guide message.
_COMMANDS_CHANNEL_RAW = os.getenv("COMMANDS_CHANNEL_ID")
COMMANDS_CHANNEL_ID = int(_COMMANDS_CHANNEL_RAW) if _COMMANDS_CHANNEL_RAW else None

CATEGORY_COLORS = {
    "Software Engineering": discord.Color.blue(),
    "AI / ML": discord.Color.purple(),
    "Data Science": discord.Color.green(),
    "Quant": discord.Color.gold(),
    "Product": discord.Color.orange(),
    "Other": discord.Color.light_grey(),
}

MESSAGE_SEND_DELAY_SECONDS = 1.2
MAX_POSTS_PER_CYCLE = 400

ENRICH_WORKERS = int(os.getenv("ENRICH_WORKERS", "4"))
REFRESH_CONCURRENCY = int(os.getenv("REFRESH_CONCURRENCY", "5"))

# Rolling closed-status sweep: each 30-min tick re-checks the oldest-checked
# CLOSED_CHECK_BATCH posted-open rows (2 workers keeps it light on the 512 MB
# instance). Closed postings get their message edited to the closed embed.
CLOSED_CHECK_BATCH = int(os.getenv("CLOSED_CHECK_BATCH", "150"))
CLOSED_CHECK_WORKERS = int(os.getenv("CLOSED_CHECK_WORKERS", "2"))

CHANNEL_CACHE = {}
SEARCH_URL_CACHE = {}
BOT_AVATAR_URL = None
# Interaction ids already handled — guards against Discord's duplicate
# component dispatch causing a double ack (40060).
_HANDLED_INTERACTIONS = set()
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
    if not rows:
        return []

    try:
        enriched = details.enrich_jobs(rows, max_workers=ENRICH_WORKERS)
    except Exception:
        logger.exception("Detail enrichment failed; posting unenriched rows")
        return rows
    finally:
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
    if COMMANDS_CHANNEL_ID:
        await get_cached_channel("commands", COMMANDS_CHANNEL_ID)


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


def format_location_inline(location, limit=2):
    parts = [loc.strip() for loc in (location or "").split(" | ") if loc.strip()]
    if not parts:
        return None

    if len(parts) <= limit:
        return " · ".join(parts)

    return f"{' · '.join(parts[:limit])} +{len(parts) - limit} more"


def format_posted(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    return f"{parsed:%b} {parsed.day}, {parsed.year}"


def format_bullets(items, limit=3, max_item_len=180):
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return None

    shown = items[:limit]
    lines = [f"• {truncate_embed_value(i, max_item_len)}" for i in shown]
    if len(items) > limit:
        lines.append(f"• +{len(items) - limit} more")

    return truncate_embed_value("\n".join(lines))


SECTION_PREVIEW = 3
SECTION_PREVIEW_LONG = 2  # when the preview runs tall, show fewer bullets
SECTION_MAX_LINES = 4     # collapse to 2 bullets once 3 would wrap past this
WRAP_CHARS = 50           # ~chars per display line on a narrow (mobile) client
SKILLS_PREVIEW = 6


def _wrapped_lines(item):
    """Estimate how many display lines a bullet wraps to on a narrow client."""
    return max(1, -(-len(item) // WRAP_CHARS))  # ceil division


def _section_preview_count(items):
    """Show 3 bullets normally, but drop to 2 once the first 3 would wrap past
    SECTION_MAX_LINES total — so a wall of long text can't dominate the card."""
    head = items[:SECTION_PREVIEW]
    total_lines = sum(_wrapped_lines(i) for i in head)
    if total_lines > SECTION_MAX_LINES:
        return SECTION_PREVIEW_LONG
    return SECTION_PREVIEW


def format_section(items, expanded=False, max_item_len=200):
    """'• ' bullets for a section value. The title is the field NAME (bold in
    Discord), set by the caller. Collapsed shows 2-3 items (fewer when they're
    long) plus a '+N more' line."""
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not items:
        return None

    preview = _section_preview_count(items)
    shown = items if expanded else items[:preview]
    lines = [f"• {truncate_embed_value(i, max_item_len)}" for i in shown]

    hidden = len(items) - len(shown)
    if hidden > 0:
        lines.append(f"*+{hidden} more*")

    return truncate_embed_value("\n".join(lines))


def format_skills(tags, expanded=False):
    """Skill chips for a section value. Title is the field name (bold)."""
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if not tags:
        return None

    shown = tags if expanded else tags[:SKILLS_PREVIEW]
    text = "  ".join(f"`{t}`" for t in shown)

    hidden = len(tags) - len(shown)
    if hidden > 0:
        text += f"\n*+{hidden} more*"

    return truncate_embed_value(text, 1018)


def job_has_more(internship):
    def section_over(field):
        items = [i for i in (internship.get(field) or []) if str(i).strip()]
        # Compare against the same preview count the section will actually
        # render, so a job that collapses to 2 long bullets still gets a button.
        return len(items) > _section_preview_count([str(i) for i in items])

    def skills_over(field):
        items = [i for i in (internship.get(field) or []) if str(i).strip()]
        return len(items) > SKILLS_PREVIEW

    return (
        section_over("job_responsibilities")
        or section_over("job_requirements")
        or skills_over("job_tags")
    )


def format_pay(internship):
    pay = internship.get("salary_desc")
    if pay:
        return pay
    low = internship.get("comp_min")
    if not low:
        return None
    high = internship.get("comp_max") or low
    return f"${low}k/yr" if low == high else f"${low}k - ${high}k/yr"


_KIND_LABELS = {
    "internship": "Internship",
    "internships": "Internship",
    "new grad": "New Grad",
    "new_grad": "New Grad",
    "new_grads": "New Grad",
    "new grads": "New Grad",
}


def _job_type_label(internship, kind):
    explicit = internship.get("employment_type")
    if explicit:
        return explicit
    return _KIND_LABELS.get(str(kind).strip().lower(), str(kind).title())


def build_internship_embed(internship, type="internship", expanded=False):
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

    # Top half mirrors the original layout: rocket-prefixed role as the title,
    # the company as a big '##' header in the description, logo in the author
    # line. The new enrichment (summary, comp, sections, buttons) hangs below.
    embed = discord.Embed(
        title=f"🚀 {title}",
        url=url,
        color=CATEGORY_COLORS.get(category, discord.Color.blurple()),
        timestamp=discord.utils.utcnow(),
    )

    embed.set_author(
        name=company,
        icon_url=company_logo or BOT_AVATAR_URL,
    )

    # Company as the big header, matching the original top half. The summary
    # prose stays removed (per the earlier request); enriched detail lives in
    # the sections below.
    embed.description = f"## {company}"

    stats = [
        ("💰 Compensation", format_pay(internship)),
        ("🏢 Work Model", internship.get("work_model")),
        ("💼 Type", _job_type_label(internship, type)),
        ("💻 Category", category),
        ("📍 Location", format_location(location)),
        ("🗓️ Posted", posted),
    ]
    present = [(name, value) for name, value in stats if value]
    for name, value in present:
        embed.add_field(
            name=name,
            value=f"```{truncate_embed_value(str(value), 1018)}```",
            inline=True,
        )
    for _ in range((3 - len(present) % 3) % 3):
        embed.add_field(name="​", value="​", inline=True)

    # Titles are the field NAMES (Discord renders those bold), content in the
    # value — cleaner than a '###' heading inside the value.
    responsibilities = format_section(
        internship.get("job_responsibilities"), expanded=expanded
    )
    if responsibilities:
        embed.add_field(name="📋 What you'll do", value=responsibilities, inline=False)

    requirements = format_section(
        internship.get("job_requirements"), expanded=expanded
    )
    if requirements:
        embed.add_field(name="✅ Requirements", value=requirements, inline=False)

    skills = format_skills(internship.get("job_tags"), expanded=expanded)
    if skills:
        embed.add_field(name="🏷️ Skills", value=skills, inline=False)

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
        "⚠️ **This posting has closed** — it's no longer accepting applicants."
    )
    posted = internship.get("job_posted_at")
    if posted:
        embed.add_field(name="🗓️ Was posted", value=f"```{posted}```", inline=True)
    location = internship.get("job_location")
    if location:
        embed.add_field(
            name="📍 Location",
            value=f"```{format_location(location)}```",
            inline=True,
        )
    embed.set_footer(text="CS Internship Bot • Posting closed")
    return embed


# --- Message components (buttons) ---------------------------------------


def _fetch_row_for_buttons(table, row_id):
    resp = (
        get_db()
        .supabase.table(table)
        .select("*, company_info(*)")
        .eq("id", row_id)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def build_job_view(internship, table, expanded=False, saved_flash=False):
    # One row, ordered by UX priority: primary action (Apply) → high-value
    # resume tools (Score, Tailor) → Save → Show more (least urgent). Discord
    # caps a row at 5 and auto-sizes each button to its label — there's no
    # width control, so order + label length are the only levers.
    #
    # saved_flash: render the Save button as a green "✅ Saved" for the brief
    # confirmation flip after a click.
    view = discord.ui.View(timeout=None)
    row_id = internship["id"]
    url = internship.get("job_url")

    if url:
        view.add_item(
            discord.ui.Button(
                label="Apply",
                style=discord.ButtonStyle.link,
                url=url,
                emoji="🟢",
                row=0,
            )
        )

    # Resume features — wired to the router but not yet backed by real logic;
    # they reply with a "coming soon" placeholder until the pipeline is built.
    view.add_item(
        discord.ui.Button(
            label="Score",
            style=discord.ButtonStyle.secondary,
            custom_id=f"jobsec:score:{table}:{row_id}",
            emoji="📊",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Tailor",
            style=discord.ButtonStyle.secondary,
            custom_id=f"jobsec:tailor:{table}:{row_id}",
            emoji="✍️",
            row=0,
        )
    )

    view.add_item(
        discord.ui.Button(
            label="Saved" if saved_flash else "Save",
            style=(
                discord.ButtonStyle.success
                if saved_flash
                else discord.ButtonStyle.secondary
            ),
            custom_id=f"jobsec:save:{table}:{row_id}",
            emoji="✅" if saved_flash else "🔖",
            row=0,
        )
    )

    if job_has_more(internship):
        # The custom_id carries the CURRENT expanded state (1/0) so the handler
        # can flip it deterministically — no inferring state from the embed
        # text, which was fragile and made bullets flicker on toggle.
        view.add_item(
            discord.ui.Button(
                label="Less" if expanded else "More",
                style=discord.ButtonStyle.primary,
                custom_id=f"jobsec:more:{table}:{row_id}:{1 if expanded else 0}",
                emoji="📄",
                row=0,
            )
        )

    return view


async def _handle_more(interaction, table, row_id, currently_expanded):
    # Guard against a double-dispatch (Discord can deliver the same component
    # interaction more than once): if it's already been acknowledged, bail
    # rather than calling response.* twice and hitting 40060.
    if interaction.response.is_done():
        return

    row = await asyncio.to_thread(_fetch_row_for_buttons, table, int(row_id))
    if not row:
        await interaction.response.send_message(
            "That posting is no longer available.", ephemeral=True
        )
        return

    # Flip the state the button was rendered with — deterministic, no parsing
    # the embed.
    expanded = not currently_expanded
    embed = build_internship_embed(row, expanded=expanded)
    view = build_job_view(row, table, expanded=expanded)
    await interaction.response.edit_message(embed=embed, view=view)


SAVE_FLASH_SECONDS = 2


def _rebuild_view_from_message(message, table, row_id, expanded, saved_flash):
    """Rebuild the button row from the message's OWN components — link buttons
    keep their url, custom_id buttons keep their id — flipping only the Save
    button's label/style. No DB fetch, so the interaction ack stays well under
    Discord's 3s deadline."""
    view = discord.ui.View(timeout=None)
    for comp_row in getattr(message, "components", []):
        for comp in getattr(comp_row, "children", []):
            if getattr(comp, "type", None) != discord.ComponentType.button:
                continue
            cid = getattr(comp, "custom_id", None)
            is_save = bool(cid) and ":save:" in cid

            if getattr(comp, "url", None):
                view.add_item(
                    discord.ui.Button(
                        label=comp.label,
                        style=discord.ButtonStyle.link,
                        url=comp.url,
                        emoji=comp.emoji,
                        row=0,
                    )
                )
            elif is_save:
                view.add_item(
                    discord.ui.Button(
                        label="Saved" if saved_flash else "Save",
                        style=(
                            discord.ButtonStyle.success
                            if saved_flash
                            else discord.ButtonStyle.secondary
                        ),
                        custom_id=cid,
                        emoji="✅" if saved_flash else "🔖",
                        row=0,
                    )
                )
            else:
                view.add_item(
                    discord.ui.Button(
                        label=comp.label,
                        style=comp.style,
                        custom_id=cid,
                        emoji=comp.emoji,
                        row=0,
                    )
                )
    return view


async def _handle_save(interaction, table, row_id):
    if interaction.response.is_done():
        return

    # ACK FIRST, with NO DB work before it — Discord expires a component
    # interaction after 3s. Rebuild the button row from the message's own
    # components (no fetch) and flip Save -> "✅ Saved"; this edit is the ack.
    expanded = _current_expanded(interaction.message)
    flashed = _rebuild_view_from_message(
        interaction.message, table, row_id, expanded, saved_flash=True
    )
    try:
        await interaction.response.edit_message(view=flashed)
    except discord.HTTPException:
        logger.exception("Failed acking Save click")
        return

    # Interaction is acknowledged — safe to hit the DB now.
    user = interaction.user
    db = get_db()
    try:
        user_uuid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        if user_uuid:
            await asyncio.to_thread(
                db.toggle_saved_job, user_uuid, table, int(row_id)
            )
    except Exception:
        logger.exception("Save DB write failed for %s %s", table, row_id)

    # Revert the button label after the flash window.
    await asyncio.sleep(SAVE_FLASH_SECONDS)
    normal = _rebuild_view_from_message(
        interaction.message, table, row_id, expanded, saved_flash=False
    )
    try:
        await interaction.edit_original_response(view=normal)
    except discord.HTTPException:
        logger.exception("Failed reverting Save button flash")


def _current_expanded(message):
    """Read the expand state off the message's More/Less button custom_id
    (…:more:table:id:<0|1>). Defaults to collapsed if not found."""
    for row in getattr(message, "components", []):
        for comp in getattr(row, "children", []):
            cid = getattr(comp, "custom_id", "") or ""
            if ":more:" in cid:
                return cid.rsplit(":", 1)[-1] == "1"
    return False


async def _run_job_ai(interaction, action, table, row_id):
    """Shared body for the Score/Tailor buttons. The interaction was already
    deferred in on_interaction; this just runs the AI and sends the result.
    Fallback-defers only if that somehow didn't happen."""
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            logger.warning("Score/Tailor interaction could not be deferred")
            return

    runner = job_ai.run_score if action == "score" else job_ai.run_tailor
    embed, file, error = await runner(get_db(), interaction.user, table, row_id)
    if error:
        await interaction.followup.send(error, ephemeral=True)
    elif file is not None:
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


async def _handle_score(interaction, table, row_id):
    await _run_job_ai(interaction, "score", table, row_id)


async def _handle_tailor(interaction, table, row_id):
    await _run_job_ai(interaction, "tailor", table, row_id)


async def handle_section_button(interaction):
    # custom_id: jobsec:more:<table>:<id>:<0|1>  or  jobsec:save:<table>:<id>
    parts = interaction.data.get("custom_id", "").split(":")
    if len(parts) < 4:
        return
    action, table, row_id = parts[1], parts[2], parts[3]

    if action == "more":
        currently_expanded = len(parts) > 4 and parts[4] == "1"
        await _handle_more(interaction, table, row_id, currently_expanded)
    elif action == "save":
        await _handle_save(interaction, table, row_id)
    elif action == "score":
        await _handle_score(interaction, table, row_id)
    elif action == "tailor":
        await _handle_tailor(interaction, table, row_id)


async def send_internship(internship, channel, table, type="internship"):
    embed = build_internship_embed(internship, type)
    view = build_job_view(internship, table)

    message = await channel.send(embed=embed, view=view)

    await asyncio.to_thread(
        update_internship_message_id, internship["id"], message.id, table
    )


def _sub_matches(sub, internship):
    """A subscription matches a job when its category (if set) equals the job's
    category AND its keyword (if set) appears in the company or title."""
    cat = sub.get("category")
    if cat and cat != (internship.get("job_type") or ""):
        return False
    kw = (sub.get("keyword") or "").strip().lower()
    if kw:
        haystack = (
            f"{internship.get('company_name', '')} {internship.get('job_title', '')}"
        ).lower()
        if kw not in haystack:
            return False
    return True


async def notify_subscribers(internship, table, kind):
    """DM subscribers whose filters match this newly-posted job. Best-effort:
    a blocked DM or a bad id is logged and skipped."""
    subs = await asyncio.to_thread(get_db().get_all_subscriptions)
    if not subs:
        return

    matched = [s for s in subs if _sub_matches(s, internship)]
    if not matched:
        return

    embed = build_internship_embed(internship, kind)
    for sub in matched:
        try:
            user = await bot.fetch_user(int(sub["discord_id"]))
            await user.send(
                content=f"🔔 New match for your **{sub.get('category') or 'alert'}** subscription:",
                embed=embed,
            )
            await asyncio.sleep(0.5)  # gentle on the DM rate limit
        except (discord.Forbidden, discord.NotFound):
            continue  # DMs closed or user gone — skip
        except Exception:
            logger.exception("Failed DMing subscriber %s", sub.get("discord_id"))


async def _post_batch(rows, channel, kind, table):
    logger.info("Found %s unsent %s", len(rows), kind)

    if len(rows) > MAX_POSTS_PER_CYCLE:
        logger.info(
            "Capping this cycle to %d of %d %s; the rest post next cycle",
            MAX_POSTS_PER_CYCLE, len(rows), kind,
        )
        rows = rows[:MAX_POSTS_PER_CYCLE]

    rows = await asyncio.to_thread(enrich_rows, rows, table)

    for internship in rows:
        internship_id = internship.get("id", "unknown")

        try:
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

            # Best-effort alert DMs to matching subscribers.
            await notify_subscribers(internship, table, kind)

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


REFRESH_TARGETS = (
    ("internships", "internships", INTERNSHIPS_CHANNEL_ID),
    ("new_grads", "new_grads", NEW_GRADS_CHANNEL_ID),
)


async def refresh_posted_embeds(kind="internship", progress=None, limit=None,
                                only_table=None):
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
                view = build_job_view(row, table)
                await message.edit(embed=embed, view=view)
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


def _check_closed_batch(rows):
    """Concurrently check a batch of rows for closure. Returns a list of
    (row, is_closed) where is_closed is True/False/None (None = undetermined,
    leave the row alone). Blocking httpx, so run via asyncio.to_thread."""
    from concurrent.futures import ThreadPoolExecutor

    def check(row):
        return row, details.check_job_closed(row.get("detail_url"))

    try:
        with ThreadPoolExecutor(max_workers=CLOSED_CHECK_WORKERS) as pool:
            results = list(pool.map(check, rows))
    finally:
        details.close_http_clients()
        gc.collect()
    return results


CLOSED_CHECK_TARGETS = (
    ("internships", "internships", INTERNSHIPS_CHANNEL_ID),
    ("new_grads", "new_grads", NEW_GRADS_CHANNEL_ID),
)


async def sweep_closed_status():
    """Rolling closed-status sweep. Each run re-checks the oldest-checked
    posted-open rows; any found closed get their DB row flagged and their
    Discord message edited to the closed embed."""
    db = get_db()

    for table, cache_key, channel_id in CLOSED_CHECK_TARGETS:
        rows = await asyncio.to_thread(
            db.get_rows_to_recheck, table, CLOSED_CHECK_BATCH
        )
        if not rows:
            continue

        results = await asyncio.to_thread(_check_closed_batch, rows)

        closed_rows = [row for row, is_closed in results if is_closed is True]
        logger.info(
            "Closed-check %s: %d checked, %d newly closed",
            table, len(results), len(closed_rows),
        )

        channel = await get_cached_channel(cache_key, channel_id)
        for row in closed_rows:
            await asyncio.to_thread(db.mark_closed, row["id"], table)
            message_id = row.get("discord_message_id")
            if not message_id:
                continue
            try:
                message = await channel.fetch_message(int(message_id))
                # Re-fetch the full row so the closed embed shows title/company.
                full = await asyncio.to_thread(
                    _fetch_row_for_buttons, table, row["id"]
                )
                embed = build_closed_embed(full or row)
                # No buttons on a dead posting.
                await message.edit(embed=embed, view=None)
            except discord.NotFound:
                # Message was deleted by hand — clear the stale id.
                await asyncio.to_thread(
                    update_internship_message_id, row["id"], None, table
                )
            except discord.HTTPException:
                logger.exception(
                    "Failed editing closed message %s in %s", message_id, table
                )
            await asyncio.sleep(MESSAGE_SEND_DELAY_SECONDS)

        # Stamp every row we checked (open or closed) so it rotates to the back.
        await asyncio.to_thread(
            db.mark_checked, [row["id"] for row, _ in results], table
        )


@tasks.loop(minutes=30)
async def check_closed_status():
    logger.info("=" * 60)
    logger.info("🔎 Starting closed-status sweep")
    start = time.perf_counter()
    try:
        await sweep_closed_status()
    except Exception:
        logger.exception("❌ Closed-status sweep failed")
    logger.info("✅ Closed-status sweep done in %.1fs", time.perf_counter() - start)
    gc.collect()


@check_closed_status.before_loop
async def before_closed_check():
    await bot.wait_until_ready()
    # Offset from both scrape loops so the three don't run at once on the small
    # instance.
    await asyncio.sleep(300)


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
    await asyncio.sleep(90)



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

    for emoji in ("👾", "🎮", "💜"):
        await message.add_reaction(emoji)
        await asyncio.sleep(0.35)


@bot.event
async def on_member_join(member):
    try:
        await send_welcome(member)
    except Exception:
        logger.exception("Failed sending welcome message for member %s", member.id)


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

    await bot.process_commands(message)


testwelcome_cmd.register(bot, send_welcome=send_welcome, logger=logger)
clearinternships_cmd.register(bot, logger=logger)
refreshembeds_cmd.register(
    bot, refresh_posted_embeds=refresh_posted_embeds, logger=logger
)
latest_cmd.register(
    bot, build_embed=build_internship_embed, get_db=get_db, logger=logger
)
search_cmd.register(
    bot, build_embed=build_internship_embed, get_db=get_db, logger=logger
)
saved_cmd.register(
    bot, build_embed=build_internship_embed, get_db=get_db, logger=logger
)
stats_cmd.register(bot, get_db=get_db, logger=logger)
help_cmd.register(bot)
resume_cmd.register(bot, get_db=get_db, logger=logger)
ai_cmd.register(bot, get_db=get_db, logger=logger)
profile_cmd.register(bot, get_db=get_db, logger=logger)
subscribe_cmd.register(bot, get_db=get_db, logger=logger)


@bot.event
async def on_interaction(interaction):
    if interaction.type is not discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith("jobsec:"):
        return

    # Discord can deliver the same component interaction more than once (gateway
    # resume / duplicate dispatch). Each delivery is a fresh Interaction object,
    # so `response.is_done()` can't see the earlier ack — that's the 40060
    # "already acknowledged". Dedupe by interaction id instead.
    if interaction.id in _HANDLED_INTERACTIONS:
        return
    _HANDLED_INTERACTIONS.add(interaction.id)
    if len(_HANDLED_INTERACTIONS) > 1000:
        _HANDLED_INTERACTIONS.clear()

    # Score/Tailor take seconds (Gemma) — ack immediately, as the very first
    # await, so the 3s interaction token can't expire before the handler runs.
    # more/save respond their own way and aren't pre-acked.
    action = custom_id.split(":")[1] if ":" in custom_id else ""
    if action in ("score", "tailor"):
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.NotFound:
            logger.warning("jobsec interaction expired before defer")
            return
        except discord.HTTPException as e:
            if getattr(e, "code", None) == 40060:
                return
            logger.exception("Failed early defer for jobsec button")
            return
    try:
        await handle_section_button(interaction)
    except Exception:
        logger.exception("Section button handler failed")


@bot.event
async def on_ready():
    global BOT_AVATAR_URL
    global COMMANDS_SYNCED

    BOT_AVATAR_URL = bot.user.display_avatar.url

    try:
        await cache_channels()
    except Exception:
        logger.exception("Failed caching Discord channels")

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

    if COMMANDS_CHANNEL_ID:
        try:
            channel = await get_cached_channel("commands", COMMANDS_CHANNEL_ID)
            await commands_board.post_or_update_board(bot, channel)
        except Exception:
            logger.exception("Failed posting command board")

    if not check_new_internships.is_running():
        check_new_internships.start()

    if not check_new_grads.is_running():
        check_new_grads.start()

    if not check_closed_status.is_running():
        check_closed_status.start()

    # Warm the Gemma client so the first AI command isn't cold.
    try:
        from commands import gemma_client
        asyncio.create_task(asyncio.to_thread(gemma_client.warm_up))
    except Exception:
        logger.exception("Failed scheduling Gemma warm-up")

    logger.info("Bot ready as %s", bot.user)


if __name__ == "__main__":
    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    bot.run(TOKEN, log_handler=None, reconnect=True)