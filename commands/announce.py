"""Silver Wolf update announcements.

When new features ship, post a patch-notes-style announcement to the announcement
channel — written in Silver Wolf's voice, framing the release like a game update /
new content drop. Reusable: any release calls post_update(bot, features).

A "feature" is a small dict the caller fills in (truthful facts only — the AI just
voices them, never invents):
    {
        "name":   "LeetCode Grind Channel",          # headline
        "what":   "daily problem + full breakdown",  # one plain line
        "how":    "post drops daily; /leetcode <n>", # how to access it
    }

The AI turns the whole list into a title + intro + one blurb per feature + outro,
all in-voice. Facts come from the dict; flavor is the wording only.
"""

import asyncio
import logging

import discord

from commands import gemma_client, persona
from commands.gemma_client import FAST_CHAIN

logger = logging.getLogger("announce")

_INSTR = """\
You are writing a PATCH-NOTES / UPDATE ANNOUNCEMENT for a Discord server of CS \
students grinding for internships. Voice = Silver Wolf (see persona above): cocky, \
deadpan, playful, secretly on their side. Frame the release like a new content \
drop / game patch ("patch's live", "new content unlocked", "pushed an update to \
your build") — but NATURAL, at most one flavor beat per 2-3 sentences.

You are given a JSON list of features that shipped. Every fact (name, what it does, \
how to access) is TRUE — voice it, never invent or exaggerate capabilities. Do NOT \
add features that aren't in the list.

Return STRICT JSON (no markdown, no extra keys):
{
  "title": "short punchy patch title, e.g. 'Patch Notes — New Content Drop'",
  "intro": "1-2 sentence Silver Wolf opener announcing there's an update. Casual.",
  "features": [
    {
      "name": "the feature name, kept recognizable (you may lightly flavor it)",
      "blurb": "2-3 sentences: what it does + WHY they'd care, in voice",
      "how": "one short plain line on how to access it (command / where). Keep the \
actual command or channel name EXACT — students need to copy it."
    }
  ],
  "outro": "1 sentence sign-off. Encouraging under the swagger."
}
Keep it tight and genuinely useful — someone should read it and know exactly what's \
new and how to use it. Match the ORDER and COUNT of the input features."""


def build_announcement(features):
    """Turn a feature list into a Silver Wolf announcement dict, or None."""
    if not features:
        return None
    import json

    feat_json = json.dumps(features, ensure_ascii=False, indent=2)
    prompt = (
        f"{persona.SILVER_WOLF_SYSTEM}\n\n{_INSTR}\n\n"
        f"--- FEATURES THAT SHIPPED ---\n{feat_json}\n--- END ---\n\nReturn the JSON."
    )
    data = gemma_client.ask_json_text(
        prompt, max_output_tokens=2000, temperature=0.6, chain=FAST_CHAIN
    )
    if not data or not data.get("features"):
        return None
    return data


def announcement_embed(data):
    """Render the announcement dict as a Discord embed."""
    embed = discord.Embed(
        title="🐺 " + (data.get("title") or "Patch Notes"),
        description=(data.get("intro") or "")[:2000],
        color=discord.Color.purple(),
    )
    for f in data.get("features", [])[:12]:
        name = f.get("name") or "New"
        blurb = f.get("blurb") or ""
        how = f.get("how") or ""
        val = blurb[:900]
        if how:
            val += f"\n\n**▶ How:** {how[:300]}"
        embed.add_field(name=f"✨ {name}", value=val[:1024], inline=False)
    if data.get("outro"):
        embed.set_footer(text=data["outro"][:200])
    return embed


async def post_update(bot, channel, features, get_db=None):
    """Generate + post a Silver Wolf announcement for `features` to `channel`.
    Returns the sent message, or None on failure."""
    data = await asyncio.to_thread(build_announcement, features)
    if not data:
        logger.warning("announce: failed generating announcement")
        return None
    embed = announcement_embed(data)
    try:
        return await channel.send(embed=embed)
    except Exception:
        logger.exception("announce: failed posting announcement")
        return None


# --- Pending release changelog (user-facing features shipped this cycle) -------
# Facts only — the AI voices these. Update this list as new features land, then
# fire /announce to post the Silver Wolf patch notes. Backend fixes that users
# never see (dup-insert fix, daily-post gate, etc.) are intentionally omitted.
PENDING_CHANGELOG = [
    {
        "name": "LeetCode Grind Channel",
        "what": "A daily LeetCode problem posts automatically — the full problem "
        "card with examples, constraints, and which companies ask it. Try it first; "
        "the solution's hidden behind a Reveal button. Reveal opens a Silver Wolf "
        "breakdown that explains the whole thing like you're new to it — the "
        "pattern, why it works, and edge cases — then gives you THREE full "
        "solutions (brute force → better → optimal, with the best one clearly "
        "marked) and buttons to flip between each one's complete code.",
        "how": "Auto-posts daily in the LeetCode channel. Use /leetcode "
        "<number|name|daily> for any problem on demand. React with the check mark "
        "when you solve one, then /streak to see your solve streak.",
    },
    {
        "name": "Smart Match Alerts",
        "what": "The bot now watches every new posting, AI-scores it against your "
        "resume, and DMs you the high-scoring roles automatically so a great match "
        "never slips past you.",
        "how": "Run /subscribe with the smart option on. Upload your resume first "
        "with /resume upload if you haven't.",
    },
    {
        "name": "More Internship Sources",
        "what": "Added the jobright.ai minisite feed as a new source, so more fresh "
        "SWE internship postings land in the channel. Duplicates are filtered so "
        "you never see the same role twice.",
        "how": "Nothing to do — new roles just show up in the internships channel.",
    },
    {
        "name": "New Grad Roles Are Flowing Again",
        "what": "Full-time new grad postings had quietly stalled — a posting glitch "
        "was blocking new roles from coming through. That's patched, and hundreds of "
        "backed-up new grad roles are now posting.",
        "how": "Check the new grad channel — the roles are landing there now.",
    },
    {
        "name": "Faster, Sharper Match Scoring",
        "what": "The match score got an animated progress readout so you're not "
        "staring at a blank screen, plus a Jump-to-posting button so the result no "
        "longer makes you scroll. Scoring is also more honest now — it caps the "
        "score when a required skill is missing instead of overrating a resume.",
        "how": "Click Score on any posting, or use /match.",
    },
    {
        "name": "Better Resume Tailoring",
        "what": "The resume tailor is more surgical: it only touches what actually "
        "needs changing, keeps your real numbers and tools intact, and no longer "
        "weakens strong bullet points. It also folds the review notes into one "
        "clean result.",
        "how": "Click Tailor on any posting.",
    },
    {
        "name": "Under-the-Hood Tune-Up",
        "what": "Trimmed the bot's memory footprint and fixed the crashes that were "
        "causing occasional downtime, so it stays up and responsive instead of "
        "falling over when things get busy.",
        "how": "Nothing to do — the bot's just more stable now.",
    },
]


def register(bot, *, announce_channel_id, get_db=None, logger=None):
    """Register /announce — admin-only trigger that posts the PENDING_CHANGELOG as
    a Silver Wolf patch-notes announcement. Restricted to members with Manage
    Server so a random user can't spam the announcement channel."""
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="announce",
        description="(Admin) Post the pending Silver Wolf patch notes",
    )
    @discord.app_commands.default_permissions(manage_guild=True)
    async def announce_slash(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not announce_channel_id:
            await interaction.followup.send(
                "No announcement channel configured.", ephemeral=True
            )
            return
        channel = bot.get_channel(announce_channel_id) or await bot.fetch_channel(
            announce_channel_id
        )
        msg = await post_update(bot, channel, PENDING_CHANGELOG, get_db)
        if msg:
            await interaction.followup.send(
                f"📢 Posted the patch notes to {channel.mention}.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Couldn't generate/post the announcement — check logs.",
                ephemeral=True,
            )

    log.info("Registered /announce")
