"""A pinned-style reference message listing every slash command, posted once to
the CS-commands channel.

Idempotent: on each boot the bot scans the channel's recent history for its own
board message (tagged by a marker in the footer). If found it edits that message
in place; otherwise it sends a fresh one. So restarts refresh the content
instead of spamming duplicates.
"""

import logging

import discord

log = logging.getLogger("cs_internship_bot")

# Marker in the footer so we can find (and edit) our own board on restart.
BOARD_MARKER = "cmdboard:v1"

# Silver Wolf's signature violet (her hair / Aether Editing glow) — used for the
# guide's sidebar so the board reads as *hers*, not generic blurple.
SW_PURPLE = discord.Color.from_rgb(167, 139, 250)

# Grouped so the embed reads top-to-bottom by what a user wants to do.
COMMAND_GROUPS = [
    (
        "🔎 Recon & Loot",
        [
            ("/latest", "Freshest postings, hot off the spawn timer."),
            ("/search", "Filter the loot table by keyword, company, role, or location."),
            ("/saved", "Your stash — jobs you bookmarked for later."),
            ("/stats", "Live market intel. Read the meta before you queue."),
        ],
    ),
    (
        "📄 Resume Ops (my specialty)",
        [
            ("/resume", "Upload your résumé — I'll scan it into the system (PDF)."),
            ("/reviewresume", "I audit your résumé and flag every unpatched bug."),
            ("/match", "Score your résumé vs a posting. Know the fight before you enter."),
            ("/recommend", "I pick the runs you're actually built for."),
            ("/tailor", "Buttons on a posting — I Aether-Edit your résumé to fit it."),
        ],
    ),
    (
        "🎤 Prep & Ask",
        [
            ("/interview", "Boss-fight rehearsal — questions tuned to the role."),
            ("/helpme", "Stuck? Ping me. I've cracked harder systems than this."),
        ],
    ),
    (
        "🔔 Alerts & Profile",
        [
            ("/subscribe", "I'll DM you when a matching drop hits the feed."),
            ("/unsubscribe", "Mute the pings. Manage your alerts."),
            ("/profile", "Your save file — résumé, stash, subscriptions."),
        ],
    ),
    (
        "ℹ️ Info",
        [
            ("/help", "The full command list, if you insist on reading the manual."),
        ],
    ),
]

# Chinese mirror of COMMAND_GROUPS (same commands, Silver Wolf's 中文 voice).
COMMAND_GROUPS_ZH = [
    (
        "🔎 侦察 & 掉落",
        [
            ("/latest", "最新岗位，刚刷新出来热乎的。"),
            ("/search", "按关键词、公司、职位、地点筛选掉落表。"),
            ("/saved", "你的仓库——收藏起来待会儿看的岗位。"),
            ("/stats", "实时市场情报。排队前先读读 meta。"),
        ],
    ),
    (
        "📄 简历操作（我的强项）",
        [
            ("/resume", "上传简历——我把它扫进系统（PDF）。"),
            ("/reviewresume", "我审你的简历，把没修的 bug 全标出来。"),
            ("/match", "把简历和岗位对分。开打前先看清这场仗。"),
            ("/recommend", "我挑你真正 build 得起来的副本。"),
            ("/tailor", "岗位上的按钮——我用以太编辑把简历改到贴合。"),
        ],
    ),
    (
        "🎤 备战 & 提问",
        [
            ("/interview", "Boss 战彩排——按岗位调过的问题。"),
            ("/helpme", "卡住了？找我。比这更硬的系统我都破过。"),
        ],
    ),
    (
        "🔔 提醒 & 档案",
        [
            ("/subscribe", "有匹配的掉落进 feed，我私信你。"),
            ("/unsubscribe", "关掉提醒。管理你的订阅。"),
            ("/profile", "你的存档——简历、仓库、订阅。"),
        ],
    ),
    (
        "ℹ️ 信息",
        [
            ("/help", "完整命令列表，如果你非要看说明书的话。"),
        ],
    ),
]

# Buttons that live on every posting, worth calling out once here.
POSTING_BUTTONS = (
    "Every posting's got buttons: **Apply** • **Score** (how you match) • "
    "**Tailor** (I rewrite your résumé for it) • **Save** 🔖"
)
POSTING_BUTTONS_ZH = (
    "每个岗位都有按钮：**Apply** • **Score**（匹配度）• "
    "**Tailor**（我帮你改简历）• **Save** 🔖"
)

_BOARD_TEXT = {
    "en": {
        "title": "🐺 Silver Wolf's Command Guide ✦",
        "intro": (
            "Alright, listen up — here's every tool in the kit. Slash commands "
            "work anywhere in the server; just start typing `/` and the menu "
            "pops. Universe is a game, and I just handed you the console.\n​"
        ),
        "posting": "🖱️ Posting buttons",
        "posting_val": POSTING_BUTTONS,
        "groups": COMMAND_GROUPS,
        "footer": f"Aether Editing complete • {BOARD_MARKER}",
    },
    "zh": {
        "title": "🐺 银狼的命令指南 ✦",
        "intro": (
            "行，听好了——工具包里的东西都在这。斜杠命令在服务器任何地方都能用，"
            "打个 `/` 菜单就弹出来。宇宙是场游戏，控制台我刚递你手上了。\n​"
        ),
        "posting": "🖱️ 岗位按钮",
        "posting_val": POSTING_BUTTONS_ZH,
        "groups": COMMAND_GROUPS_ZH,
        "footer": f"以太编辑完成 • {BOARD_MARKER}",
    },
}


def build_board_embed(bot, lang="en"):
    t = _BOARD_TEXT.get(lang, _BOARD_TEXT["en"])
    embed = discord.Embed(
        title=t["title"],
        description=t["intro"],
        color=SW_PURPLE,
        timestamp=discord.utils.utcnow(),
    )
    for name, rows in t["groups"]:
        value = "\n".join(f"**{cmd}** — {desc}" for cmd, desc in rows)
        embed.add_field(name=name, value=value, inline=False)

    embed.add_field(name=t["posting"], value=t["posting_val"], inline=False)

    icon = bot.user.display_avatar.url if bot.user else None
    embed.set_footer(text=t["footer"], icon_url=icon)
    return embed


def _is_our_board(message, bot):
    return (
        message.author.id == (bot.user.id if bot.user else None)
        and message.embeds
        and (message.embeds[0].footer.text or "").endswith(BOARD_MARKER)
    )


# The board lives forever, so its language button must persist: timeout=None +
# a fixed custom_id. Clicks are routed by main.on_interaction (a raw gateway
# event that fires for any custom_id, registered or not), so it keeps working
# after restarts. A LangToggleView (30-min timeout) would instead go dead.
_BOARD_LANG_CID = "board:lang"  # custom_id: board:lang:<en|zh>


class BoardLangView(discord.ui.View):
    """One 🌐 button that flips the command board between EN and 中文 in place.
    Persistent (timeout=None) so it keeps working after restarts. The next
    language is encoded in the button's custom_id, so no per-message state."""

    def __init__(self, bot, next_lang="zh"):
        super().__init__(timeout=None)
        self._bot = bot
        label = "🌐 中文" if next_lang == "zh" else "🌐 English"
        self.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"{_BOARD_LANG_CID}:{next_lang}",
            )
        )


async def handle_board_lang(bot, interaction):
    """on_interaction router for the board's 🌐 button. Edits the board embed to
    the requested language and flips the button to offer the other one."""
    parts = (interaction.data or {}).get("custom_id", "").split(":")
    lang = parts[2] if len(parts) > 2 else "en"
    if lang not in ("en", "zh"):
        lang = "en"
    embed = build_board_embed(bot, lang)
    next_lang = "en" if lang == "zh" else "zh"
    try:
        await interaction.response.edit_message(embed=embed, view=BoardLangView(bot, next_lang))
    except discord.HTTPException:
        log.exception("Failed toggling command board language")


async def post_or_update_board(bot, channel):
    """Send the board, or edit the existing one in place. Best-effort."""
    embed = build_board_embed(bot)
    view = BoardLangView(bot, "zh")
    try:
        async for message in channel.history(limit=50):
            if _is_our_board(message, bot):
                await message.edit(embed=embed, view=view)
                log.info("Refreshed command board in #%s", channel.id)
                return message
    except discord.Forbidden:
        log.warning("Missing Read Message History for command board channel")
    except Exception:
        log.exception("Failed scanning for existing command board")

    try:
        message = await channel.send(embed=embed, view=view)
        log.info("Posted new command board in #%s", channel.id)
        return message
    except Exception:
        log.exception("Failed posting command board")
        return None
