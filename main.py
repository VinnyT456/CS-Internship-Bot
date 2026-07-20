import asyncio
import logging
import logging.handlers
import os
import random
import threading
from urllib.parse import quote

import discord
import uvicorn
from discord.ext import commands, tasks
from dotenv import load_dotenv
from fastapi import FastAPI

from database import SupabaseDatabase
from github_internships import GithubInternships


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

CHANNEL_CACHE = {}
SEARCH_URL_CACHE = {}
BOT_AVATAR_URL = None
COMMANDS_SYNCED = False


# Append + rotate so restarts don't wipe history; mirror to console so
# connect/disconnect events are visible in the terminal too.
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

bot = commands.Bot(command_prefix="/", intents=intents)
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


def fetch_new_internships():
    github_internships = GithubInternships()
    supabase_db = SupabaseDatabase()

    github_internships.insert_internships()
    return supabase_db.get_unsent_internships()


def mark_internship_as_sent(internship_id):
    supabase_db = SupabaseDatabase()
    supabase_db.mark_as_sent(internship_id)


def update_internship_message_id(internship_id, message_id):
    supabase_db = SupabaseDatabase()
    supabase_db.update_internship_message_id(internship_id, message_id)


def enrich_company_batch():
    github_internships = GithubInternships()
    return github_internships.enrich_companies()


def get_sent_message_ids():
    supabase_db = SupabaseDatabase()
    return supabase_db.get_sent_message_ids()


def reset_internship_discord_state(internship_id):
    supabase_db = SupabaseDatabase()
    supabase_db.reset_internship_discord_state(internship_id)


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


async def send_internship(internship):
    #channel = await get_cached_channel("test_internships", TEST_INTERNSHIPS_CHANNEL_ID)
    channel = await get_cached_channel("internships", INTERNSHIPS_CHANNEL_ID)
    
    company_info = normalize_company_info(internship.get("company_info"))

    company = company_info.get("company_name") or internship.get("company_name") or "Unknown"
    company_website = company_info.get("company_website")
    company_linkedin = company_info.get("company_linkedin")
    company_logo = company_info.get("company_logo")

    internship_id = internship["id"]
    title = internship.get("job_title") or "Untitled Internship"
    location = internship.get("job_location") or "Unknown"
    category = internship.get("job_type") or "Other"
    url = internship.get("job_url")
    posted = internship.get("job_posted_at") or "Unknown"

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

    embed.description = f"## {company}"

    embed.add_field(
        name="💻 Category",
        value=f"```{truncate_embed_value(category, 1018)}```",
        inline=True,
    )

    embed.add_field(
        name="📍 Location",
        value=f"```{format_location(location)}```",
        inline=True,
    )

    # Force next row. Discord displays inline fields in rows of three.
    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True,
    )

    embed.add_field(
        name="🗓️ Posted",
        value=f"```{truncate_embed_value(posted, 1018)}```",
        inline=True,
    )

    embed.add_field(
        name="💼 Type",
        value="```Internship```",
        inline=True,
    )

    # Force next section.
    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True,
    )

    if url:
        embed.add_field(
            name="🟢 Apply",
            value=f"**[Open Internship ↗]({url})**",
            inline=False,
        )

    source_url = internship.get("source_repo")

    embed.add_field(
        name="📂 Source",
        value=f"[Source Repository]({source_url})",
        inline=True,
    )

    fallback_urls = get_fallback_search_urls(company)
    website = company_website or fallback_urls["website"]
    linkedin = company_linkedin or fallback_urls["linkedin"]

    embed.add_field(
        name="🔍 Research",
        value=f"[Company Website]({website}) • [LinkedIn]({linkedin})",
        inline=True,
    )

    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True,
    )

    embed.set_footer(
        text="CS Internship Bot • Auto-updated every 15 minutes",
        icon_url=BOT_AVATAR_URL,
    )

    message = await channel.send(embed=embed)

    await asyncio.to_thread(
        update_internship_message_id,
        internship_id,
        message.id,
    )


@tasks.loop(minutes=15)
async def check_new_internships():
    try:
        internships = await asyncio.to_thread(fetch_new_internships)
    except Exception:
        logger.exception("Failed fetching internships")
        return

    logger.info("Found %s unsent internships", len(internships))

    for internship in internships:
        internship_id = internship.get("id", "unknown")

        try:
            await send_internship(internship)

            await asyncio.to_thread(
                mark_internship_as_sent,
                internship["id"],
            )

            await asyncio.sleep(MESSAGE_SEND_DELAY_SECONDS)

        except discord.HTTPException as exc:
            logger.exception(
                "Discord API error while sending internship %s: %s",
                internship_id,
                exc,
            )
            await asyncio.sleep(10)

        except Exception:
            logger.exception("Failed sending internship %s", internship_id)


@check_new_internships.before_loop
async def before_check():
    await bot.wait_until_ready()


# 10-minute cadence: cheap no-op when backlog is empty (one DB query),
# clears a large backlog steadily (max 20 searches per tick) without the
# hour-long stall the first tick's startup race used to cause.
@tasks.loop(minutes=10)
async def enrich_companies_task():
    try:
        await asyncio.to_thread(enrich_company_batch)
    except Exception:
        logger.exception("Company enrichment batch failed")


@enrich_companies_task.before_loop
async def before_enrich():
    await bot.wait_until_ready()


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


@bot.tree.command(
    name="testwelcome",
    description="Preview the Silver Wolf welcome message (uses you as the new member)",
)
@discord.app_commands.default_permissions(administrator=True)
async def testwelcome(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        await send_welcome(interaction.user)
    except Exception:
        logger.exception("Failed sending test welcome message")
        await interaction.followup.send("Failed to send welcome message.")
        return

    await interaction.followup.send("Welcome message fired. Check the channel.")


@bot.tree.command(
    name="clearinternships",
    description="Delete all bot-posted internship messages (uses stored message IDs)",
)
@discord.app_commands.default_permissions(administrator=True)
async def clearinternships(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        channel = await get_cached_channel("internships", INTERNSHIPS_CHANNEL_ID)
        rows = await asyncio.to_thread(get_sent_message_ids)
    except Exception:
        logger.exception("Failed preparing clearinternships")
        await interaction.followup.send("Failed to fetch stored message IDs.")
        return

    if not rows:
        await interaction.followup.send("No stored message IDs — nothing to delete.")
        return

    deleted = 0
    missing = 0
    failed = 0

    for row in rows:
        message_id = row["discord_message_id"]

        # Retry transient Discord/network failures (e.g. 503s) so a blip
        # doesn't leave the message behind
        removed = False

        for attempt in range(3):
            try:
                await channel.get_partial_message(message_id).delete()
                deleted += 1
                removed = True
                break
            except discord.NotFound:
                # Already deleted by hand — still reset the row below
                missing += 1
                removed = True
                break
            except discord.HTTPException as e:
                logger.warning(
                    "Delete attempt %d/3 failed for message %s: %s",
                    attempt + 1,
                    message_id,
                    e,
                )
                if attempt < 2:
                    await asyncio.sleep(2)

        if not removed:
            logger.error("Giving up on message %s after 3 attempts", message_id)
            failed += 1
            continue

        # Message confirmed gone: clear the ID and mark unsent so the
        # internship can be posted again
        await asyncio.to_thread(reset_internship_discord_state, row["id"])

        # Stay under Discord's rate limit
        await asyncio.sleep(0.5)

    await interaction.followup.send(
        f"Deleted {deleted} message(s), {missing} already gone, {failed} failed.\n"
        "Cleared internships are marked unsent and will re-post on the next cycle."
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

    if not enrich_companies_task.is_running():
        enrich_companies_task.start()

    logger.info("Bot ready as %s", bot.user)


if __name__ == "__main__":
    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    bot.run(TOKEN, log_handler=None, reconnect=True)