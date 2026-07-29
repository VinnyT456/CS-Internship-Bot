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

# Grouped so the embed reads top-to-bottom by what a user wants to do.
COMMAND_GROUPS = [
    (
        "🔎 Browse & Search",
        [
            ("/latest", "Browse the newest internship or new-grad postings."),
            ("/search", "Search by keyword, company, role, location, or category."),
            ("/saved", "View and manage the jobs you've saved."),
            ("/stats", "Market trends and live database stats."),
        ],
    ),
    (
        "📄 Resume & AI",
        [
            ("/resume", "Upload, view, or delete your resume (PDF)."),
            ("/reviewresume", "AI feedback and fixes for your resume."),
            ("/match", "Score your resume against a specific posting."),
            ("/recommend", "Personalized internship picks from your resume."),
            ("/tailor", "Buttons on a posting — tailor your resume to it."),
        ],
    ),
    (
        "🎤 Prep & Ask",
        [
            ("/interview", "Practice questions tailored to a role or company."),
            ("/helpme", "Ask the AI career assistant anything."),
        ],
    ),
    (
        "🔔 Alerts & Profile",
        [
            ("/subscribe", "Get DM alerts by category and keyword."),
            ("/unsubscribe", "Manage or remove your alert subscriptions."),
            ("/profile", "Your resume, saved jobs, and subscriptions."),
        ],
    ),
    (
        "ℹ️ Info",
        [
            ("/help", "Full command list and how to use them."),
        ],
    ),
]

# Buttons that live on every posting, worth calling out once here.
POSTING_BUTTONS = (
    "On every job posting: **Apply** • **Score** (resume match) • "
    "**Tailor** (rewrite for that role) • **Save** 🔖"
)


def build_board_embed(bot):
    embed = discord.Embed(
        title="🧭 Command Guide",
        description=(
            "Everything the bot can do. Slash commands work anywhere in the "
            "server — start typing `/` to see them.\n​"
        ),
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    for name, rows in COMMAND_GROUPS:
        value = "\n".join(f"**{cmd}** — {desc}" for cmd, desc in rows)
        embed.add_field(name=name, value=value, inline=False)

    embed.add_field(name="🖱️ Posting buttons", value=POSTING_BUTTONS, inline=False)

    icon = bot.user.display_avatar.url if bot.user else None
    embed.set_footer(text=f"CS Internship Bot • {BOARD_MARKER}", icon_url=icon)
    return embed


def _is_our_board(message, bot):
    return (
        message.author.id == (bot.user.id if bot.user else None)
        and message.embeds
        and (message.embeds[0].footer.text or "").endswith(BOARD_MARKER)
    )


async def post_or_update_board(bot, channel):
    """Send the board, or edit the existing one in place. Best-effort."""
    embed = build_board_embed(bot)
    try:
        async for message in channel.history(limit=50):
            if _is_our_board(message, bot):
                await message.edit(embed=embed)
                log.info("Refreshed command board in #%s", channel.id)
                return message
    except discord.Forbidden:
        log.warning("Missing Read Message History for command board channel")
    except Exception:
        log.exception("Failed scanning for existing command board")

    try:
        message = await channel.send(embed=embed)
        log.info("Posted new command board in #%s", channel.id)
        return message
    except Exception:
        log.exception("Failed posting command board")
        return None
