"""AI slash commands backed by Gemma: /reviewresume, /match, /recommend,
/interview, /helpme.

The resume commands read the user's rendered resume PNG (vision); the rest are
text prompts. Every response is shown as an ephemeral embed. Long answers are
split across multiple embed fields (1024-char cap each)."""

import asyncio
import logging

import discord

from commands import gemma_client, resume_utils

log = logging.getLogger("cs_internship_bot")

FIELD_LIMIT = 1024
MAX_FIELDS = 6  # keep embeds readable; overflow is truncated with a note


def _chunk(text, size=FIELD_LIMIT):
    """Split text into <=size pieces, preferring paragraph/line breaks."""
    text = (text or "").strip()
    chunks = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks


def _answer_embed(title, body, color=discord.Color.blurple()):
    """An embed carrying a (possibly long) AI answer across fields."""
    embed = discord.Embed(title=title, color=color)
    chunks = _chunk(body)
    for i, chunk in enumerate(chunks[:MAX_FIELDS]):
        embed.add_field(
            name="​" if i else "Response", value=chunk, inline=False
        )
    if len(chunks) > MAX_FIELDS:
        embed.add_field(name="​", value="*…truncated*", inline=False)
    embed.set_footer(text="AI-generated • verify before relying on it")
    return embed


def _job_context(row):
    """Compact text description of a posting for the AI prompt."""
    parts = [
        f"Title: {row.get('job_title')}",
        f"Company: {row.get('company_name')}",
        f"Location: {row.get('job_location')}",
    ]
    if row.get("job_responsibilities"):
        parts.append("Responsibilities:\n- " + "\n- ".join(row["job_responsibilities"][:10]))
    if row.get("job_requirements"):
        parts.append("Requirements:\n- " + "\n- ".join(row["job_requirements"][:10]))
    if row.get("job_tags"):
        parts.append("Skills: " + ", ".join(row["job_tags"][:15]))
    return "\n".join(p for p in parts if p)


# --- Resume review (precomputed at upload; JSON so it renders as fields) ----

_REVIEW_PROMPT = (
    "You are a hiring manager with 20 years of experience in tech who also "
    "tunes the ATS that screen resumes. Review this candidate's resume. Base "
    "everything on what the resume actually shows; invent nothing.\n\n"
    "Return ONLY this JSON, flat string arrays only:\n"
    "{\n"
    '  "impression": "<2-sentence overall impression>",\n'
    '  "strengths": ["<top strength>"],\n'
    '  "improvements": ["<concrete fix: wording, formatting, missing content, quantified impact>"],\n'
    '  "ats_gaps": ["<ATS/keyword gap for software-tech roles>"]\n'
    "}\n"
    "Caps: strengths<=3, improvements<=5, ats_gaps<=4. Keep items short."
)


def compute_review(text, img):
    """Compute the resume-review JSON from resume text (preferred) or image.
    Returns dict or None. Safe to call off-thread."""
    if text:
        prompt = f"{_REVIEW_PROMPT}\n\n<resume>\n{text}\n</resume>"
        return gemma_client.ask_json_text(prompt, 3000)
    if img:
        return gemma_client.ask_json_with_image(img, _REVIEW_PROMPT, 3000)
    return None


def _review_embed(data):
    embed = discord.Embed(title="📝 Resume Review", color=discord.Color.green())
    imp = str(data.get("impression") or "").strip()
    if imp:
        embed.description = imp[:4096]
    for name, key in (
        ("✅ Strengths", "strengths"),
        ("🔧 Improvements", "improvements"),
        ("🤖 ATS / keyword gaps", "ats_gaps"),
    ):
        items = data.get(key)
        if isinstance(items, list) and items:
            block = "\n".join(
                f"• {str(x).strip()}" for x in items[:5] if str(x).strip()
            )
            if block:
                embed.add_field(name=name, value=block[:1024], inline=False)
    embed.set_footer(text="AI-generated • verify before relying on it")
    return embed


def register(bot, *, get_db, logger=None):
    lg = logger or log

    async def _resume_image(interaction):
        """Fetch the caller's resume PNG, or None (and tell them to upload)."""
        db = get_db()
        user = interaction.user
        uid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        if not uid:
            return None, None
        img = await asyncio.to_thread(resume_utils.image_bytes, db, uid)
        return uid, img

    async def _need_resume_msg(interaction):
        await interaction.followup.send(
            "You need a resume first — upload one with `/resume upload`.",
            ephemeral=True,
        )

    # ---- /reviewresume ---------------------------------------------------
    @bot.tree.command(
        name="reviewresume",
        description="Get AI feedback and improvement suggestions for your resume",
    )
    async def reviewresume(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = get_db()
        user = interaction.user
        uid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        if not uid:
            await _need_resume_msg(interaction)
            return

        # Precomputed at upload → instant.
        data = await asyncio.to_thread(resume_utils.get_review, db, uid)
        if data is None:
            # Not primed yet: compute from stored text (fast) or the image.
            text = await asyncio.to_thread(resume_utils.get_resume_text, db, uid)
            img = None
            if not text:
                img = await asyncio.to_thread(resume_utils.image_bytes, db, uid)
                if not img:
                    await _need_resume_msg(interaction)
                    return
            data = await asyncio.to_thread(compute_review, text, img)
            if data:
                await asyncio.to_thread(resume_utils.store_review, db, uid, data)

        if not data:
            await interaction.followup.send(
                "The AI couldn't review your resume right now — try again later.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=_review_embed(data), ephemeral=True)

    # ---- /match ----------------------------------------------------------
    @bot.tree.command(
        name="match",
        description="Compare your resume against an internship and get a match score",
    )
    @discord.app_commands.describe(
        query="Company or role to match against",
        type="Internships or new-grad roles",
    )
    @discord.app_commands.choices(
        type=[
            discord.app_commands.Choice(name="Internships", value="internships"),
            discord.app_commands.Choice(name="New Grad", value="new_grads"),
        ]
    )
    async def match(
        interaction: discord.Interaction,
        query: str,
        type: discord.app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        _uid, img = await _resume_image(interaction)
        if not img:
            await _need_resume_msg(interaction)
            return

        table = type.value if type else "internships"
        db = get_db()
        like = f"%{query}%"
        try:
            rows = (
                db.supabase.table(table)
                .select("*, company_info(*)")
                .eq("is_closed", False)
                .not_.is_("job_summary", "null")
                .or_(f"company_name.ilike.{like},job_title.ilike.{like}")
                .limit(1)
                .execute()
                .data
            )
        except Exception:
            lg.exception("match query failed")
            rows = None
        if not rows:
            await interaction.followup.send(
                f"No open posting found matching **{discord.utils.escape_markdown(query)}**.",
                ephemeral=True,
            )
            return

        job = rows[0]
        prompt = (
            "You are an ATS and technical recruiter. The image is a candidate's "
            "resume. Compare it against this job posting and respond with:\n"
            "1. MATCH SCORE: a single 0-100 number with a one-line justification.\n"
            "2. STRONG MATCHES: skills/experience that align (bullets).\n"
            "3. GAPS: top requirements the resume is missing or weak on (bullets).\n"
            "4. QUICK WINS: 2-3 resume tweaks to improve the match, truthfully.\n\n"
            f"JOB POSTING:\n{_job_context(job)}"
        )
        answer = await asyncio.to_thread(gemma_client.ask_with_image, img, prompt)
        if not answer:
            await interaction.followup.send(
                "The AI couldn't score the match right now — try again later.",
                ephemeral=True,
            )
            return
        title = f"🎯 Match — {job.get('job_title')} @ {job.get('company_name')}"
        await interaction.followup.send(
            embed=_answer_embed(title[:256], answer, discord.Color.gold()),
            ephemeral=True,
        )

    # ---- /recommend ------------------------------------------------------
    @bot.tree.command(
        name="recommend",
        description="Get personalized internship recommendations based on your resume",
    )
    async def recommend(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        _uid, img = await _resume_image(interaction)
        if not img:
            await _need_resume_msg(interaction)
            return

        # Give the model a menu of current open roles to pick from.
        db = get_db()
        try:
            jobs = (
                db.supabase.table("internships")
                .select("company_name,job_title,job_location,job_tags")
                .eq("is_closed", False)
                .not_.is_("job_summary", "null")
                .order("job_posted_at", desc=True)
                .limit(40)
                .execute()
                .data
                or []
            )
        except Exception:
            lg.exception("recommend query failed")
            jobs = []
        if not jobs:
            await interaction.followup.send(
                "No open internships to recommend right now.", ephemeral=True
            )
            return

        menu = "\n".join(
            f"{i+1}. {j['job_title']} @ {j['company_name']} "
            f"({j.get('job_location', '?')}) — {', '.join((j.get('job_tags') or [])[:5])}"
            for i, j in enumerate(jobs)
        )
        prompt = (
            "The image is a candidate's resume. From the numbered list of open "
            "internships below, pick the 5 best fits. For each, give the number, "
            "the role, and one line on WHY it fits their background. Rank best "
            "first.\n\nOPEN INTERNSHIPS:\n" + menu
        )
        answer = await asyncio.to_thread(gemma_client.ask_with_image, img, prompt)
        if not answer:
            await interaction.followup.send(
                "The AI couldn't build recommendations right now — try again later.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_answer_embed(
                "✨ Recommended for You", answer, discord.Color.purple()
            ),
            ephemeral=True,
        )

    # ---- /interview ------------------------------------------------------
    @bot.tree.command(
        name="interview",
        description="Practice interview questions tailored to a company or role",
    )
    @discord.app_commands.describe(
        role="Role or company to prep for (e.g. 'SWE intern at Stripe')"
    )
    async def interview(interaction: discord.Interaction, role: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        prompt = (
            f"Act as an interviewer for: {role}. Generate a realistic practice "
            "set:\n- 3 behavioral questions.\n- 4 technical questions appropriate "
            "to the role.\n- 1 'why this company/role' question.\nFor each, add a "
            "one-line hint on what a strong answer covers. Keep it tight."
        )
        answer = await asyncio.to_thread(gemma_client.ask_text, prompt)
        if not answer:
            await interaction.followup.send(
                "The AI couldn't generate questions right now — try again later.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_answer_embed(
                f"🎤 Interview Prep — {role}"[:256], answer, discord.Color.teal()
            ),
            ephemeral=True,
        )

    # ---- /helpme ---------------------------------------------------------
    @bot.tree.command(
        name="helpme",
        description="Ask the AI career assistant about internships, resumes, or careers",
    )
    @discord.app_commands.describe(question="What do you want to ask?")
    async def helpme(interaction: discord.Interaction, question: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        prompt = (
            "You are a helpful, practical CS-career assistant for students hunting "
            "internships and new-grad roles. Answer concisely and specifically.\n\n"
            f"QUESTION: {question}"
        )
        answer = await asyncio.to_thread(gemma_client.ask_text, prompt)
        if not answer:
            await interaction.followup.send(
                "The AI assistant is unavailable right now — try again later.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=_answer_embed("💬 Career Assistant", answer),
            ephemeral=True,
        )
