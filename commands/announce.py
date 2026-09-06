"""Silver Wolf update announcements.

When new features ship, post a patch-notes-style announcement to the announcement
channel — written in Silver Wolf's voice, framing the release like a game update /
new content drop. Reusable: any release calls post_update(bot, features).

A "feature" is a small dict the caller fills in (truthful facts only — the AI just
voices them, never invents):
    {
        "name":   "LeetCode Grind Channel",          # headline
        "what":   "daily problem + full breakdown",  # one plain line
        "how":    "post drops daily; /leetcode problem <n>", # how to access it
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
        blurb = (f.get("blurb") or f.get("what") or "").strip()
        how = (f.get("how") or "").strip()
        val = blurb[:900]
        if how:
            val += f"\n\n**▶ How:** {how[:300]}"
        val = val.strip()
        if not val:
            continue  # skip a feature the model returned empty (Discord rejects blank fields)
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
        "name": "GitHub Project Scout",
        "what": "Scans your public GitHub repos, ranks them against a job "
        "description, and hands back résumé-ready project blurbs, an improvement "
        "plan for your strongest repo, and a fresh project idea to build. It also "
        "reads each repo like an ATS — pulling a clean summary, the tech stack, a "
        "category (AI/ML, web, mobile, and so on), and the finish date — and files "
        "them so your projects are ready the moment a role needs them.",
        "how": "/resume github username:<your-handle> job:<paste the posting> "
        "— it remembers your username after the first run.",
    },
    {
        "name": "Tailor now checks your GitHub",
        "what": "When you tailor your résumé for a posting, a new 'Check my "
        "GitHub' button finds a repo that fits the role better than the projects "
        "already on your résumé — and offers to add it (with drafted bullets). "
        "Nothing changes unless you press Add.",
        "how": "Hit Tailor on any posting, then press 🔍 Check my GitHub.",
    },
    {
        "name": "Rebuilt DS&A Roadmap",
        "what": "The roadmap is now a real visual skill-tree, drawn as two "
        "connected graphs — Data Structures and Algorithms & Techniques. Every "
        "pattern is a node linked by arrows you can actually follow, laid out in "
        "difficulty bands (easy up top, harder at the bottom). It highlights the "
        "one thing to start now, marks what unlocks next, and tracks a personal "
        "study order that remembers where you are.",
        "how": "/leetcode roadmap",
    },
    {
        "name": "Persona toggle",
        "what": "You can now switch my Silver Wolf voice off for a neutral, "
        "professional assistant tone — handy if you want plain-and-simple output. "
        "It's on by default, and flipping it never changes the accuracy of "
        "anything I generate, just the flavor.",
        "how": "Admins: /test persona on | off | default.",
    },
]


def changelog_version(features=None):
    """A stable content hash of the changelog. Editing the changelog changes the
    hash, so the startup auto-announce posts a fresh announcement exactly once per
    distinct changelog and never re-posts the same one on restart."""
    import hashlib
    import json

    features = PENDING_CHANGELOG if features is None else features
    blob = json.dumps(features, sort_keys=True, ensure_ascii=False)
    return "cl_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


async def post_pending_if_new(bot, channel, get_db):
    """Auto-post the pending changelog ONCE, gated on the sent_announcements table
    by changelog hash. Safe to call on every startup: it posts only when the
    current changelog hasn't been announced yet, so a restart never double-posts,
    and editing the changelog announces the new one exactly once. Returns the sent
    message, or None if nothing was posted."""
    if not PENDING_CHANGELOG:
        return None
    version = changelog_version()
    db = get_db() if get_db else None
    if db is not None:
        if await asyncio.to_thread(db.was_announcement_sent, version):
            logger.info("announce: changelog %s already announced, skipping", version)
            return None
    msg = await post_update(bot, channel, PENDING_CHANGELOG, get_db)
    if msg and db is not None:
        await asyncio.to_thread(
            db.mark_announcement_sent, version, getattr(msg, "id", None)
        )
    return msg


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
