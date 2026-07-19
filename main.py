import os
import random
import logging
import logging.handlers
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from github_internships import GithubInternships
from database import SupabaseDatabase
import threading
from fastapi import FastAPI
import uvicorn
from urllib.parse import quote
import asyncio

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID"))
INTERNSHIPS_CHANNEL_ID = int(os.getenv("INTERNSHIPS_CHANNEL_ID"))
NEW_GRADS_CHANNEL_ID = int(os.getenv("NEW_GRADS_CHANNEL_ID"))
CATEGORY_COLORS = {
    "Software Engineering": discord.Color.blue(),
    "AI / ML": discord.Color.purple(),
    "Data Science": discord.Color.green(),
    "Quant": discord.Color.gold(),
    "Product": discord.Color.orange(),
    "Other": discord.Color.light_grey(),
}

# Append + rotate so restarts don't wipe history; mirror to console so
# connect/disconnect events are visible in the terminal too.
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

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
discord_logger.addHandler(file_handler)
discord_logger.addHandler(console_handler)
intents = discord.Intents.default()
intents.members = True  # required for on_member_join
bot = commands.Bot(command_prefix="/", intents=intents)
github_internships = GithubInternships()
supabase_db = SupabaseDatabase()

app = FastAPI()


@app.get("/")
def home():
    return {"status": "Discord bot running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)


async def send_internship(internship):
    channel = await bot.fetch_channel(INTERNSHIPS_CHANNEL_ID)

    company = internship["company_info"]["company_name"]
    company_website = internship["company_info"]["company_website"]
    company_linkedin = internship["company_info"]["company_linkedin"]
    company_logo = internship["company_info"]["company_logo"]

    internship_id = internship["id"]
    title = internship["job_title"]
    location = internship["job_location"]
    category = internship["job_type"]
    url = internship["job_url"]
    posted = internship["job_posted_at"]

    embed = discord.Embed(
        title=f"🚀 {title}",
        url=url,
        color=CATEGORY_COLORS[category],
        timestamp=discord.utils.utcnow(),
    )

    embed.set_author(
        name=company,
        icon_url=company_logo if company_logo else bot.user.display_avatar.url,
    )

    embed.description = f"## {company}"


    # ----------------------------
    # Internship Details
    # ----------------------------

    embed.add_field(
        name="💻 Category",
        value=f"```{category}```",
        inline=True,
    )

    location_list = location.split(" | ")
    if len(location_list) > 5:
        location_formatted = "\n".join(
            f"• {loc}" for loc in location_list[:5]
        )
        location_formatted += f"\n• +{len(location_list)-5} more"
    else:
        location_formatted = "\n".join(
            f"• {loc}" for loc in location_list
        )

    embed.add_field(
        name="📍 Location",
        value=f"```{location_formatted}```",
        inline=True,
    )

    # Force next row
    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True,
    )

    embed.add_field(
        name="🗓️ Posted",
        value=f"```{posted}```",
        inline=True,
    )

    embed.add_field(
        name="💼 Type",
        value="```Internship```",
        inline=True,
    )

    # Force next section
    embed.add_field(
        name="\u200b",
        value="\u200b",
        inline=True,
    )

    embed.add_field(
        name="🟢 Apply",
        value=f"**[Open Internship ↗]({url})**",
        inline=False,
    )

    embed.add_field(
        name="📂 Source",
        value="[Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)",
        inline=True,
    )

    # Use stored company links, otherwise fall back to search
    website = (
        company_website
        if company_website
        else f"https://www.google.com/search?q={quote(company + ' careers')}"
    )

    linkedin = (
        company_linkedin
        if company_linkedin
        else f"https://www.linkedin.com/search/results/companies/?keywords={quote(company)}"
    )

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
        icon_url=bot.user.display_avatar.url,
    )

    message = await channel.send(embed=embed)
    supabase_db.update_internship_message_id(internship_id, message.id)

@tasks.loop(minutes=15)
async def check_new_internships():
    await asyncio.to_thread(github_internships.insert_internships)

    internships = await asyncio.to_thread(
        supabase_db.get_unsent_internships
    )

    for internship in internships:
        try:
            await send_internship(internship)

            await asyncio.to_thread(
                supabase_db.mark_as_sent,
                internship["id"],
            )

        except Exception:
            logging.exception("Failed sending internship")


@check_new_internships.before_loop
async def before_check():
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
        icon_url=bot.user.display_avatar.url,
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
        icon_url=bot.user.display_avatar.url,
    )

    return embed


async def send_welcome(member):
    channel = await bot.fetch_channel(WELCOME_CHANNEL_ID)

    greeting_idx = random.randrange(len(WELCOME_L10N["zh"]["greetings"]))
    embed = build_welcome_embed(member, "zh", greeting_idx)

    message = await channel.send(embed=embed)

    # Silver Wolf's reaction to a new spawn
    for emoji in ("👾", "🎮", "💜"):
        await message.add_reaction(emoji)


@bot.event
async def on_member_join(member):
    await send_welcome(member)


@bot.tree.command(
    name="testwelcome",
    description="Preview the Silver Wolf welcome message (uses you as the new member)",
)
@discord.app_commands.default_permissions(administrator=True)
async def testwelcome(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await send_welcome(interaction.user)
    await interaction.followup.send("Welcome message fired. Check the channel.")


@bot.event
async def on_ready():
    # Guild sync is instant; global sync can take up to an hour to propagate
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    await bot.tree.sync()
    if not check_new_internships.is_running():
        check_new_internships.start()


if __name__ == "__main__":
    threading.Thread(target=run_web_server).start()
    bot.run(TOKEN, log_handler=None, reconnect=True)
