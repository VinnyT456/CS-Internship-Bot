"""LeetCode grind channel — daily problem + Silver Wolf explainer, on-demand
lookup, and solve streaks.

Pieces:
  - build_daily_embed(problem)      -> the spoiler-safe public embed (problem,
                                       tags, companies, hints — NO solution).
  - RevealView                      -> persistent '🔓 Reveal Solution' button;
                                       clicking DMs nothing, sends an EPHEMERAL
                                       Silver Wolf walkthrough only to the clicker.
  - post_daily(bot, channel, get_db) -> fetch daily, post embed + view, add ✅.
  - register(bot, get_db, logger)   -> /leetcode <query> and /streak.
  - handle_reveal / handle_solve_react -> routed from main.py.

Difficulty is the embed color. The Silver Wolf voice lives in the explainer prose
(commands/leetcode_ai.py); the reference solution stays clean, professional code.
"""

import asyncio
import logging

import discord

from commands import leetcode_ai
from leetcode import leetcode_api

logger = logging.getLogger("leetcode_cmd")

_DIFF_COLOR = {
    "Easy": discord.Color.from_rgb(67, 181, 129),   # green
    "Medium": discord.Color.from_rgb(250, 166, 26),  # amber
    "Hard": discord.Color.from_rgb(237, 66, 69),     # red
}
_DIFF_EMOJI = {"Easy": "🟢", "Medium": "🟡", "Hard": "🔴"}
SOLVE_EMOJI = "✅"


def _problem_list_lines(rows, limit=15):
    """Format light problem rows (from the API list endpoints or slug lists) into
    embed lines: difficulty dot + linked title. Truncates to `limit`."""
    out = []
    for r in rows[:limit]:
        dm = _DIFF_EMOJI.get(r.get("difficulty"), "⚪")
        title = r.get("title") or r.get("slug") or "?"
        url = r.get("url") or (
            f"https://leetcode.com/problems/{r.get('slug')}/" if r.get("slug") else None
        )
        num = r.get("id")
        head = f"#{num} " if num else ""
        out.append(f"{dm} [{head}{title}]({url})" if url else f"{dm} {head}{title}")
    return "\n".join(out) or "_none found_"

# Small Silver Wolf avatar for the embed author line (data-free URL — Discord
# hotlinks it; falls back gracefully if unreachable).
_SW_ICON = (
    "https://static.wikia.nocookie.net/houkai-star-rail/images/6/69/"
    "Character_Silver_Wolf_Icon.png"
)


def _acceptance_bar(ac):
    """A tiny 10-cell bar for acceptance rate, e.g. 45% -> ▰▰▰▰▱▱▱▱▱▱ 45%."""
    if not isinstance(ac, (int, float)):
        return None
    filled = max(0, min(10, round(ac / 10)))
    return "▰" * filled + "▱" * (10 - filled) + f"  {ac:.0f}%"


def _strip_examples(body, limit=600):
    """Trim a problem statement for the card: keep the ask, drop the long
    Example/Constraints dumps (they're one click away on LeetCode)."""
    if not body:
        return ""
    cut = body
    for marker in ("Example 1", "Example:", "Constraints:", "Follow-up:"):
        i = cut.find(marker)
        if i > 80:  # keep at least the problem ask before cutting
            cut = cut[:i]
            break
    cut = cut.strip()
    if len(cut) > limit:
        cut = cut[: limit - 1].rstrip() + "…"
    return cut


def _extract_examples(body, keep=2, limit=1000):
    """Pull the first `keep` worked examples out of the problem body, formatted
    for an embed field. These are load-bearing for actually solving it, so the
    card keeps them (up to the embed field limit)."""
    if not body or "Example" not in body:
        return None
    import re

    # Split on "Example N:" markers; each chunk is one example's text.
    parts = re.split(r"Example\s*\d*\s*:", body)
    examples = [p.strip() for p in parts[1:] if p.strip()]  # parts[0] = the ask
    if not examples:
        return None

    out = []
    for i, ex in enumerate(examples[:keep], 1):
        # stop an example bleeding into Constraints/Follow-up
        for stop in ("Constraints:", "Follow-up:"):
            j = ex.find(stop)
            if j != -1:
                ex = ex[:j].strip()
        # tidy Input/Output/Explanation onto their own lines
        ex = re.sub(r"\s*(Input:|Output:|Explanation:)", r"\n**\1**", ex).strip()
        out.append(f"**Example {i}**{'' if ex.startswith(chr(10)) else ' '}{ex}")
    text = "\n\n".join(out)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _extract_constraints(body, limit=1000):
    """Pull the Constraints block — the bounds dictate the right approach (an
    n up to 1e5 rules out O(n²)), so a solver needs them on the card."""
    if not body or "Constraints:" not in body:
        return None
    tail = body.split("Constraints:", 1)[1]
    # cut off a trailing Follow-up section if present
    for stop in ("Follow-up:", "Follow up:"):
        k = tail.find(stop)
        if k != -1:
            tail = tail[:k]
    lines = [ln.strip(" •-\t") for ln in tail.splitlines() if ln.strip(" •-\t")]
    if not lines:
        return None
    text = "\n".join(f"• `{ln}`" for ln in lines[:8])
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


# custom_id: leet:reveal:<slug>  (slug carries the problem so the button is
# stateless and survives a bot restart).
_REVEAL_PREFIX = "leet:reveal:"


def build_daily_embed(problem, *, daily=True):
    """Spoiler-safe embed: what the problem is + hints + who asks it. No solution.

    Layout, top → bottom:
      author line  ·  🐺 LeetCode Daily / On-Demand
      title (links to LeetCode)  ·  🟢/🟡/🔴 #123 Two Sum
      stat row (inline)          ·  Difficulty | Acceptance bar | # Asked by
      🏷️ tags line
      📋 the ask (examples stripped — one click away)
      💡 hints (spoiler-tagged, try-first)
      🏢 asked by (top companies)
      footer                     ·  how to use it
    """
    diff = problem.get("difficulty") or ""
    color = _DIFF_COLOR.get(diff, discord.Color.blurple())
    demoji = _DIFF_EMOJI.get(diff, "⚪")

    title = problem.get("title") or "LeetCode Problem"
    num = problem.get("id")

    embed = discord.Embed(
        title=f"{demoji}  #{num} · {title}",
        url=problem.get("url") or None,
        color=color,
    )
    embed.set_author(
        name="LeetCode Daily" if daily else "LeetCode · On-Demand",
        icon_url=_SW_ICON,
    )

    # --- scannable stat row (inline fields sit side by side) ---
    embed.add_field(name="Difficulty", value=f"{demoji} **{diff or '—'}**", inline=True)
    bar = _acceptance_bar(problem.get("ac_rate"))
    if bar:
        embed.add_field(name="Acceptance", value=bar, inline=True)
    companies = leetcode_api.companies_for(problem.get("slug"), limit=8)
    if companies:
        embed.add_field(name="Asked by", value=f"🏢 **{len(companies)}+** companies", inline=True)

    # --- tags as their own quiet line ---
    tags = problem.get("tags") or []
    if tags:
        embed.add_field(
            name="🏷️ Topics",
            value=" · ".join(f"`{t}`" for t in tags[:6]),
            inline=False,
        )

    # --- roadmap tie-in: which DS&A patterns this problem trains + how to learn ---
    learn = leetcode_api.roadmap_patterns_for_tags(tags)
    if learn:
        picks = learn[:3]
        val = " · ".join(f"**{name}** (`/leetcode learn {key.replace('_', ' ')}`)" for key, name in picks)
        embed.add_field(
            name="📚 Learn the pattern",
            value=val[:1024],
            inline=False,
        )

    # --- the ask, kept separate from examples/constraints so each is scannable ---
    full = problem.get("content") or ""
    body = _strip_examples(full, limit=700)
    if body:
        embed.add_field(name="📋 The Problem", value=body, inline=False)

    # Examples are load-bearing for solving — the card keeps them.
    examples = _extract_examples(full, keep=2, limit=1000)
    if examples:
        embed.add_field(name="🧾 Examples", value=examples, inline=False)

    # Constraints dictate the approach (bounds → which complexity is allowed).
    constraints = _extract_constraints(full, limit=1000)
    if constraints:
        embed.add_field(name="📐 Constraints", value=constraints, inline=False)

    # --- progressive hints, spoiler-tagged so nothing's given away at a glance ---
    hints = problem.get("hints") or []
    if hints:
        # hints carry raw LeetCode HTML (<code>…</code>) — clean it like the body.
        lines = [
            f"`{i}` ||{leetcode_api._html_to_text(h)[:220]}||"
            for i, h in enumerate(hints[:3], 1)
        ]
        embed.add_field(
            name="💡 Hints — tap to reveal, one at a time",
            value="\n".join(lines),
            inline=False,
        )

    # --- who asks it (names, freq-sorted) ---
    if companies:
        pretty = "  ".join(f"`{c.replace('-', ' ').title()}`" for c in companies)
        embed.add_field(name="🏢 Companies", value=pretty[:1024], inline=False)

    embed.set_footer(
        text=f"🐺 Try it first → Reveal for the full breakdown → react {SOLVE_EMOJI} when you clear it"
    )
    return embed


class RevealView(discord.ui.View):
    """Persistent view holding the Reveal button. Stateless — the slug rides in
    the custom_id, so it keeps working after a restart (timeout=None)."""

    def __init__(self, slug):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="🔓 Reveal Solution",
                style=discord.ButtonStyle.primary,
                custom_id=f"{_REVEAL_PREFIX}{slug}",
            )
        )
        # A convenience link to the problem on LeetCode.
        # (link buttons need a url; added by caller when available)


def _split_complexity(text):
    """Best-effort split of a complexity line into (time, space) for two inline
    fields. Falls back to (whole text, None) if it can't find both."""
    if not text:
        return None, None
    import re

    t = re.search(r"time[^,;]*?(O\([^)]*\))", text, re.I)
    s = re.search(r"space[^,;]*?(O\([^)]*\))", text, re.I)
    if t and s:
        return f"`{t.group(1)}`", f"`{s.group(1)}`"
    return text[:200], None


# A blank spacer field — Discord renders U+2800 (braille blank) as an empty line,
# giving real vertical breathing room between sections.
_SPACER = "⠀"


def _spacer(embed):
    embed.add_field(name=_SPACER, value=_SPACER, inline=False)


def build_breakdown_embed(problem, ex):
    """The main ephemeral breakdown embed — the ELI5 teaching walkthrough (prose
    only; code lives in the switchable code embeds via ApproachView).

    Sections are separated by blank spacer fields for breathing room:
      author  ·  🐺 Silver Wolf · Breakdown
      title   ·  🔓 #123 Two Sum
      intro (voice)
      ─ 🎯 What it's really asking (ELI5)
      ─ 🧩 The Pattern (badge)  +  💭 why / when (ELI5)
      ─ 🗺️ The Approaches (list of 3, optimal starred)
      ─ 🕳️ Edge cases
      footer  ·  outro
    """
    diff = problem.get("difficulty") or ""
    color = _DIFF_COLOR.get(diff, discord.Color.blurple())
    demoji = _DIFF_EMOJI.get(diff, "⚪")
    num = problem.get("id")

    e = discord.Embed(
        title=f"🔓  {demoji} #{num} · {problem.get('title')}",
        url=problem.get("url") or None,
        color=color,
    )
    e.set_author(name="Silver Wolf · Breakdown", icon_url=_SW_ICON)
    if ex.get("intro"):
        e.description = ex["intro"][:600]

    if ex.get("restate"):
        e.add_field(name="🎯 What it's really asking", value=ex["restate"][:1024], inline=False)
        _spacer(e)

    if ex.get("pattern"):
        e.add_field(name="🧩 The Pattern", value=f"**{ex['pattern']}**", inline=False)
    if ex.get("why_pattern"):
        e.add_field(name="💭 What it is & when to use it", value=ex["why_pattern"][:1024], inline=False)
    _spacer(e)

    # Summary of the 3 approaches so they see the menu before flipping code.
    apps = ex.get("approaches") or []
    if apps:
        lines = []
        for a in apps:
            star = " ⭐ **optimal**" if a.get("is_optimal") else ""
            cx = f" — `{a.get('complexity','').split(',')[0].strip()}`" if a.get("complexity") else ""
            lines.append(f"**{a.get('name','Approach')}**{star}{cx}")
        e.add_field(
            name=f"🗺️ {len(apps)} Ways to Crack It",
            value="\n".join(lines)[:1024],
            inline=False,
        )
        e.add_field(
            name=_SPACER,
            value="_Use the buttons below to flip through each solution's code._",
            inline=False,
        )

    gotchas = ex.get("gotchas") or []
    if gotchas:
        _spacer(e)
        val = "\n".join(f"⚠️ {g}" for g in gotchas)
        e.add_field(name="🕳️ Edge cases (hidden mechanics)", value=val[:1024], inline=False)

    if ex.get("outro"):
        e.set_footer(text="🐺 " + ex["outro"][:200])
    return e


def _code_embeds(problem, approach, idx, total):
    """A single approach's FULL code as a list of embeds — the header embed (idea +
    complexity + first code chunk) plus continuation embeds for any overflow, so a
    long solution renders COMPLETE across multiple embeds, never truncated."""
    diff = problem.get("difficulty") or ""
    color = _DIFF_COLOR.get(diff, discord.Color.blurple())
    optimal = approach.get("is_optimal")
    name = approach.get("name", "Approach")
    tag = "⭐ OPTIMAL" if optimal else f"Approach {idx + 1}/{total}"

    e = discord.Embed(
        title=f"💻 {name}",
        color=discord.Color.from_rgb(67, 181, 129) if optimal else color,
    )
    e.set_author(name=f"{tag} · Python")

    if approach.get("idea"):
        e.description = approach["idea"][:600]

    if approach.get("complexity"):
        tcx, scx = _split_complexity(approach["complexity"])
        if tcx and scx:
            e.add_field(name="⏱️ Time", value=tcx, inline=True)
            e.add_field(name="🧠 Space", value=scx, inline=True)
        elif tcx:
            e.add_field(name="⏱️ Complexity", value=tcx, inline=False)

    code = (approach.get("code") or "").strip()
    chunks = _code_chunks(code, first_budget=4096 - len(e.description or "") - 24)
    e.description = (e.description or "") + f"\n\n```python\n{chunks[0]}\n```"
    e.set_footer(text="Study it, don't just paste it. 别退游啊.")

    embeds = [e]
    # Long solutions overflow into continuation embeds so the FULL code always
    # shows — never truncated. Each continuation is its own code block.
    for extra in chunks[1:]:
        cont = discord.Embed(
            color=discord.Color.from_rgb(67, 181, 129) if optimal else color,
            description=f"```python\n{extra}\n```",
        )
        embeds.append(cont)
    return embeds


def _code_chunks(code, first_budget, rest_budget=3900):
    """Split code into embed-description-sized chunks WITHOUT cutting mid-line, so
    every line of the full solution renders across one or more embeds."""
    chunks, cur, budget = [], [], max(first_budget, 500)
    size = 0
    for line in code.split("\n"):
        add = len(line) + 1
        if size + add > budget and cur:
            chunks.append("\n".join(cur))
            cur, size, budget = [], 0, rest_budget
        cur.append(line)
        size += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [""]


class _LearnView(discord.ui.View):
    """A single '✅ Mark as learned' button under a /learn lesson. Ephemeral +
    short-lived, so it holds the pattern key directly (no persistence needed)."""

    def __init__(self, pattern_key, pattern_name, get_db):
        super().__init__(timeout=900)
        self.pattern_key = pattern_key
        self.pattern_name = pattern_name
        self.get_db = get_db
        btn = discord.ui.Button(
            label="✅ Mark as learned", style=discord.ButtonStyle.success
        )
        btn.callback = self._on_learned
        self.add_item(btn)

    async def _on_learned(self, interaction: discord.Interaction):
        db = self.get_db()
        u = interaction.user
        uid = await asyncio.to_thread(
            db.get_or_create_user, u.id, u.name, u.display_name
        )
        if uid:
            await asyncio.to_thread(db.mark_pattern_learned, uid, self.pattern_key)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"🐺 **{self.pattern_name}** logged. One more patched into your kit — check `/leetcode roadmap`.",
            view=self,
        )


class ApproachView(discord.ui.View):
    """Ephemeral view with one button per approach — flips the code embed in place.
    The optimal one is a green (success) button; others are secondary. Stateless
    across restarts isn't needed (ephemeral, short-lived), so it holds the data."""

    def __init__(self, problem, ex):
        super().__init__(timeout=900)  # 15 min — plenty for a study session
        self.problem = problem
        self.approaches = ex.get("approaches") or []
        for i, a in enumerate(self.approaches):
            optimal = a.get("is_optimal")
            label = a.get("name", f"Approach {i + 1}")
            if len(label) > 40:
                label = label[:37] + "…"
            btn = discord.ui.Button(
                label=("⭐ " + label) if optimal else label,
                style=discord.ButtonStyle.success if optimal else discord.ButtonStyle.secondary,
                custom_id=f"appr:{i}",
                row=0 if i < 3 else 1,
            )
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, i):
        async def cb(interaction: discord.Interaction):
            approach = self.approaches[i]
            embeds = _code_embeds(self.problem, approach, i, len(self.approaches))
            await interaction.response.edit_message(embeds=embeds, view=self)
        return cb


async def _reveal_for(problem, interaction):
    """Build + send the ephemeral Silver Wolf breakdown for a problem: the ELI5
    teaching embed, then a code embed (opened on the OPTIMAL approach) with buttons
    to flip between all 3 solutions."""
    ex = await asyncio.to_thread(leetcode_ai.build_explanation, problem)
    if not ex:
        await interaction.followup.send(
            "系统警告 — couldn't generate the breakdown right now. Try again in a bit.",
            ephemeral=True,
        )
        return

    # 1) the teaching walkthrough (prose only)
    await interaction.followup.send(
        embed=build_breakdown_embed(problem, ex), ephemeral=True
    )

    # 2) the code, opened on the optimal approach, with approach-switch buttons
    approaches = ex.get("approaches") or []
    if approaches:
        opt_idx = next(
            (i for i, a in enumerate(approaches) if a.get("is_optimal")), 0
        )
        embeds = _code_embeds(problem, approaches[opt_idx], opt_idx, len(approaches))
        view = ApproachView(problem, ex)
        await interaction.followup.send(embeds=embeds, view=view, ephemeral=True)


async def handle_reveal(interaction):
    """Routed from main.py on a 'leet:reveal:<slug>' button click. Sends the
    walkthrough EPHEMERALLY (only the clicker sees it — spoiler-safe for others)."""
    custom_id = (interaction.data or {}).get("custom_id", "")
    slug = custom_id[len(_REVEAL_PREFIX):] if custom_id.startswith(_REVEAL_PREFIX) else ""
    if not slug:
        return
    problem = await asyncio.to_thread(leetcode_api.get_problem, slug)
    if not problem:
        await interaction.followup.send(
            "Couldn't fetch that problem right now.", ephemeral=True
        )
        return
    await _reveal_for(problem, interaction)


async def post_daily(bot, channel, get_db=None):
    """Fetch today's daily, post the spoiler-safe embed + Reveal button, seed the
    ✅ react. Returns the sent message, or None."""
    problem = await asyncio.to_thread(leetcode_api.get_daily)
    if not problem:
        logger.warning("leetcode: no daily problem fetched")
        return None
    embed = build_daily_embed(problem, daily=True)
    view = RevealView(problem["slug"])
    if problem.get("url"):
        view.add_item(
            discord.ui.Button(
                label="Open on LeetCode", style=discord.ButtonStyle.link,
                url=problem["url"],
            )
        )
    msg = await channel.send(embed=embed, view=view)
    try:
        await msg.add_reaction(SOLVE_EMOJI)
    except Exception:
        logger.exception("leetcode: failed seeding solve react")
    return msg


async def _already_posted_history(channel, bot, slug, *, scan=25):
    """Fallback dedup when no DB is available: True if a recent bot message
    already carries this problem's Reveal button. Scans the last `scan` messages."""
    try:
        async for m in channel.history(limit=scan):
            if m.author and m.author.id == bot.user.id and _slug_from_message(m) == slug:
                return True
    except Exception:
        logger.exception("leetcode: failed scanning channel history")
    return False


async def post_daily_if_missing(bot, channel, get_db=None):
    """Post today's daily ONLY if it isn't already recorded as sent. Gated on the
    leetcode_daily_posts table (per calendar date) so the scheduled loop and the
    startup catch-up never double-post and a day is never skipped. Falls back to a
    channel-history scan if no DB is wired. Returns the sent message, or None."""
    problem = await asyncio.to_thread(leetcode_api.get_daily)
    if not problem:
        logger.warning("leetcode: no daily problem fetched")
        return None

    # problem['date'] is the daily's own date (YYYY-MM-DD) from the API; that's
    # the calendar key we gate on, not the local wall clock.
    day = problem.get("date")
    db = get_db() if get_db else None

    if db is not None and day:
        if await asyncio.to_thread(db.was_daily_posted, day):
            logger.info("leetcode: daily for %s already posted, skipping", day)
            return None
    elif await _already_posted_history(channel, bot, problem["slug"]):
        logger.info("leetcode: today's daily already in channel, skipping")
        return None

    msg = await post_daily(bot, channel, get_db)
    if msg and db is not None and day:
        await asyncio.to_thread(
            db.mark_daily_posted, day, problem["slug"], getattr(msg, "id", None)
        )
    return msg


# --- ✅ react → solve tracking ------------------------------------------------
async def handle_solve_react(bot, payload, get_db, *, added):
    """A ✅ react on a bot LeetCode message toggles the user's solve record.
    `added` True on add, False on remove. Called from on_raw_reaction_* in main.py.
    Returns True if it handled a LeetCode message (so main can stop)."""
    if str(payload.emoji) != SOLVE_EMOJI:
        return False
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return False
    try:
        msg = await channel.fetch_message(payload.message_id)
    except Exception:
        return False
    # Only our LeetCode posts: authored by the bot, carrying a leet:reveal button.
    if not msg.author or msg.author.id != bot.user.id:
        return False
    slug = _slug_from_message(msg)
    if not slug:
        return False
    if payload.user_id == bot.user.id:  # ignore the seeded react
        return True

    db = get_db()
    member = payload.member or (await bot.fetch_user(payload.user_id))
    difficulty = _difficulty_from_message(msg)
    user_uuid = await asyncio.to_thread(
        db.get_or_create_user, payload.user_id,
        getattr(member, "name", None), getattr(member, "display_name", None),
    )
    if not user_uuid:
        return True
    if not added:
        await asyncio.to_thread(db.unmark_leetcode_solved, user_uuid, slug)
        return True

    # Solved: schedule a spaced-repetition review (+3 days) and capture topics for
    # /weakspots. Topics come from the problem's tags (fetched once, best effort).
    from datetime import datetime, timezone, timedelta

    review_at = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    topics = None
    try:
        prob = await asyncio.to_thread(leetcode_api.get_problem, slug)
        if prob and prob.get("tags"):
            topics = ",".join(prob["tags"])
    except Exception:
        pass
    await asyncio.to_thread(
        db.mark_leetcode_solved, user_uuid, slug, difficulty,
        "solved", topics, review_at,
    )

    # Streak milestone: DM a little Silver Wolf hype at 3/7/14/30/50/100-day marks.
    try:
        await _maybe_milestone_dm(bot, db, user_uuid, member)
    except Exception:
        logger.exception("leetcode: milestone DM failed")
    return True


_STREAK_MILESTONES = {3, 7, 14, 30, 50, 100, 200, 365}


async def _maybe_milestone_dm(bot, db, user_uuid, member):
    """If the user's current solve streak just hit a milestone, DM them. Best
    effort — a DM failure (closed DMs) is swallowed."""
    solves = await asyncio.to_thread(db.get_leetcode_solves, user_uuid)
    streak = _consecutive_day_streak(solves)
    if streak not in _STREAK_MILESTONES:
        return
    line = {
        3: "3-day streak. Warming up — don't let it drop now.",
        7: "7 days straight. A full week cleared. 隐藏分 climbing.",
        14: "Two weeks. This is a real grind now, not a fluke.",
        30: "30-DAY STREAK. Month-long run. That's T0 discipline. 秒了.",
        50: "50 days. Most people quit by now. You're built different.",
        100: "💯 100 DAYS. Certified no-lifer (respect). This is elite.",
        200: "200 days. At this point the leetcode boss fears YOU.",
        365: "A FULL YEAR. 我独自满级. Absolute legend status.",
    }.get(streak, f"{streak}-day streak!")
    try:
        user = member if hasattr(member, "send") else await bot.fetch_user(
            getattr(member, "id", member)
        )
        await user.send(f"🔥 **{line}**\n— Silver Wolf")
    except Exception:
        pass


def _slug_from_message(msg):
    """Pull the problem slug out of a posted LeetCode message via its Reveal
    button custom_id."""
    for row in msg.components:
        for comp in getattr(row, "children", []):
            cid = getattr(comp, "custom_id", "") or ""
            if cid.startswith(_REVEAL_PREFIX):
                return cid[len(_REVEAL_PREFIX):]
    return None


def _difficulty_from_message(msg):
    for e in msg.embeds:
        d = e.description or ""
        for k in ("Easy", "Medium", "Hard"):
            if k in d:
                return k
    return None


# --- slash commands -----------------------------------------------------------
def register(bot, *, get_db, logger=None):
    """Register the /leetcode command group — all LeetCode + DS&A learning tools
    live under one group (/leetcode <subcommand>) to keep the top-level list clean."""
    log = logger or logging.getLogger("cs_internship_bot")

    group = discord.app_commands.Group(
        name="leetcode", description="LeetCode practice, learning, and stats"
    )

    @group.command(
        name="problem",
        description="Get a Silver Wolf breakdown of any LeetCode problem",
    )
    @discord.app_commands.describe(
        query="Problem number, slug, or 'daily' for today's challenge"
    )
    async def leetcode_cmd(interaction: discord.Interaction, query: str = "daily"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        q = (query or "daily").strip().lower()
        if q in ("daily", "today", ""):
            problem = await asyncio.to_thread(leetcode_api.get_daily)
        else:
            # slugify a title query: "two sum" -> "two-sum"; leave ids/slugs as-is
            key = q if (q.isdigit() or "-" in q) else q.replace(" ", "-")
            problem = await asyncio.to_thread(leetcode_api.get_problem, key)
        if not problem:
            await interaction.followup.send(
                f"Couldn't find a problem for `{query}`. Try a number (e.g. `1`) "
                "or slug (e.g. `two-sum`).",
                ephemeral=True,
            )
            return
        if problem.get("paid_only"):
            await interaction.followup.send(
                f"**{problem['title']}** is LeetCode Premium-only — I can't pull "
                "its full content. Pick a free one?",
                ephemeral=True,
            )
            return
        # Show the problem embed + the full breakdown (this IS on-demand, so no
        # spoiler gate — they asked for it).
        overview = build_daily_embed(problem, daily=False)
        await interaction.followup.send(embed=overview, ephemeral=True)
        await _reveal_for(problem, interaction)

    @group.command(
        name="streak", description="Your LeetCode solve streak in the grind channel"
    )
    async def streak_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = get_db()
        user = interaction.user
        user_uuid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        solves = await asyncio.to_thread(db.get_leetcode_solves, user_uuid) if user_uuid else []
        total = len(solves)
        by_diff = {"Easy": 0, "Medium": 0, "Hard": 0}
        for s in solves:
            d = s.get("difficulty")
            if d in by_diff:
                by_diff[d] += 1
        streak = _consecutive_day_streak(solves)

        embed = discord.Embed(
            title=f"🐺 {user.display_name}'s Grind Log",
            color=discord.Color.blurple(),
        )
        if total == 0:
            embed.description = (
                "No clears logged yet. Go react ✅ on a problem you solved — "
                "I'll keep score. 别退游啊."
            )
        else:
            embed.description = (
                f"**{total}** problem{'s' if total != 1 else ''} cleared  ·  "
                f"🔥 **{streak}**-day streak"
            )
            embed.add_field(
                name="Breakdown",
                value=(
                    f"🟢 Easy **{by_diff['Easy']}**   "
                    f"🟡 Medium **{by_diff['Medium']}**   "
                    f"🔴 Hard **{by_diff['Hard']}**"
                ),
                inline=False,
            )
            embed.set_footer(text="Keep the run going. 我带你.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(
        name="preview",
        description="(Test) Preview the LeetCode embed UI — daily card + reveal",
    )
    @discord.app_commands.describe(
        query="Problem number, slug, or 'daily' (default: today's daily)",
        show="Which view to preview",
    )
    @discord.app_commands.choices(
        show=[
            discord.app_commands.Choice(name="Both (card + breakdown)", value="both"),
            discord.app_commands.Choice(name="Daily card only", value="card"),
            discord.app_commands.Choice(name="Reveal breakdown only", value="reveal"),
        ]
    )
    async def testleetcode_cmd(
        interaction: discord.Interaction,
        query: str = "daily",
        show: discord.app_commands.Choice[str] = None,
    ):
        """Preview the new LeetCode embed UI on demand — no waiting for the daily
        post. Everything is ephemeral (only you see it). The daily card's Reveal
        button is LIVE, so you can test the full click-through too."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        mode = show.value if show else "both"

        q = (query or "daily").strip().lower()
        if q in ("daily", "today", ""):
            problem = await asyncio.to_thread(leetcode_api.get_daily)
        else:
            key = q if (q.isdigit() or "-" in q) else q.replace(" ", "-")
            problem = await asyncio.to_thread(leetcode_api.get_problem, key)

        if not problem:
            await interaction.followup.send(
                f"Couldn't fetch a problem for `{query}`. Try `daily`, a number "
                "(`1`), or a slug (`two-sum`).",
                ephemeral=True,
            )
            return
        if problem.get("paid_only"):
            await interaction.followup.send(
                f"**{problem['title']}** is Premium-only — no full content to preview.",
                ephemeral=True,
            )
            return

        # 1) the spoiler-safe daily card, with a LIVE Reveal button + LeetCode link
        if mode in ("both", "card"):
            card = build_daily_embed(problem, daily=True)
            view = RevealView(problem["slug"])
            if problem.get("url"):
                view.add_item(
                    discord.ui.Button(
                        label="Open on LeetCode",
                        style=discord.ButtonStyle.link,
                        url=problem["url"],
                    )
                )
            await interaction.followup.send(
                content="🧪 **Daily card preview** (Reveal button is live):",
                embed=card, view=view, ephemeral=True,
            )

        # 2) the reveal breakdown, rendered directly (skips the button click):
        #    the ELI5 teaching embed, then the code opened on the optimal approach
        #    with the approach-switch buttons live.
        if mode in ("both", "reveal"):
            ex = await asyncio.to_thread(leetcode_ai.build_explanation, problem)
            if not ex:
                await interaction.followup.send(
                    "🧪 Breakdown preview: explainer returned nothing (transient — "
                    "run it again).",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    content="🧪 **Reveal breakdown preview:**",
                    embed=build_breakdown_embed(problem, ex), ephemeral=True,
                )
                approaches = ex.get("approaches") or []
                if approaches:
                    oi = next((i for i, a in enumerate(approaches) if a.get("is_optimal")), 0)
                    await interaction.followup.send(
                        embeds=_code_embeds(problem, approaches[oi], oi, len(approaches)),
                        view=ApproachView(problem, ex), ephemeral=True,
                    )

    # ---------------- /random ----------------
    _DIFF_CHOICES = [
        discord.app_commands.Choice(name="Easy", value="easy"),
        discord.app_commands.Choice(name="Medium", value="medium"),
        discord.app_commands.Choice(name="Hard", value="hard"),
    ]

    @group.command(
        name="random", description="Grab a random LeetCode problem to grind"
    )
    @discord.app_commands.describe(
        difficulty="Filter by difficulty (optional)",
        topic="Filter by topic slug, e.g. two-pointers, dynamic-programming (optional)",
    )
    @discord.app_commands.choices(difficulty=_DIFF_CHOICES)
    async def random_cmd(
        interaction: discord.Interaction,
        difficulty: discord.app_commands.Choice[str] = None,
        topic: str = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        diff = difficulty.value if difficulty else None
        tag = topic.strip().lower().replace(" ", "-") if topic else None
        problem = await asyncio.to_thread(leetcode_api.random_problem, diff, tag)
        if not problem:
            await interaction.followup.send(
                "Couldn't roll a problem with those filters. Try loosening them.",
                ephemeral=True,
            )
            return
        card = build_daily_embed(problem, daily=False)
        view = RevealView(problem["slug"])
        if problem.get("url"):
            view.add_item(discord.ui.Button(
                label="Open on LeetCode", style=discord.ButtonStyle.link,
                url=problem["url"],
            ))
        await interaction.followup.send(
            content="🎲 Rolled you a problem:", embed=card, view=view, ephemeral=True
        )

    # ---------------- /company ----------------
    @group.command(
        name="company",
        description="Top LeetCode problems a company is known to ask",
    )
    @discord.app_commands.describe(name="Company name, e.g. Google, Amazon, Bloomberg")
    async def company_cmd(interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        slugs = await asyncio.to_thread(leetcode_api.problems_for_company, name, 15)
        if not slugs:
            await interaction.followup.send(
                f"No problem data for **{name}**. Try a big-tech name (Google, "
                "Amazon, Meta, Microsoft, Bloomberg…).",
                ephemeral=True,
            )
            return
        # Build rows straight from the (frequency-ranked) slugs — no per-problem
        # HTTP fetch. Title is derived from the slug and each links to LeetCode; the
        # ranking is the signal here, and 15 serial /problem fetches would just add
        # seconds of latency for a difficulty dot we don't need.
        rows = [
            {"title": s.replace("-", " ").title(), "slug": s}
            for s in slugs
        ]
        embed = discord.Embed(
            title=f"🏢 {name.title()} · Top Asked Problems",
            description=_problem_list_lines(rows, limit=15),
            color=discord.Color.blurple(),
        )
        embed.set_author(name="LeetCode · Company Prep", icon_url=_SW_ICON)
        embed.set_footer(text="🐺 Frequency-ranked from interview reports. Grind the top ones first.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /pattern ----------------
    @group.command(
        name="pattern",
        description="Silver Wolf explains a LeetCode technique + example problems",
    )
    @discord.app_commands.describe(
        name="Technique, e.g. two pointers, sliding window, dynamic programming"
    )
    async def pattern_cmd(interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        lesson = await asyncio.to_thread(leetcode_ai.explain_pattern, name)
        if not lesson:
            await interaction.followup.send(
                "Couldn't put together that lesson right now. Try again in a bit.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=f"🧩 {lesson.get('name', name.title())}",
            color=discord.Color.purple(),
        )
        embed.set_author(name="Silver Wolf · Pattern School", icon_url=_SW_ICON)
        if lesson.get("intro"):
            embed.description = lesson["intro"][:600]
        if lesson.get("what"):
            embed.add_field(name="🎯 What it is", value=lesson["what"][:1024], inline=False)
            _spacer(embed)
        if lesson.get("when"):
            embed.add_field(name="🔑 When to reach for it", value=lesson["when"][:1024], inline=False)
            _spacer(embed)
        if lesson.get("how"):
            embed.add_field(name="🪜 How it works", value=lesson["how"][:1024], inline=False)
        # example problems (slugs → linked). Pull tag matches from the API.
        tag = name.strip().lower().replace(" ", "-")
        rows, _ = await asyncio.to_thread(leetcode_api.problems_by_tag, tag, 6)
        if rows:
            _spacer(embed)
            embed.add_field(
                name="🎮 Practice problems",
                value=_problem_list_lines(rows, limit=6),
                inline=False,
            )
        if lesson.get("outro"):
            embed.set_footer(text="🐺 " + lesson["outro"][:200])
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /leaderboard ----------------
    _LB_CHOICES = [
        discord.app_commands.Choice(name="All-time", value="all"),
        discord.app_commands.Choice(name="This week", value="week"),
        discord.app_commands.Choice(name="Today", value="day"),
    ]

    @group.command(
        name="leaderboard", description="LeetCode solve rankings for the server"
    )
    @discord.app_commands.describe(range="Time range")
    @discord.app_commands.choices(range=_LB_CHOICES)
    async def leaderboard_cmd(
        interaction: discord.Interaction,
        range: discord.app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from datetime import datetime, timezone, timedelta

        rng = range.value if range else "all"
        since = None
        label = "All-time"
        now = datetime.now(timezone.utc)
        if rng == "week":
            since = (now - timedelta(days=7)).isoformat()
            label = "This week"
        elif rng == "day":
            since = now.date().isoformat()
            label = "Today"

        db = get_db()
        board = await asyncio.to_thread(db.get_leetcode_leaderboard, since, 15)
        if not board:
            await interaction.followup.send(
                f"No solves logged {label.lower()} yet. React ✅ on a problem to "
                "get on the board.",
                ephemeral=True,
            )
            return
        # Resolve all names in ONE batched query (not N serial round-trips).
        names = await asyncio.to_thread(
            db.get_discord_names_for_users, [r["user_id"] for r in board]
        )
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(board):
            name = names.get(row["user_id"]) or "a grinder"
            mark = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{mark} **{name}** — {row['solves']} solved")
        embed = discord.Embed(
            title=f"🏆 LeetCode Leaderboard · {label}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="🐺 React ✅ when you clear one to climb the board.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /hint ----------------
    @group.command(
        name="hint",
        description="Get progressive hints for a problem — no spoilers, no solution",
    )
    @discord.app_commands.describe(query="Problem number, slug, or 'daily'")
    async def hint_cmd(interaction: discord.Interaction, query: str = "daily"):
        await interaction.response.defer(ephemeral=True, thinking=True)
        q = (query or "daily").strip().lower()
        if q in ("daily", "today", ""):
            problem = await asyncio.to_thread(leetcode_api.get_daily)
        else:
            key = q if (q.isdigit() or "-" in q) else q.replace(" ", "-")
            problem = await asyncio.to_thread(leetcode_api.get_problem, key)
        if not problem:
            await interaction.followup.send(
                f"Couldn't find `{query}`. Try a number or slug.", ephemeral=True
            )
            return
        ladder = await asyncio.to_thread(leetcode_ai.hint_ladder, problem)
        if not ladder or not ladder.get("hints"):
            await interaction.followup.send(
                "Couldn't build hints right now. Try again in a bit.", ephemeral=True
            )
            return
        diff = problem.get("difficulty") or ""
        embed = discord.Embed(
            title=f"💡 Hints · {problem.get('title')}",
            url=problem.get("url") or None,
            color=_DIFF_COLOR.get(diff, discord.Color.blurple()),
        )
        embed.set_author(name="Silver Wolf · Hint Ladder", icon_url=_SW_ICON)
        embed.description = "_Each hint is hidden — reveal them one at a time, only as far as you need._"
        labels = ["🟢 Nudge", "🟡 The pattern", "🔴 First step"]
        for i, h in enumerate(ladder["hints"][:3]):
            embed.add_field(
                name=labels[i] if i < len(labels) else f"Hint {i + 1}",
                value=f"||{h[:1000]}||",
                inline=False,
            )
            _spacer(embed)
        embed.set_footer(text="🐺 Try it before peeking further. That's how the pattern sticks.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /explaincode ----------------
    @group.command(
        name="explaincode",
        description="Paste YOUR code — Silver Wolf explains why it's slow/buggy + the fix",
    )
    @discord.app_commands.describe(
        code="Your solution code",
        problem="(Optional) problem number/slug for context",
    )
    async def explaincode_cmd(
        interaction: discord.Interaction, code: str, problem: str = None
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        prob = None
        if problem:
            key = problem.strip().lower()
            key = key if (key.isdigit() or "-" in key) else key.replace(" ", "-")
            prob = await asyncio.to_thread(leetcode_api.get_problem, key)
        review = await asyncio.to_thread(leetcode_ai.explain_user_code, code, prob)
        if not review:
            await interaction.followup.send(
                "Couldn't analyze that right now. Try again in a bit.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="🔎 Code Review",
            color=discord.Color.blurple(),
        )
        embed.set_author(name="Silver Wolf · Debugger", icon_url=_SW_ICON)
        if review.get("verdict"):
            embed.description = f"**{review['verdict'][:500]}**"
        if review.get("what_it_does"):
            embed.add_field(name="📖 What your code does", value=review["what_it_does"][:1024], inline=False)
            _spacer(embed)
        if review.get("complexity"):
            embed.add_field(name="⏱️ Complexity", value=review["complexity"][:1024], inline=False)
        issues = review.get("issues") or []
        if issues:
            _spacer(embed)
            embed.add_field(
                name="🐛 Issues",
                value="\n".join(f"• {x}" for x in issues)[:1024],
                inline=False,
            )
        if review.get("fix"):
            _spacer(embed)
            embed.add_field(name="🛠️ The fix", value=review["fix"][:1024], inline=False)
        if review.get("encouragement"):
            embed.set_footer(text="🐺 " + review["encouragement"][:200])
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /weakspots ----------------
    @group.command(
        name="weakspots",
        description="See which LeetCode patterns you keep struggling with",
    )
    async def weakspots_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = get_db()
        user = interaction.user
        uid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        solves = await asyncio.to_thread(db.get_leetcode_solves, uid) if uid else []
        # Aggregate struggle/fail counts by topic tag.
        from collections import Counter
        struggled_by_topic = Counter()
        total_by_topic = Counter()
        for s in solves:
            topics = (s.get("topics") or "").split(",")
            for t in topics:
                t = t.strip()
                if not t:
                    continue
                total_by_topic[t] += 1
                if s.get("status") in ("struggled", "failed"):
                    struggled_by_topic[t] += 1
        if not total_by_topic:
            await interaction.followup.send(
                "No attempt history yet. Log some problems (react ✅, or mark ones "
                "you struggled with) and I'll map your weak spots.",
                ephemeral=True,
            )
            return
        # Rank by struggle RATE (min 2 attempts to be meaningful).
        ranked = []
        for t, tot in total_by_topic.items():
            if tot < 2:
                continue
            rate = struggled_by_topic[t] / tot
            if rate > 0:
                ranked.append((t, struggled_by_topic[t], tot, rate))
        ranked.sort(key=lambda x: -x[3])
        embed = discord.Embed(
            title=f"🎯 {user.display_name}'s Weak Spots",
            color=discord.Color.orange(),
        )
        embed.set_author(name="Silver Wolf · Scouting Report", icon_url=_SW_ICON)
        if not ranked:
            embed.description = "No clear weak pattern yet — you're clearing what you touch. Go pick a harder fight."
        else:
            lines = []
            for t, bad, tot, rate in ranked[:8]:
                bar = "🔴" if rate >= 0.6 else "🟠" if rate >= 0.3 else "🟡"
                lines.append(f"{bar} **{t}** — struggled {bad}/{tot} ({rate * 100:.0f}%)")
            embed.description = "\n".join(lines)
            embed.add_field(
                name="🐺 Silver Wolf says",
                value=f"Grind `{ranked[0][0]}` next — that's the hole in your build. "
                "Try `/leetcode pattern " + ranked[0][0].lower() + "`.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /review (spaced repetition) ----------------
    @group.command(
        name="review",
        description="Problems due for a spaced-repetition review — beat forgetting",
    )
    async def review_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from datetime import datetime, timezone

        db = get_db()
        user = interaction.user
        uid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        now = datetime.now(timezone.utc).isoformat()
        due = await asyncio.to_thread(db.get_leetcode_due_reviews, uid, now) if uid else []
        if not due:
            await interaction.followup.send(
                "Nothing due for review — your memory's holding. Solve more problems "
                "(especially ones you struggled with) and I'll resurface them later.",
                ephemeral=True,
            )
            return
        rows = [
            {
                "title": s["problem_slug"].replace("-", " ").title(),
                "slug": s["problem_slug"],
                "difficulty": s.get("difficulty"),
            }
            for s in due
        ]
        embed = discord.Embed(
            title="🔁 Due for Review",
            description=_problem_list_lines(rows, limit=15),
            color=discord.Color.teal(),
        )
        embed.set_author(name="Silver Wolf · Spaced Repetition", icon_url=_SW_ICON)
        embed.set_footer(
            text="🐺 Re-solve these to lock them in. Forgetting is the real boss."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /mock (timed OA simulation) ----------------
    @group.command(
        name="mock",
        description="Timed OA simulation — N problems, a countdown, real pressure",
    )
    @discord.app_commands.describe(
        minutes="Time limit (default 60)",
        count="How many problems (default 2)",
        difficulty="Difficulty (default medium)",
    )
    @discord.app_commands.choices(difficulty=_DIFF_CHOICES)
    async def mock_cmd(
        interaction: discord.Interaction,
        minutes: int = 60,
        count: int = 2,
        difficulty: discord.app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        minutes = max(5, min(180, minutes))
        count = max(1, min(4, count))
        diff = difficulty.value if difficulty else "medium"

        problems = []
        seen = set()
        for _ in range(count * 3):  # oversample; dedupe
            if len(problems) >= count:
                break
            p = await asyncio.to_thread(leetcode_api.random_problem, diff)
            if p and p["slug"] not in seen:
                seen.add(p["slug"])
                problems.append(p)
        if not problems:
            await interaction.followup.send(
                "Couldn't assemble a mock set right now. Try again.", ephemeral=True
            )
            return

        from datetime import datetime, timezone, timedelta

        # deadline as a Discord relative timestamp (client renders a live countdown)
        end = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        end_ts = int(end.timestamp())

        embed = discord.Embed(
            title=f"⏱️ Mock OA · {len(problems)} problem(s) · {minutes} min",
            description=(
                f"**Clock's running.** Ends <t:{end_ts}:R> (at <t:{end_ts}:t>).\n"
                "No peeking at solutions — treat it like the real assessment."
            ),
            color=discord.Color.red(),
        )
        embed.set_author(name="Silver Wolf · Timed Run", icon_url=_SW_ICON)
        for i, p in enumerate(problems, 1):
            dm = _DIFF_EMOJI.get(p.get("difficulty"), "⚪")
            embed.add_field(
                name=f"{dm} Problem {i}",
                value=f"[{p['title']}]({p['url']})  ·  `{p.get('difficulty','')}`",
                inline=False,
            )
        embed.set_footer(text="🐺 Simulate it for real: no IDE autocomplete, no Google. Go.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- /learn (DS&A pattern lesson + practice) ----------------
    async def _learn_autocomplete(interaction: discord.Interaction, current: str):
        cur = (current or "").lower()
        opts = []
        for p in leetcode_api.roadmap_patterns():
            if cur in p["name"].lower() or cur in p["key"]:
                opts.append(
                    discord.app_commands.Choice(name=p["name"], value=p["key"])
                )
            if len(opts) >= 25:
                break
        return opts

    @group.command(
        name="learn",
        description="Learn a DS&A pattern with Silver Wolf + practice problems",
    )
    @discord.app_commands.describe(
        topic="Pattern to learn, e.g. two pointers, sliding window, dp, bfs"
    )
    @discord.app_commands.autocomplete(topic=_learn_autocomplete)
    async def learn_cmd(interaction: discord.Interaction, topic: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pat = await asyncio.to_thread(leetcode_api.roadmap_pattern, topic)
        # Fall back to a freeform pattern lesson if it's not a roadmap key.
        pat_name = pat["name"] if pat else topic
        # Anchor the lesson to the student's own study-note facts when we have them.
        knowledge = leetcode_api.pattern_knowledge(pat["key"]) if pat else None
        lesson = await asyncio.to_thread(
            leetcode_ai.explain_pattern, pat_name, knowledge
        )
        if not lesson:
            await interaction.followup.send(
                "Couldn't build that lesson right now. Try again in a bit.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📚 Learn · {lesson.get('name', pat_name)}",
            color=discord.Color.purple(),
        )
        embed.set_author(name="Silver Wolf · DS&A School", icon_url=_SW_ICON)
        if lesson.get("intro"):
            embed.description = lesson["intro"][:600]
        # Shape-first order: recognition (when) leads — it's the real skill — then
        # what/how, then the named parts (state, complexity), self-check questions,
        # the reusable template, and the classic misconception.
        if lesson.get("when"):
            embed.add_field(name="🔍 Spot it (the signal)", value=lesson["when"][:1024], inline=False)
            _spacer(embed)
        if lesson.get("what"):
            embed.add_field(name="🎯 What it is", value=lesson["what"][:1024], inline=False)
        if lesson.get("how"):
            embed.add_field(name="🪜 How it works", value=lesson["how"][:1024], inline=False)
        _spacer(embed)
        if lesson.get("state"):
            embed.add_field(name="📦 State to track", value=lesson["state"][:1024], inline=False)
        if lesson.get("complexity"):
            embed.add_field(name="⏱️ Complexity", value=lesson["complexity"][:1024], inline=False)
        qs = lesson.get("questions") or []
        if qs:
            _spacer(embed)
            embed.add_field(
                name="❓ Ask yourself",
                value="\n".join(f"• {q}" for q in qs[:4])[:1024],
                inline=False,
            )
        if lesson.get("template"):
            tmpl = lesson["template"][:1000]
            embed.add_field(
                name="🧩 The template",
                value=f"```\n{tmpl}\n```"[:1024],
                inline=False,
            )
        if lesson.get("misconception"):
            embed.add_field(
                name="⚠️ Common trap",
                value=lesson["misconception"][:1024],
                inline=False,
            )

        # Practice problems: prefer the roadmap's curated examples, top up from the
        # LeetCode tag if the pattern has one.
        practice_rows = []
        if pat:
            for slug in pat.get("examples", [])[:6]:
                practice_rows.append({"title": slug.replace("-", " ").title(), "slug": slug})
        tag = (pat or {}).get("tag") or pat_name.strip().lower().replace(" ", "-")
        if len(practice_rows) < 6:
            more, _ = await asyncio.to_thread(leetcode_api.problems_by_tag, tag, 6)
            seen = {r["slug"] for r in practice_rows}
            for r in more:
                if r["slug"] not in seen:
                    practice_rows.append(r)
                if len(practice_rows) >= 8:
                    break
        if practice_rows:
            _spacer(embed)
            embed.add_field(
                name="🎮 Practice these",
                value=_problem_list_lines(practice_rows, limit=8),
                inline=False,
            )
        if lesson.get("outro"):
            embed.set_footer(text="🐺 " + lesson["outro"][:200])

        view = None
        if pat:  # only roadmap patterns are trackable
            view = _LearnView(pat["key"], pat["name"], get_db)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ---------------- /roadmap (curriculum + progress) ----------------
    @group.command(
        name="roadmap",
        description="Your DS&A learning roadmap — what to study and what's next",
    )
    async def roadmap_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = get_db()
        user = interaction.user
        uid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        learned = set(await asyncio.to_thread(db.get_learned_patterns, uid) if uid else [])
        patterns = leetcode_api.roadmap_patterns()
        cats = {c["key"]: c for c in leetcode_api.roadmap_categories()}

        embed = discord.Embed(
            title="🗺️ DS&A Roadmap",
            description=(
                f"**{len(learned)}/{len(patterns)}** patterns learned. "
                "✅ = done, ⬜ = to go. Use `/leetcode learn <pattern>` to study one."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_author(name="Silver Wolf · Skill Tree", icon_url=_SW_ICON)

        # Group by category, in category declared order.
        by_cat = {}
        for p in patterns:
            by_cat.setdefault(p["category"], []).append(p)
        for ckey, c in cats.items():
            group = by_cat.get(ckey)
            if not group:
                continue
            lines = []
            for p in group:
                mark = "✅" if p["key"] in learned else "⬜"
                lines.append(f"{mark} {p['name']}")
            embed.add_field(
                name=f"{c.get('emoji','')} {c['name']}",
                value="\n".join(lines)[:1024],
                inline=True,
            )

        nxt = leetcode_api.roadmap_next(learned)
        if nxt:
            embed.add_field(
                name="🐺 Study next",
                value=f"**{nxt['name']}** — `/leetcode learn {nxt['key'].replace('_',' ')}`",
                inline=False,
            )
        else:
            embed.add_field(
                name="🐺 100% cleared",
                value="You've learned every pattern on the roadmap. 我独自满级. Now go grind them.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    bot.tree.add_command(group)
    log.info(
        "Registered /leetcode group (problem, random, company, pattern, learn, "
        "roadmap, hint, mock, explaincode, streak, leaderboard, weakspots, review, preview)"
    )


def _consecutive_day_streak(solves):
    """Count consecutive days ending today/yesterday with at least one solve.
    solves: rows with 'solved_at' ISO timestamps, newest first."""
    from datetime import datetime, timezone, timedelta

    days = set()
    for s in solves:
        ts = s.get("solved_at")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
                timezone.utc
            ).date()
            days.add(d)
        except Exception:
            continue
    if not days:
        return 0
    today = datetime.now(timezone.utc).date()
    # Streak counts only if the most recent solve was today or yesterday.
    if today not in days and (today - timedelta(days=1)) not in days:
        return 0
    streak = 0
    cur = today if today in days else today - timedelta(days=1)
    while cur in days:
        streak += 1
        cur -= timedelta(days=1)
    return streak
