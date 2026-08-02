"""AI slash commands backed by Gemma: /reviewresume, /match, /recommend,
/interview, /helpme.

The resume commands read the user's rendered resume PNG (vision); the rest are
text prompts. Every response is shown as an ephemeral embed. Long answers are
split across multiple embed fields (1024-char cap each)."""

import asyncio
import logging

import discord

from commands import gemma_client, lang_view, persona, resume_utils

log = logging.getLogger("cs_internship_bot")

FIELD_LIMIT = 1024
MAX_FIELDS = 6  # keep embeds readable; overflow is truncated with a note

# Reply in whatever language the user wrote in — Silver Wolf speaks both. Appended
# to the free-text AI prompts so a Chinese question gets a natural 中文 answer.
_LANG_MIRROR = (
    "\n\nLANGUAGE: reply in the SAME language the user wrote in. If their input is "
    "in Chinese, answer in Silver Wolf's natural Simplified-Chinese voice (痞帅、"
    "慵懒、游戏黑客俚语，自然不堆梗); otherwise answer in English. Never mix the two."
)


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
    embed.set_footer(text="🐺 Silver Wolf • don't trust the RNG blind, double-check")
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
    persona.SILVER_WOLF_SYSTEM
    + "\n\n<this_task>\n"
    "You're scanning a CS student's résumé (their character build) and reporting "
    "back — like a hiring manager with 20 years reading résumés who also tunes the "
    "ATS that screen them, except you're Silver Wolf doing it. Base EVERYTHING on "
    "what the résumé actually shows; invent nothing. Write the text fields in your "
    "voice — sharp, a little smug, genuinely on their side — but the advice must be "
    "concrete and real, and the flavor stays light (a beat or two, natural, not a "
    "caricature). Every field is honest, useful, and specific.\n</this_task>\n\n"
    "Write every field TWICE — English + native Simplified Chinese (银狼中文语气："
    "痞帅慵懒，游戏词汇用中文，别硬堆梗；只有真正的技术名词保留英文如 Python/AWS/ATS). "
    "The _zh is Silver Wolf actually speaking Chinese, same energy, not a literal "
    "translation. The _en and _zh arrays must have the SAME number of items in the "
    "same order.\n"
    "Return ONLY this JSON, flat string arrays only:\n"
    "{\n"
    '  "impression_en": "<2-sentence overall read, Silver Wolf voice>",\n'
    '  "impression_zh": "<中文，2句，银狼语气>",\n'
    '  "strengths_en": ["<top strength, specific>"],\n'
    '  "strengths_zh": ["<中文>"],\n'
    '  "improvements_en": ["<concrete fix: wording, formatting, missing content, quantified impact>"],\n'
    '  "improvements_zh": ["<中文>"],\n'
    '  "ats_gaps_en": ["<ATS/keyword gap for software-tech roles>"],\n'
    '  "ats_gaps_zh": ["<中文>"]\n'
    "}\n"
    "Caps: strengths<=3, improvements<=5, ats_gaps<=4 (each language). Keep items "
    "short. Never invent skills, tools, or numbers the résumé doesn't show."
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


_SW_PURPLE = discord.Color.from_rgb(167, 139, 250)  # Silver Wolf violet

_REVIEW_LABELS = {
    "en": {
        "title": "📝 Résumé Review", "strengths": "✅ Strengths",
        "improvements": "🔧 Improvements", "ats_gaps": "🤖 ATS / keyword gaps",
        "footer": "🐺 Silver Wolf • don't trust the RNG blind, double-check",
    },
    "zh": {
        "title": "📝 简历点评", "strengths": "✅ 强项",
        "improvements": "🔧 待改进", "ats_gaps": "🤖 ATS / 关键词短板",
        "footer": "🐺 银狼 • 别全信 RNG，自己再核对一遍",
    },
}


def _pick_review(data, base, lang):
    """A review field with EN/中文 fallback (base_<lang> → base_en → base)."""
    for key in (f"{base}_{lang}", f"{base}_en", base):
        v = data.get(key)
        if v:
            return v
    return None


def _review_embed(data, lang="en"):
    lab = _REVIEW_LABELS.get(lang, _REVIEW_LABELS["en"])
    embed = discord.Embed(title=lab["title"], color=_SW_PURPLE)
    imp = str(_pick_review(data, "impression", lang) or "").strip()
    if imp:
        embed.description = imp[:4096]
    for base in ("strengths", "improvements", "ats_gaps"):
        items = _pick_review(data, base, lang)
        if isinstance(items, list) and items:
            block = "\n".join(
                f"• {str(x).strip()}" for x in items[:5] if str(x).strip()
            )
            if block:
                embed.add_field(name=lab[base], value=block[:1024], inline=False)
    embed.set_footer(text=lab["footer"])
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
        view = lang_view.LangToggleView(lambda lang: _review_embed(data, lang), lang="en")
        await interaction.followup.send(embed=_review_embed(data, "en"), view=view, ephemeral=True)

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
        # Fast-fail with a friendly message if there's no résumé; run_score below
        # re-checks and does the actual scoring (this just saves a round-trip).
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

        # Reuse the tuned Score feature as the single source of truth: the same
        # calibrated subscore-first weighted-average rubric, Silver Wolf voice,
        # score wheel, Level-Up Plan, and EN/中文 toggle the Score button gives.
        # Imported lazily — job_ai imports from this module, so a top-level import
        # would be circular.
        from commands import job_ai

        job = rows[0]
        embed, file, view, error = await job_ai.run_score(
            db, interaction.user, table, job["id"]
        )
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        kwargs = {"ephemeral": True}
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file
        if view is not None:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)

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
            persona.SILVER_WOLF_SYSTEM
            + "\n\n<this_task>\n"
            "The image is a CS student's résumé — their build. You scanned it, and "
            "now you're pointing them at the runs actually worth queuing. From the "
            "numbered list of open internships below, pick the 5 BEST fits for THIS "
            "candidate. For each: the number, the role, and one line on WHY it fits "
            "their real background (tie it to something actually on the résumé — a "
            "skill, project, or focus they have; don't invent). Rank best first. "
            "Your Silver Wolf voice colors the wording lightly (a beat or two, "
            "natural); the picks + reasons stay honest and genuinely useful.\n"
            "</this_task>"
            + _LANG_MIRROR
            + "\n\nOPEN INTERNSHIPS:\n" + menu
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
            persona.SILVER_WOLF_SYSTEM
            + "\n\n<this_task>\n"
            f"You're running interview drills for a CS student prepping for: {role}. "
            "The interview is the boss fight; you're the friend who's cleared it and "
            "is training them."
            + _LANG_MIRROR
            + "\n\nGenerate a REALISTIC, role-appropriate practice set:\n"
            "- 3 behavioral questions\n- 4 technical questions fitting THIS role\n"
            "- 1 'why this company/role' question\n"
            "For each, add a one-line hint on what a strong answer covers. The "
            "questions and hints stay genuine and substantive — a real interviewer's "
            "questions, not a joke set; your Silver Wolf voice lives in the short "
            "framing/intro line only, and stays light (a beat or two, natural, not a "
            "caricature). Keep the whole thing tight and actually useful. Invent "
            "nothing about the company you don't plausibly know.\n</this_task>"
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
            persona.SILVER_WOLF_SYSTEM
            + "\n\n<this_task>\n"
            "A CS student is asking you for career help — internships, résumés, "
            "new-grad roles, the whole grind. Answer as Silver Wolf: the genius "
            "hacker-friend who's already mapped this system and is handing them the "
            "shortcut. Real, specific, practical advice FIRST — concrete steps, not "
            "platitudes; the personality colors HOW you say it, never replaces the "
            "substance. Stay truthful — no made-up facts or fake guarantees. Natural "
            "voice, not a caricature: a beat or two of flavor, the rest clear and "
            "human. LENGTH: keep the whole answer under ~1500 characters — a tight, "
            "scannable reply (short paragraphs or a few bullets), not a wall of "
            "text; lead with the most useful thing.\n</this_task>"
            + _LANG_MIRROR
            + f"\n\n<question>\n{question}\n</question>"
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
