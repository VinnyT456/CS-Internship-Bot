"""Shared AI actions for the Score / Tailor buttons on a posting.

Both the posted-message buttons (main.py's jobsec:* handler) and the /latest
browser call these. Each fetches the clicking user's resume image + the posting
row, runs Gemma, and returns a ready-to-send ephemeral embed — or a "need a
resume" prompt when the user hasn't uploaded one.
"""

import asyncio
import io
import logging
import os
import re

import discord

from commands import gemma_client, lang_view, persona, resume_utils, score_wheel
from commands.ai_commands import _answer_embed, _job_context

log = logging.getLogger("cs_internship_bot")

# Optional resume-build microservice (FastAPI + tectonic). When set, Tailor
# POSTs the YAML there and returns a compiled PDF; otherwise it returns the YAML
# file for the user to build themselves.
RESUME_BUILD_URL = os.getenv("RESUME_BUILD_URL")
# Accept either name; RESUME_BUILD_TOKEN wins, else share BUILD_TOKEN.
RESUME_BUILD_TOKEN = os.getenv("RESUME_BUILD_TOKEN") or os.getenv("BUILD_TOKEN")
RESUME_BUILD_TIMEOUT = float(os.getenv("RESUME_BUILD_TIMEOUT", "30"))


def _build_service_base():
    """The build service base URL, ensuring an https scheme (Render's
    `property: host` injects a bare hostname)."""
    url = (RESUME_BUILD_URL or "").strip().rstrip("/")
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def io_bytes(data):
    return io.BytesIO(data)


def _build_pdf_sync(yaml_text):
    """POST YAML to the build service, return PDF bytes or None. Blocking —
    call via asyncio.to_thread. Never raises."""
    base = _build_service_base()
    if not base:
        return None
    try:
        import httpx

        headers = {"Content-Type": "application/x-yaml"}
        if RESUME_BUILD_TOKEN:
            headers["Authorization"] = f"Bearer {RESUME_BUILD_TOKEN}"
        url = base + "/build"
        resp = httpx.post(
            url,
            content=yaml_text.encode("utf-8"),
            headers=headers,
            timeout=RESUME_BUILD_TIMEOUT,
        )
        if resp.status_code == 200 and resp.content:
            return resp.content
        log.warning("Resume build service returned %s: %s", resp.status_code, resp.text[:300])
    except Exception:
        log.exception("Resume build service call failed")
    return None


def _parse_score(answer):
    """Pull the 0-100 match score out of the model's reply. Looks for an
    explicit `SCORE: <n>` first, then falls back to the first standalone
    0-100 number. Returns int or None."""
    if not answer:
        return None
    m = re.search(r"SCORE\s*[:=]\s*(\d{1,3})", answer, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(\d{1,3})\s*(?:/\s*100|%|\bout of 100\b)", answer, re.I)
    if not m:
        m = re.search(r"\b(100|[1-9]?\d)\b", answer)
    if not m:
        return None
    val = int(m.group(1))
    return max(0, min(100, val))


def _fetch_row(db, table, row_id):
    try:
        data = (
            db.supabase.table(table)
            .select("*, company_info(*)")
            .eq("id", int(row_id))
            .limit(1)
            .execute()
            .data
        )
        return data[0] if data else None
    except Exception:
        log.exception("job_ai: failed fetching %s %s", table, row_id)
        return None


async def _resume_image(db, user):
    uid = await asyncio.to_thread(
        db.get_or_create_user, user.id, user.name, user.display_name
    )
    if not uid:
        return None
    return await asyncio.to_thread(resume_utils.image_bytes, db, uid)


async def _resume_source(db, uid):
    """Best resume input for a text-capable AI call: prefer the stored plain
    text (fast, no vision); fall back to the rendered image. Returns
    ("text", str) | ("image", bytes) | None."""
    text = await asyncio.to_thread(resume_utils.get_resume_text, db, uid)
    if text:
        return ("text", text)
    img = await asyncio.to_thread(resume_utils.image_bytes, db, uid)
    if img:
        return ("image", img)
    return None


async def _ask_json_resume(source, prompt, cap=4000):
    """Run a JSON prompt against a resume source — text-only when we have the
    text (faster), else the vision path with the image."""
    kind, payload = source
    if kind == "text":
        full = f"{prompt}\n\n<resume>\n{payload}\n</resume>"
        return await asyncio.to_thread(gemma_client.ask_json_text, full, cap)
    return await asyncio.to_thread(
        gemma_client.ask_json_with_image, payload, prompt, cap
    )


NEED_RESUME = "You need a resume first — upload one with `/resume upload`."


# Five score bands, each its own color. Kept in sync with score_wheel.band().
def _score_color(score):
    if score is None:
        return discord.Color.light_grey()
    if score >= 80:
        return discord.Color.from_rgb(67, 181, 129)    # green — Excellent
    if score >= 60:
        return discord.Color.from_rgb(150, 200, 60)    # lime — Strong
    if score >= 40:
        return discord.Color.from_rgb(250, 197, 40)    # yellow — Moderate
    if score >= 20:
        return discord.Color.from_rgb(240, 138, 30)    # orange — Weak
    return discord.Color.from_rgb(237, 66, 69)         # red — Very weak


# XML-tagged prompt: clear delimiters parse cleaner than prose; strict-JSON
# output (via response_mime_type) removes any scraping. The schema is shaped so
# each section answers one user question — how good a fit, why, what's holding
# it back, what to do next — with tight caps to stay scannable and fast.
_SCORE_PROMPT = (
    persona.SILVER_WOLF_SYSTEM
    + """

<expertise>
On top of being Silver Wolf, you have a hiring manager's instincts: 20 years \
screening tens of thousands of resumes, deep ATS knowledge, and a sharp read on \
exactly what turns a resume into a callback. You're scoring this candidate's \
resume against the job like you're sizing up whether to invite them to the boss \
fight (the interview).
</expertise>

<voice_for_this_task>
Write EVERY text field (summary, highest_reason, lowest_reason, why_not_higher, \
strengths, gaps, quick_wins) fully in-character as Silver Wolf — cocky, sharp, \
teasing, genuinely on their side. Commit to the bit: treat the résumé as their \
loadout / build, the job as a raid or boss fight, matched skills as good gear or \
maxed stats, missing requirements as unpatched bugs / missing gear / locked \
content, quick wins as easy XP or free loot, the interview as the boss you're \
prepping them to clear. Drop natural gamer-hacker slang (build, loadout, meta, \
grind, carry, T0, nerf, exploit, GG, "秒了"). Reference your Aether Editing / \
scanning their data when it fits.

LENGTH — go longer and richer than a dry one-liner:
- summary: 2-3 full sentences that actually explain the score with personality.
- highest_reason / lowest_reason: 1-2 punchy sentences each, with a concrete \
detail from the résumé or posting, not vague.
- why_not_higher: 2-3 sentences naming the highest-impact missing pieces AND \
what landing them would do to the score.
- strengths / gaps / quick_wins: each item a full, specific sentence (not a \
2-word fragment) — name the exact skill/tool/section and WHY it matters here.

TONE — she's brash and a little irreverent. A mild swear is fine when it lands \
naturally (e.g. "recruiter ghosting is bullshit", "this gap is gonna screw you", \
"damn solid build") — sparingly, for punch, never in every field, never slurs or \
anything nasty aimed at the candidate. Keep it confident and fun, not crude.

Stay truthful — the flavor is in the wording only, never in the facts or the \
numbers. Never invent skills, tools, or metrics. Follow the JSON exactly.
</voice_for_this_task>

<posting>
{posting}
</posting>

<rules>
- Judge ONLY on evidence visible in the resume image. Invent nothing.
- Surface the HIGHEST-impact gaps first (required skills > required experience \
> domain knowledge > nice-to-haves). Skip trivial keyword differences.
- Do not repeat the same skill across sections.
- Every string is one short line — optimized for fast scanning, no paragraphs.
- quick_wins must be realistic tweaks using what the candidate already has \
(add existing coursework, name tech already used, quantify results). Never \
suggest fabricating experience.
- tier: "Excellent" (85-100), "Strong" (70-84), "Moderate" (50-69), "Weak" (0-49).
</rules>

<subscores>
Rate three DISTINCT dimensions 0-100 (integers). They must measure different \
things with minimal overlap, judged on demonstrated evidence, NOT keyword count:

- technical_skills (Technical Alignment): how well the candidate's specific \
technical stack matches the technologies THIS role requires — languages, \
frameworks, libraries, dev tools, platforms. This is NOT overall engineering \
ability: a strong engineer whose stack differs from the requirements should \
still get only a moderate score here.

- experience (Experience): how effectively the resume shows the candidate can \
DO this job — projects, research, internships, leadership, coursework, technical \
impact, complexity of work. Judge demonstrated experience, not years. Do NOT \
heavily penalize a student for lacking internships if strong projects give \
equivalent evidence.

- domain_fit (Domain Fit): how closely the candidate's background matches the \
role's SPECIALIZED industry knowledge (AI/ML, cybersecurity, robotics, embedded, \
cloud infra, enterprise software, finance, data engineering, etc). General SWE \
experience does NOT earn full credit when the role needs specialized domain \
expertise.

The overall score must be consistent with these three but need not be their \
average. Then: one sentence for the HIGHEST subscore (why it scored highest, \
resume evidence), one for the LOWEST (why lowest, using job + resume evidence), \
and a "why_not_higher": 1-2 sentences naming the primary HIGHEST-IMPACT missing \
qualifications holding the overall score back — not a list of every gap.
</subscores>

<bilingual>
Every user-facing TEXT field must be written TWICE — once in English (Silver \
Wolf's English voice) and once in fluent, natural Simplified Chinese (银狼的中文 \
语气：同样的痞帅游戏黑客口吻，游戏黑客俚语). The Chinese is not a stiff literal \
translation — it's Silver Wolf actually speaking Chinese, same energy. Numbers \
and tier are language-neutral (single value).
</bilingual>

<output_format>
Return ONLY this JSON object, no prose. Use ONLY flat string arrays exactly as \
shown — do not nest objects inside the arrays:
{{
  "score": <integer 0-100>,
  "tier": "<Excellent|Strong|Moderate|Weak>",
  "summary_en": "<2-3 sentences, Silver Wolf voice, explaining the score>",
  "summary_zh": "<中文：2-3 句，银狼语气，解释分数>",
  "technical_skills": <integer 0-100>,
  "experience": <integer 0-100>,
  "domain_fit": <integer 0-100>,
  "highest_reason_en": "<1-2 sentences on the strongest subscore, with a concrete detail>",
  "highest_reason_zh": "<中文，1-2 句，带具体细节>",
  "lowest_reason_en": "<1-2 sentences on the weakest subscore, with a concrete detail>",
  "lowest_reason_zh": "<中文，1-2 句，带具体细节>",
  "why_not_higher_en": "<2-3 sentences: the highest-impact missing pieces AND what landing them does to the score>",
  "why_not_higher_zh": "<中文，2-3 句>",
  "strengths_en": ["<one full sentence: the exact skill/tool, why it matters for THIS role, and where it shows in the résumé>"],
  "strengths_zh": ["<中文，完整一句>"],
  "gaps_en": ["<one full sentence: the exact missing requirement and why it hurts here>"],
  "gaps_zh": ["<中文，完整一句>"],
  "quick_wins_en": ["<one full sentence: a specific, realistic, truthful tweak>"],
  "quick_wins_zh": ["<中文，完整一句>"]
}}
Caps: strengths<=3, gaps<=4, quick_wins<=4 (each language). Each item is a FULL \
sentence, not a fragment. The _en and _zh arrays must have the SAME number of \
items in the same order.
</output_format>"""
)


def _bullets(items, limit=5):
    """Join a JSON string-list into an embed bullet block, capped."""
    if not isinstance(items, list):
        return None
    lines = [f"• {str(x).strip()}" for x in items[:limit] if str(x).strip()]
    return "\n".join(lines) or None


_TIER_EMOJI = {
    "excellent": "🟢",
    "strong": "🟢",
    "moderate": "🟡",
    "weak": "🟠",
}


def _tier_emoji(tier, score):
    """Tier emoji, falling back to the score band if tier text is missing."""
    e = _TIER_EMOJI.get((tier or "").lower())
    if e:
        return e
    if score is None:
        return ""
    if score >= 80:
        return "🟢"
    if score >= 60:
        return "🟢"
    if score >= 40:
        return "🟡"
    if score >= 20:
        return "🟠"
    return "🔴"


_SUBSCORES = (
    ("technical_skills", "Technical Alignment"),
    ("experience", "Experience"),
    ("domain_fit", "Domain Fit"),
)


def _band_dot(pct):
    """A colored dot matching the 5-band scale for a 0-100 value."""
    if pct >= 80:
        return "🟢"
    if pct >= 60:
        return "🟢"
    if pct >= 40:
        return "🟡"
    if pct >= 20:
        return "🟠"
    return "🔴"


def _bar(pct, width=12):
    """A fine-grained gauge bar for a 0-100 value using block glyphs."""
    pct = max(0, min(100, pct))
    filled = pct / 100 * width
    full = int(filled)
    rem = filled - full
    # Partial-cell glyph for a smoother end.
    partial = ""
    if full < width:
        partial = "▓" if rem >= 0.5 else ("▒" if rem >= 0.15 else "")
    used = full + (1 if partial else 0)
    return "█" * full + partial + "░" * (width - used)


def _subscore_block(data, labels):
    """Render the three subscores as aligned bars with band dots, using the
    given localized `labels` (technical, experience, domain). Returns None if no
    subscore is present."""
    keys = [k for k, _ in _SUBSCORES]
    vals = []
    for key, label in zip(keys, labels):
        raw = data.get(key)
        try:
            vals.append((label, max(0, min(100, int(raw)))))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    width = max(len(lbl) for lbl, _ in vals)
    lines = [
        f"{_band_dot(v)} `{lbl:<{width}}` `{_bar(v)}` **{v}**"
        for lbl, v in vals
    ]
    return "\n".join(lines)


# Localized field labels + footer for the Score embed.
_SCORE_LABELS = {
    "en": {
        "author": "{company} · Silver Wolf's Scan",
        "breakdown": "📊 Stat Breakdown",
        "reasons": "​",
        "strongest": "▲ **Strongest**",
        "weakest": "▼ **Weakest**",
        "why_not": "🔒 What's Capping Your Score",
        "strengths": "✅ Best Gear",
        "gaps": "🐛 Unpatched Bugs",
        "wins": "⚡ Easy XP",
        "sub": ("Technical Alignment", "Experience", "Domain Fit"),
        "footer": "Scanned by Silver Wolf · double-check before you trust the RNG",
    },
    "zh": {
        "author": "{company} · 银狼的扫描",
        "breakdown": "📊 属性面板",
        "reasons": "​",
        "strongest": "▲ **最强项**",
        "weakest": "▼ **最弱项**",
        "why_not": "🔒 卡住你分数的东西",
        "strengths": "✅ 最强装备",
        "gaps": "🐛 未修复的 Bug",
        "wins": "⚡ 轻松经验值",
        "sub": ("技术契合度", "经验", "领域匹配"),
        "footer": "银狼扫描完毕 · 别全信 RNG，自己再核对一遍",
    },
}


def _pick(data, base, lang):
    """A localized string field: prefer base_<lang>, fall back to base_en / base."""
    for key in (f"{base}_{lang}", f"{base}_en", base):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _pick_list(data, base, lang):
    """A localized string-list field with the same fallback chain."""
    for key in (f"{base}_{lang}", f"{base}_en", base):
        v = data.get(key)
        if isinstance(v, list) and v:
            return v
    return []


def _reason_block(data, lang):
    lab = _SCORE_LABELS[lang]
    hi = _pick(data, "highest_reason", lang)
    lo = _pick(data, "lowest_reason", lang)
    lines = []
    if hi:
        lines.append(f"{lab['strongest']} — {hi}")
    if lo:
        lines.append(f"{lab['weakest']} — {lo}")
    return "\n".join(lines) or None


def _build_score_embed(row, data, lang="en"):
    """Assemble a modern, scannable, localized Score embed. `lang` in {en, zh}."""
    lab = _SCORE_LABELS.get(lang, _SCORE_LABELS["en"])
    score = data.get("score")
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = None

    tier = str(data.get("tier") or "").strip()
    company = row.get("company_name") or "Company"
    job_title = row.get("job_title") or "Role"

    company_info = row.get("company_info")
    if isinstance(company_info, list):
        company_info = company_info[0] if company_info else {}
    company_info = company_info or {}
    logo = company_info.get("company_logo")

    embed = discord.Embed(
        title=f"{job_title}"[:256],
        url=row.get("job_url") or None,
        color=_score_color(score),
    )
    embed.set_author(name=lab["author"].format(company=company)[:256], icon_url=logo or None)

    headline = ""
    if score is not None:
        headline = f"## {_tier_emoji(tier, score)} {score}%  ·  {tier}".rstrip(" ·")
    summary = _pick(data, "summary", lang)
    desc_parts = [p for p in (headline, summary) if p]
    if desc_parts:
        embed.description = ("\n".join(desc_parts) + "\n―――")[:4096]

    sub = _subscore_block(data, lab["sub"])
    if sub:
        embed.add_field(name=lab["breakdown"], value=sub[:1024], inline=False)
    reasons = _reason_block(data, lang)
    if reasons:
        embed.add_field(name=lab["reasons"], value=reasons[:1024], inline=False)

    why_not = _pick(data, "why_not_higher", lang)
    if why_not:
        embed.add_field(name=lab["why_not"], value=why_not[:1024], inline=False)

    strengths = _bullets(_pick_list(data, "strengths", lang), limit=3)
    if strengths:
        embed.add_field(name=lab["strengths"], value=strengths[:1024], inline=False)

    gaps = _bullets(_pick_list(data, "gaps", lang), limit=4)
    if gaps:
        embed.add_field(name=lab["gaps"], value=gaps[:1024], inline=False)

    wins = _bullets(_pick_list(data, "quick_wins", lang), limit=4)
    if wins:
        embed.add_field(name=lab["wins"], value=wins[:1024], inline=False)

    location = row.get("job_location")
    foot = lab["footer"]
    if location:
        foot = f"📍 {location}  ·  {foot}"
    embed.set_footer(text=foot[:2048])
    return embed, score


async def run_score(db, user, table, row_id):
    """Resume-vs-this-posting match. Returns (embed, file, error) — file is the
    score-wheel PNG (or None). Cached per (user, job): a repeat click skips
    Gemma entirely and returns instantly. Cache is cleared on resume change.

    Fast path: uses the stored resume TEXT (text-only Gemma call) when present,
    falling back to the rendered image. Row + user lookups run in parallel."""
    row, uid = await asyncio.gather(
        asyncio.to_thread(_fetch_row, db, table, row_id),
        asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        ),
    )
    if not row:
        return None, None, None, "That posting is no longer available."

    data = None
    if uid:
        data = await asyncio.to_thread(db.get_cached_score, uid, table, row_id)

    if data is None:
        if not uid:
            return None, None, None, NEED_RESUME
        source = await _resume_source(db, uid)
        if not source:
            return None, None, None, NEED_RESUME

        prompt = _SCORE_PROMPT.format(posting=_job_context(row))
        # Cap is generous: Gemma's JSON mode can silently burn budget and return
        # empty at a tight cap (2000) yet completes cleanly at ~270 tokens with
        # 4000. Billing is on actual output, so the headroom is free.
        data = await _ask_json_resume(source, prompt, 4000)
        if not data:
            return None, None, None, "The AI couldn't score the match right now — try again later."

        await asyncio.to_thread(db.set_cached_score, uid, table, row_id, data)

    # Render the wheel bytes ONCE, then hand out a fresh discord.File per send.
    # A File is single-use, and Discord drops attachments on any edit that does
    # not re-supply them — so the toggle must re-attach a new File built from the
    # same bytes, or the top-right circle disappears after switching languages.
    _e0, score = _build_score_embed(row, data, "en")
    png = None
    if score is not None:
        try:
            png = await asyncio.to_thread(score_wheel.render, score)
        except Exception:
            log.exception("Failed rendering score wheel")

    have_wheel = png is not None

    def make_file():
        return discord.File(io_bytes(png), filename="score.png") if have_wheel else None

    def build(lang):
        embed, _ = _build_score_embed(row, data, lang)
        if have_wheel:
            embed.set_thumbnail(url="attachment://score.png")
        return embed

    view = lang_view.LangToggleView(build, lang="en", make_file=make_file)
    return build("en"), make_file(), view, None


# Tailor outputs the resume as YAML in the husayni/resume_builder schema — a
# downstream builder renders it to PDF. The model REWORDS the candidate's real
# content toward the posting; it must never invent anything (see <truth_rules>).
_TAILOR_PROMPT = (
    persona.SILVER_WOLF_SYSTEM
    + """

<this_task>
You're **Aether Editing** this candidate's whole résumé so it slips past the ATS \
and lands clean on the recruiter — your favorite kind of system to game. You also \
have a hiring manager's instincts and know exactly which keywords, phrasings, and \
section structures beat the screeners while still impressing a human.

CRITICAL — the résumé you output goes onto a REAL résumé a recruiter reads: the \
content stays crisp, professional, recruiter-grade. NO Silver Wolf slang or \
in-character chatter inside the résumé itself. You're the operator; you don't \
sign the work. Your personality shows in how sharp the optimization is, nothing \
else.
</this_task>

<ats_mission priority="HIGHEST">
Your ONE job is to get this résumé PAST the ATS keyword/relevance filter so a \
human ever sees it. Most résumés die here, silently, before any person reads \
them — do not let that happen. Optimize aggressively for the parser:

1. KEYWORD MATCH IS EVERYTHING. Pull the exact hard skills, tools, technologies, \
frameworks, certifications, and role nouns from the posting. Wherever the \
candidate has TRUTHFULLY done that thing, use the posting's EXACT string, spelled \
and cased like the posting (e.g. if the posting says "Node.js", write "Node.js", \
not "NodeJS"; "CI/CD" not "continuous integration"). Exact-string matching is how \
ATS scores relevance.
2. MIRROR THE POSTING'S LANGUAGE. Re-title and re-word the candidate's real \
experience to echo the posting's phrasing for the same work. Same concept, their \
words.
3. SPELL OUT ACRONYMS BOTH WAYS at least once where natural — e.g. "Amazon Web \
Services (AWS)", "Natural Language Processing (NLP)" — so the parser catches \
either form.
4. COVER THE REQUIRED SKILLS the candidate genuinely has. If a required skill is \
truthfully present but buried, surface it into a bullet or the skills section so \
the ATS finds it. Put a "Skills" section with a clean, comma-listed set of the \
real, role-relevant hard skills (great for keyword density).
5. STANDARD, PARSER-SAFE STRUCTURE: conventional section headings (Education, \
Experience, Projects, Skills), simple bullets, no tables/columns/graphics/odd \
characters. Dates and titles in plain text.
6. STRONG ACTION VERB + KEYWORD in every bullet; front-load the relevant tech.
7. DENSITY WITHOUT STUFFING: weave keywords naturally into real accomplishments. \
Never keyword-spam gibberish, and NEVER claim a skill the résumé doesn't support \
— a fabricated keyword that gets caught in the interview is worse than a miss.

Bottom line: maximize truthful keyword and relevance overlap with the posting so \
the ATS ranks this résumé high enough to reach a human. That is the win \
condition.
</ats_mission>

<posting>
{posting}
</posting>"""
    + """

<task>
Rewrite the candidate's resume tailored to this posting so it passes the ATS. \
Reorder and emphasize the most relevant real items first, and reword existing \
bullets to mirror the posting's language and include the posting's keywords \
ONLY where they truthfully describe work the candidate already did.

Write every experience and project bullet using Google's XYZ formula: \
"Accomplished [X] as measured by [Y], by doing [Z]" — i.e. lead with the \
accomplishment/impact [X], quantify it with a metric [Y], and state how it was \
done with the tools/methods [Z]. BUT the metric [Y] must be REAL: if the \
original resume does not state a number for that bullet, do NOT invent one — \
write the [Y] slot as the literal placeholder "[ADD METRIC]" for the candidate \
to fill in. Never fabricate a figure to complete the formula.

Style: start each bullet with a strong past-tense action verb and do not reuse \
the same opening verb twice; lead with impact not task; cut weak filler \
("responsible for", "helped with", "worked on"); weave in the posting's EXACT \
keyword/tech strings verbatim where truthful (ATS matches exact text); keep \
bullets to one tight active-voice line, no first person. Order sections and \
bullets so the most role-relevant content comes first.
</task>

<truth_rules>
CRITICAL — the output must be 100% TRUE to the original resume. A fabricated \
resume gets the candidate fired or rejected. Obey ALL of these:
- Use ONLY facts present in the original resume: names, employers, schools, \
degrees, dates, titles, projects, skills, links. Copy them verbatim.
- NEVER invent or infer: employers, roles, degrees, dates, certifications, \
publications, skills, tools, or technologies the resume does not explicitly show.
- NEVER add, change, or guess NUMBERS or metrics (percentages, dollar amounts, \
user counts, team sizes, latencies, "improved X by Y%"). If a bullet would be \
stronger with a metric that is NOT in the original, append the literal \
placeholder " [ADD METRIC]" so the candidate can fill it in manually. Do not \
insert any number yourself.
- Only add a posting keyword to a bullet if the candidate genuinely did that \
work. If unsure, leave it out.
- Do not upgrade job titles, seniority, or scope. Keep them exactly as written.
- You may rephrase and reorder real content, and drop less-relevant items. You \
may NOT create new content.
- If a field is unknown, leave it empty ("") — never guess.
</truth_rules>

<output_format>
Output ONLY valid YAML, no prose, no code fences, in EXACTLY this structure \
(omit any section the resume has no data for; keep keys lowercase). Every value \
must come from the original resume per <truth_rules>:

name: <full name from resume>
contact:
  phone: "<phone or empty>"
  email: <email or empty>
  linkedin: <username or empty>
  github: <username or empty>
education:
  - school: <school>
    location: <city, state>
    degree: <degree, and append ", GPA X.XX" if the resume states a GPA>
    dates: "<start - end>"
experience:
  - company: <company>
    role: <title>
    location: <city, state>
    dates: "<start - end>"
    description:
      - "<XYZ bullet: action verb + accomplishment, measured by [ADD METRIC] if the resume has no real number, by doing Z with real tools>"
projects:
  - name: <project>
    technologies: [<tech>, <tech>]
    date: <dates>
    link: <url or empty>
    description:
      - "<XYZ bullet built only from real content; [ADD METRIC] where no real number exists>"
skills:
  - category: <e.g. Languages>
    list: [<skill>, <skill>]
  - category: Relevant Coursework   # include ONLY if the resume lists courses
    list: [<course>, <course>]
achievements:
  - <real achievement>
publications:
  - <real publication>
certifications:
  - name: <cert>
    link: <url or empty>
</output_format>"""
)


def _strip_yaml_fence(text):
    """Remove any ```yaml ... ``` fence the model wraps the YAML in."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


# --- Fast tailor: rewrite only the bullets, keep everything else verbatim ----

_BULLET_REWRITE_PROMPT = (
    persona.SILVER_WOLF_SYSTEM
    + """

<this_task>
Right now you're doing your favorite move: **Aether Editing** a candidate's \
resume so it slips past the ATS and hits the recruiter clean. You're a genius \
hacker AND you know exactly how these screening systems rank text — this is a \
system you can game.

CRITICAL — output register: the résumé bullets you produce go straight onto a \
REAL résumé a human recruiter reads. So the bullet TEXT itself stays crisp, \
professional, recruiter-grade — NOT slang, NOT in-character chatter. Silver Wolf \
is the operator making the edit; she does not sign her name in the output. Your \
personality lives in HOW sharp and optimized the rewrite is, not in goofy \
wording. No gamer slang inside the bullets.
</this_task>

<posting>
{posting}
</posting>

<ats_mission priority="HIGHEST">
Your #1 goal: make each bullet PASS the ATS keyword/relevance filter so a human \
ever reads it. Most résumés die at the parser, silently. Optimize hard:
- Pull the EXACT hard-skill / tool / framework / technology strings from the \
posting and use them verbatim, spelled and cased exactly like the posting \
(e.g. "Node.js" not "NodeJS", "CI/CD" not "continuous integration"), wherever the \
bullet TRUTHFULLY involved that thing.
- Mirror the posting's phrasing for the same work — their words, the candidate's \
real accomplishment.
- Front-load the relevant technology/keyword in each bullet.
- Where natural, expand an acronym once, e.g. "Amazon Web Services (AWS)".
- NEVER invent or imply a skill the bullet doesn't support — a fake keyword that \
surfaces in the interview is worse than a miss. Density from REAL content only.
</ats_mission>

<task>
Rewrite each resume bullet below to target this posting and pass the ATS, using \
Google's XYZ formula: "Accomplished [X] as measured by [Y], by doing [Z]" — lead \
with the accomplishment [X], put the metric slot [Y] as the literal placeholder \
"[ADD METRIC]", then how [Z] (naming the real, posting-matching tools).
</task>

<style_rules>
- Start every bullet with a strong past-tense action verb (Built, Led, \
Designed, Automated, Optimized, Shipped, Reduced, Architected).
- Lead with impact/outcome, not the task. Cut weak filler ("responsible for", \
"helped with", "worked on").
- Weave in the posting's EXACT keyword/tech strings verbatim where truthful.
- One tight active-voice line each, no first person, professional tone.
</style_rules>

<truth_rules>
- Reword ONLY — never invent facts, tools, employers, scope, or NUMBERS. The \
only number-like token allowed is the literal "[ADD METRIC]".
- Keep the SAME number of bullets, in the SAME order.
</truth_rules>

<output_format>
Output ONLY the rewritten bullets, one per numbered line (e.g. "1. ..."), same \
count and order as the input. No JSON, no headers, no commentary, no persona voice.
e.g.
  1. Cut API latency as measured by [ADD METRIC] by adding a Redis cache
</output_format>

<bullets>
{bullets}
</bullets>"""
)


# A tight second-pass prompt: turn metric-version bullets into clean no-metric
# versions, kept role-tailored. Run on the FAST tier (Flash-lite) — cheap.
_NOMETRIC_PROMPT = """Rewrite each bullet to remove the metric/measurement clause \
so it reads naturally WITHOUT any number, while keeping it tailored to this role \
and ATS-friendly. Do not invent anything; only remove the "as measured by …" \
part and smooth the wording.

<role_context>
{posting}
</role_context>

<output_format>
Output ONLY the rewritten bullets, one per numbered line ("1. ..."), same count \
and order. No commentary.
</output_format>

<bullets>
{bullets}
</bullets>"""


def _collect_bullets(structured):
    """Gather every experience/project bullet with a locator so we can splice
    rewrites back. Returns (locators, texts)."""
    locators, texts = [], []
    for section in ("experience", "projects"):
        items = structured.get(section)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            desc = item.get("description") if isinstance(item, dict) else None
            if not isinstance(desc, list):
                continue
            for j, bullet in enumerate(desc):
                if str(bullet).strip():
                    locators.append((section, i, j))
                    texts.append(str(bullet).strip())
    return locators, texts


def _splice_bullets(structured, locators, rewritten):
    """Return a deep-ish copy of structured with rewritten bullets spliced in.
    Falls back to the original bullet if a rewrite is missing/blank."""
    import copy

    out = copy.deepcopy(structured)
    for (section, i, j), new in zip(locators, rewritten):
        new = str(new).strip()
        if not new:
            continue
        try:
            out[section][i]["description"][j] = new
        except (KeyError, IndexError, TypeError):
            continue
    return out


_BULLET_CHUNK = 6  # Flash-lite handles this fast + reliably; fewer round-trips


def _apply_edu_extras(structured):
    """The resume builder's education schema is fixed (school/location/degree/
    dates) — it has no GPA or coursework fields. Fold any captured GPA into the
    degree string and surface coursework as a 'Relevant Coursework' skills
    category, then drop the non-schema keys so the builder doesn't warn. Returns
    a modified deep copy."""
    import copy

    out = copy.deepcopy(structured)

    coursework_all = []
    for edu in out.get("education", []) or []:
        if not isinstance(edu, dict):
            continue
        gpa = str(edu.pop("gpa", "") or "").strip()
        courses = edu.pop("coursework", None) or []
        if gpa and "gpa" not in (edu.get("degree", "") or "").lower():
            sep = " · " if edu.get("degree") else ""
            edu["degree"] = f"{edu.get('degree', '')}{sep}GPA {gpa}".strip()
        for c in courses:
            c = str(c).strip()
            if c and c not in coursework_all:
                coursework_all.append(c)

    if coursework_all:
        skills = out.setdefault("skills", [])
        if not isinstance(skills, list):
            skills = out["skills"] = []
        # Merge into an existing coursework category or prepend a new one.
        existing = next(
            (s for s in skills if isinstance(s, dict)
             and "coursework" in str(s.get("category", "")).lower()),
            None,
        )
        if existing:
            lst = existing.setdefault("list", [])
            for c in coursework_all:
                if c not in lst:
                    lst.append(c)
        else:
            skills.append({"category": "Relevant Coursework", "list": coursework_all})
    return out


def _parse_numbered(text, expected):
    """Parse '1. ...' plain-text lines into a list of `expected` strings, in
    order. Tolerates missing numbers / wrapped continuation lines."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*(\d+)[.)]\s*(.*)$", line)
        if m:
            out.append(m.group(2).strip().strip('"').strip("-").strip())
        elif out:
            out[-1] = (out[-1] + " " + line).strip()
    return out[:expected]


def _rewrite_chunk(posting_ctx, chunk):
    """Rewrite a small batch of bullets into their METRIC versions (1x output —
    fast). Returns a list aligned to the chunk, keeping originals on shortfall."""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(chunk))
    prompt = _BULLET_REWRITE_PROMPT.format(posting=posting_ctx, bullets=numbered)
    text = gemma_client.ask_text(prompt, chain=gemma_client.FAST_CHAIN)
    parsed = _parse_numbered(text, len(chunk))
    return [
        parsed[i] if i < len(parsed) and parsed[i] else original
        for i, original in enumerate(chunk)
    ]


def _strip_metric_phrase(text):
    """Rough no-metric version: remove an 'as measured by [ADD METRIC]' clause."""
    if not text:
        return text
    t = re.sub(r"(?i)\s*,?\s*as measured by\s*\[ADD METRIC\]", "", text)
    return t.replace(_METRIC_TOKEN, "").strip()


def _polish_nometrics(posting_ctx, metric_bullets):
    """One FAST-tier pass: turn the metric-version bullets into clean, role-
    tailored no-metric versions. Returns a list aligned to the input; falls back
    to a local strip for any the model drops. Blocking."""
    if not metric_bullets:
        return []
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(metric_bullets))
    prompt = _NOMETRIC_PROMPT.format(posting=posting_ctx, bullets=numbered)
    text = gemma_client.ask_text(prompt, chain=gemma_client.FAST_CHAIN)
    parsed = _parse_numbered(text, len(metric_bullets))
    out = []
    for i, m in enumerate(metric_bullets):
        n = parsed[i] if i < len(parsed) and parsed[i] else ""
        # Never let a number the model hallucinated into the no-metric text; if
        # it still has [ADD METRIC] or looks empty, fall back to the strip.
        if not n or _METRIC_TOKEN in n:
            n = _strip_metric_phrase(m)
        out.append(n)
    return out


async def _tailor_build(structured, row, progress=None):
    """Fast path (1x main output): rewrite bullets to their METRIC versions in
    small concurrent batches, then ONE Flash-lite pass derives clean, role-
    tailored no-metric versions. Returns a TAILOR BLOB:
        {"structured": <resume>, "bullets": [{"loc": [sec,i,j], "m": ..., "n": ...}]}
    Returns None on total failure. `progress(done, total)` ticks over the batches
    plus the final polish step."""
    locators, texts = _collect_bullets(structured)
    if not texts:
        return {"structured": structured, "bullets": []}

    posting_ctx = _job_context(row)
    chunks = [texts[i : i + _BULLET_CHUNK] for i in range(0, len(texts), _BULLET_CHUNK)]
    # +1 for the no-metric polish step at the end.
    total = len(chunks) + 1
    if progress:
        await progress(0, total)

    results = [None] * len(chunks)
    done = 0
    lock = asyncio.Lock()

    async def run_chunk(idx, chunk):
        nonlocal done
        results[idx] = await asyncio.to_thread(_rewrite_chunk, posting_ctx, chunk)
        async with lock:
            done += 1
            if progress:
                await progress(done, total)

    await asyncio.gather(*(run_chunk(i, c) for i, c in enumerate(chunks)))

    metric_bullets = []
    for r in results:
        if r:
            metric_bullets.extend(r)
    if not metric_bullets:
        return None

    # One cheap batched pass for the clean no-metric variants.
    nometrics = await asyncio.to_thread(
        _polish_nometrics, posting_ctx, metric_bullets
    )
    if progress:
        await progress(total, total)

    bullets = [
        {
            "loc": list(loc),
            "m": m,
            "n": nometrics[i] if i < len(nometrics) else _strip_metric_phrase(m),
        }
        for i, (loc, m) in enumerate(zip(locators, metric_bullets))
    ]
    return {"structured": structured, "bullets": bullets}


_METRIC_TOKEN = "[ADD METRIC]"
_MODAL_PAGE = 5  # Discord modal hard limit is 5 inputs


def _blob_to_yaml(blob, values=None):
    """Assemble resume YAML from a tailor blob. For each tailored bullet: if a
    metric value is provided (non-empty, non-skip), use the metric version with
    [ADD METRIC] replaced by that value; otherwise use the clean no-metric
    version. `values` is a dict {bullet_index: value}. Education/skills untouched.
    """
    import copy

    import yaml as _yaml

    structured = copy.deepcopy(blob.get("structured") or {})
    values = values or {}
    for idx, b in enumerate(blob.get("bullets") or []):
        sec, i, j = b["loc"]
        val = str(values.get(idx, "") or "").strip()
        if val and val != _SKIP:
            text = (b.get("m") or b.get("n") or "").replace(_METRIC_TOKEN, val)
        else:
            # Blank / skipped → the clean no-metric version.
            text = b.get("n") or _strip_metric_phrase(b.get("m", ""))
        # Safety: a placeholder must NEVER reach a recruiter/ATS. If the
        # no-metric fallback somehow still carries the token, strip it clean.
        if _METRIC_TOKEN in text:
            text = _strip_metric_phrase(text)
        try:
            structured[sec][i]["description"][j] = text
        except (KeyError, IndexError, TypeError):
            continue
    return _yaml.safe_dump(
        _apply_edu_extras(structured), sort_keys=False, allow_unicode=True
    )


def _blob_metric_count(blob):
    """How many tailored bullets have a metric slot to fill."""
    return sum(
        1 for b in (blob.get("bullets") or [])
        if _METRIC_TOKEN in (b.get("m") or "")
    )


def progress_bar(done, total, width=12):
    """A text progress bar like '▰▰▰▱▱▱▱▱ 3/8'."""
    total = max(1, total)
    filled = round(done / total * width)
    pct = round(done / total * 100)
    return f"{'▰' * filled}{'▱' * (width - filled)}  {pct}%  ({done}/{total})"


def make_progress_updater(interaction, verb="Aether Editing your résumé"):
    """Return an async progress(done, total) that edits the interaction's
    deferred response with a status line + live bar. Failure-tolerant."""
    async def progress(done, total):
        bar = progress_bar(done, total)
        content = (
            f"✍️ **{verb}…** — hang tight, I'll **DM** you the preview when the "
            "rewrite's done. Go grab a drink, this is my job.\n"
            f"{bar}"
        )
        try:
            await interaction.edit_original_response(content=content, embed=None)
        except Exception:
            pass  # a dropped progress edit must never break the actual work
    return progress


async def deliver_tailor(interaction, embed, file, view):
    """Deliver a finished tailor: DM the PDF/embed to the user, then collapse the
    ephemeral progress message to a short confirmation. Falls back to the
    ephemeral message if DMs are closed."""
    import discord

    user = interaction.user
    dmed = False
    try:
        dm = await user.create_dm()
        kwargs = {}
        if embed is not None:
            kwargs["embed"] = embed
        if file is not None:
            kwargs["file"] = file
        if view is not None:
            kwargs["view"] = view
        await dm.send(**kwargs)
        dmed = True
    except (discord.Forbidden, discord.HTTPException):
        dmed = False
    except Exception:
        log.exception("Failed DMing tailored resume")
        dmed = False

    if dmed:
        # Clear the ephemeral (only-you) progress — result now lives in DMs.
        try:
            await interaction.edit_original_response(
                content="📬 Dropped the Aether-Edited résumé in your **DMs**. Go check.",
                embed=None,
                attachments=[],
                view=None,
            )
        except Exception:
            pass
        return

    # DMs closed — send the result ephemerally instead (best effort).
    kwargs = {"ephemeral": True}
    if embed is not None:
        kwargs["embed"] = embed
    if file is not None:
        kwargs["file"] = file
    if view is not None:
        kwargs["view"] = view
    try:
        await interaction.edit_original_response(
            content="📎 Couldn't DM you (DMs closed) — here it is:",
            embed=embed, attachments=[file] if file else [], view=view,
        )
    except Exception:
        try:
            await interaction.followup.send(
                content="📎 Couldn't DM you (DMs closed) — here it is:", **kwargs
            )
        except Exception:
            log.exception("Failed delivering tailor ephemerally")


# Sentinel for "user chose to skip this metric" (distinct from not-yet-filled).
_SKIP = "\x00SKIP\x00"


def _metric_indices(blob):
    """Bullet indices (into blob['bullets']) that actually have a metric slot."""
    return [
        i for i, b in enumerate(blob.get("bullets") or [])
        if _METRIC_TOKEN in (b.get("m") or "")
    ]


class MetricsModal(discord.ui.Modal):
    """One page (up to 5) of metric inputs for the bullets that have a metric
    slot. Pre-fills entered values so the user can edit; writes back by index."""

    def __init__(self, view, page):
        idxs = view.metric_indices
        total_pages = (len(idxs) + _MODAL_PAGE - 1) // _MODAL_PAGE
        title = "Add your real metrics"
        if total_pages > 1:
            title += f" ({page + 1}/{total_pages})"
        super().__init__(title=title[:45])
        self._view = view
        self._fields = []
        page_idxs = idxs[page * _MODAL_PAGE : (page + 1) * _MODAL_PAGE]
        for idx in page_idxs:
            b = view.blob["bullets"][idx]
            shown = (b.get("m") or "").replace(_METRIC_TOKEN, "____")
            current = view.values.get(idx, "")
            default = "" if current in ("", _SKIP) else current
            field = discord.ui.TextInput(
                label=f"Bullet {idx + 1}"[:45],
                placeholder=(shown[:97] + "…") if len(shown) > 98 else (shown or "your metric"),
                default=default or None,
                required=False,
                max_length=100,
            )
            self._fields.append((idx, field))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        for idx, field in self._fields:
            raw = (field.value or "").strip()
            # Empty box on submit = skip this one → its no-metric version is used.
            self._view.values[idx] = raw if raw else _SKIP
        await self._view.refresh(interaction)


_TAILOR_TEXT = {
    "en": {
        "title": "✍️ Aether-Edited résumé — {company}",
        "preview_have": (
            "Ran my **Aether Editing** on your résumé for this run. Here's the "
            "preview — **{filled}/{total} metrics filled**. Drop in real numbers to "
            "buff the bullets, or ship it as-is (blanks fall back to the clean "
            "no-number wording). I didn't invent a thing.\n"
            "Hit **📄 Download PDF** when you want the 1-page file."
        ),
        "preview_none": (
            "Ran my **Aether Editing** on your résumé — here's the preview. Hit "
            "**📄 Download PDF** for the 1-page file. Only your real stats, no "
            "fabricated loot."
        ),
        "name": "👤 Name", "education": "🎓 Education", "experience": "💼 Experience",
        "projects": "🛠️ Projects", "skills": "🧩 Skills",
        "footer": "Aether-Edited by Silver Wolf • review before you ship it",
        "add": "Add metrics ({n} left)", "edit": "Edit metrics",
        "build_no": "Build without metrics", "download": "Download PDF",
        "use_metrics": "Add metrics instead",
        "builder_off_name": "⚠️ Builder offline",
        "builder_off": ("Couldn't reach the PDF builder — here's the YAML. Press "
                        "**Download PDF** to retry."),
        "status_ready": ("Your **1-page** résumé's patched and ready — preview "
                         "below, click to download.\n"),
        "status_no_metrics": ("*Shipped without metrics — every bullet uses its "
                              "clean, number-free version.*"),
        "status_real": "*Only your real stats — no fabricated loot.*",
        "status_metrics": ("Numbers are your crit buff — drop in real ones to power "
                           "up the bullets. Leave any blank and I'll use the clean "
                           "no-number wording. Or hit **Build without metrics**.\n​"),
    },
    "zh": {
        "title": "✍️ 以太编辑过的简历 — {company}",
        "preview_have": (
            "用**以太编辑**帮你把简历改好了。这是预览——**已填 {filled}/{total} 个数据**。"
            "填上真实数字给要点加暴击，或者直接出（留空的会用干净的无数字版本）。"
            "我一个字都没编。\n"
            "想要一页 PDF 就点 **📄 Download PDF**。"
        ),
        "preview_none": (
            "用**以太编辑**帮你改好简历了——这是预览。点 **📄 Download PDF** 拿一页文件。"
            "只用你的真实数据，绝不刷假装备。"
        ),
        "name": "👤 姓名", "education": "🎓 教育", "experience": "💼 经历",
        "projects": "🛠️ 项目", "skills": "🧩 技能",
        "footer": "银狼以太编辑完成 • 提交前自己再看一眼",
        "add": "填写数据（还剩 {n} 个）", "edit": "修改数据",
        "build_no": "不填数据直接生成", "download": "下载 PDF",
        "use_metrics": "改为填写数据",
        "builder_off_name": "⚠️ 生成器离线",
        "builder_off": "连不上 PDF 生成器——先给你 YAML。点 **下载 PDF** 重试。",
        "status_ready": "你的**一页**简历已经打好补丁——下方预览，点击下载。\n",
        "status_no_metrics": "*没填数据直接出——每条要点用的都是干净的无数字版本。*",
        "status_real": "*只用你的真实数据，没有刷假装备。*",
        "status_metrics": ("数字就是你的暴击 buff——填真实的进去给要点加成。"
                           "留空的我会用无数字版本（我不编假的）。或者点 **不填数据直接生成**。\n​"),
    },
}


class TailorView(discord.ui.View):
    """Post-tailor workspace over a tailor blob (per-bullet metric / no-metric
    variants). Buttons: 🌐 lang · Add metrics · Build without metrics · Download.
    A blank/skipped metric uses that bullet's clean no-metric version. The résumé
    bullets themselves stay English; only the chat labels/notes localize."""

    def __init__(self, blob, company):
        super().__init__(timeout=1800)
        self.blob = blob
        self.company = company
        self.lang = "en"
        self.safe_company = re.sub(r"[^A-Za-z0-9_-]+", "_", company).strip("_") or "role"
        self.metric_indices = _metric_indices(blob)
        # index -> "" (untouched) | _SKIP | value
        self.values = {i: "" for i in self.metric_indices}
        self.no_metrics = False  # "Build without metrics" toggle
        self.last_pdf = None
        self._build_buttons()

    # --- state ------------------------------------------------------------
    @property
    def n_total(self):
        return len(self.metric_indices)

    @property
    def n_filled(self):
        return sum(1 for v in self.values.values() if v not in ("", _SKIP))

    @property
    def n_pending(self):
        return sum(1 for v in self.values.values() if v == "")

    def current_yaml(self):
        # no_metrics mode → force every bullet to its no-metric version.
        if self.no_metrics:
            vals = {i: _SKIP for i in self.metric_indices}
        else:
            vals = self.values
        return _blob_to_yaml(self.blob, vals)

    # --- rendering --------------------------------------------------------
    def preview_embed(self):
        """A readable, in-Discord preview of the tailored resume (no PDF yet).
        Shows the assembled sections so the user can read it before building."""
        import yaml as _yaml

        try:
            data = _yaml.safe_load(self.current_yaml()) or {}
        except Exception:
            data = self.blob.get("structured") or {}

        t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
        embed = discord.Embed(
            title=t["title"].format(company=self.company)[:256],
            color=discord.Color.blurple(),
        )
        total = self.n_total
        if total:
            embed.description = t["preview_have"].format(
                filled=self.n_filled, total=total
            )
        else:
            embed.description = t["preview_none"]

        name = str(data.get("name") or "").strip()
        if name:
            embed.add_field(name=t["name"], value=name[:1024], inline=False)

        for edu in (data.get("education") or [])[:2]:
            if not isinstance(edu, dict):
                continue
            line = " · ".join(
                str(edu.get(k, "")).strip()
                for k in ("degree", "school", "dates")
                if str(edu.get(k, "")).strip()
            )
            if line:
                embed.add_field(name=t["education"], value=line[:1024], inline=False)

        def _section(title, key):
            block = []
            for item in (data.get(key) or []):
                if not isinstance(item, dict):
                    continue
                head = str(item.get("role") or item.get("name") or "").strip()
                where = str(item.get("company") or "").strip()
                hdr = f"**{head}**" + (f" — {where}" if where else "")
                if hdr.strip("* "):
                    block.append(hdr)
                for b in (item.get("description") or [])[:4]:
                    block.append(f"• {str(b).strip()}")
            if block:
                text = "\n".join(block)
                embed.add_field(
                    name=title, value=(text[:1020] + "…") if len(text) > 1024 else text,
                    inline=False,
                )

        _section(t["experience"], "experience")
        _section(t["projects"], "projects")

        skills = []
        for s in (data.get("skills") or []):
            if isinstance(s, dict) and s.get("list"):
                cat = str(s.get("category", "")).strip()
                lst = ", ".join(str(x) for x in s["list"])
                skills.append(f"**{cat}:** {lst}" if cat else lst)
        if skills:
            txt = "\n".join(skills)
            embed.add_field(
                name=t["skills"], value=(txt[:1020] + "…") if len(txt) > 1024 else txt,
                inline=False,
            )

        embed.set_footer(text=t["footer"])
        return embed

    def status_embed(self):
        t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
        total = self.n_total
        if self.no_metrics or total == 0:
            embed = discord.Embed(
                title=t["title"].format(company=self.company)[:256],
                color=discord.Color.green(),
                description=(
                    t["status_ready"]
                    + (t["status_no_metrics"] if self.no_metrics else t["status_real"])
                ),
            )
            return embed

        filled = self.n_filled
        color = discord.Color.green() if filled == total else discord.Color.blurple()
        zh = self.lang == "zh"
        embed = discord.Embed(
            title=t["title"].format(company=self.company)[:256], color=color
        )
        blocks = "🟩" * filled + "⬜" * (total - filled)
        head = (f"**已填数据：{filled}/{total}**  {blocks}\n" if zh
                else f"**Metrics: {filled}/{total} filled**  {blocks}\n")
        embed.description = head + t["status_metrics"]
        SHOW = 9
        st_skip = "无数字版本" if zh else "no-metric version"
        st_need = "需要一个数字" if zh else "needs a number"
        st_added = "已填" if zh else "added"
        more_name = f"…还有 {total - SHOW} 条" if zh else f"…and {total - SHOW} more"
        more_val = ("用 **📊 填写数据** 填剩下的。" if zh
                    else "Use **📊 Add metrics** to fill the rest.")
        for shown, (idx, b) in enumerate(
            (i, self.blob["bullets"][i]) for i in self.metric_indices
        ):
            if shown >= SHOW:
                embed.add_field(name=more_name, value=more_val, inline=False)
                break
            val = self.values.get(idx, "")
            metric_txt = (b.get("m") or "").replace(_METRIC_TOKEN, "____")
            if val == _SKIP:
                mark, status = "➖", st_skip
                preview = b.get("n") or _strip_metric_phrase(b.get("m", ""))
            elif val:
                mark, status = "✅", f"{st_added} **{val}**"
                preview = (b.get("m") or "").replace(_METRIC_TOKEN, f"**{val}**")
            else:
                mark, status = "⬜", st_need
                preview = metric_txt
            preview = (preview[:150] + "…") if len(preview) > 151 else preview
            embed.add_field(
                name=f"{mark} {idx + 1}. {status}",
                value=preview or "*(bullet)*",
                inline=False,
            )
        embed.set_footer(text=t["footer"])
        return embed

    def default_embed(self):
        """Which embed to show right now: the read-only preview until the user
        starts building, then the status board on rebuilds."""
        return self.preview_embed()

    def _build_buttons(self):
        self.clear_items()
        t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])

        # 🌐 language toggle — flips chat labels/notes (résumé stays English).
        lang_btn = discord.ui.Button(
            label="🌐 中文" if self.lang == "en" else "🌐 English",
            style=discord.ButtonStyle.secondary,
        )
        lang_btn.callback = self._on_lang
        self.add_item(lang_btn)

        has_metrics = self.n_total > 0 and not self.no_metrics
        if has_metrics:
            btn = discord.ui.Button(
                label=(t["add"].format(n=self.n_pending) if self.n_pending
                       else t["edit"]),
                emoji="📊", style=discord.ButtonStyle.primary,
            )
            btn.callback = self._on_add
            self.add_item(btn)

            nom = discord.ui.Button(
                label=t["build_no"], emoji="🚫",
                style=discord.ButtonStyle.secondary,
            )
            nom.callback = self._on_no_metrics
            self.add_item(nom)

        download = discord.ui.Button(
            label=t["download"], emoji="📄", style=discord.ButtonStyle.success
        )
        download.callback = self._on_download
        self.add_item(download)

        if self.no_metrics and self.n_total:
            back = discord.ui.Button(
                label=t["use_metrics"], emoji="↩️",
                style=discord.ButtonStyle.secondary,
            )
            back.callback = self._on_use_metrics
            self.add_item(back)

    # --- actions ----------------------------------------------------------
    async def _on_lang(self, interaction):
        self.lang = "zh" if self.lang == "en" else "en"
        self._build_buttons()
        # Re-render whichever embed is currently showing (preview by default).
        await interaction.response.edit_message(
            embed=self.default_embed(), view=self
        )

    async def _on_add(self, interaction):
        # One combined button → open page 1; further pages appear only if needed.
        await interaction.response.send_modal(MetricsModal(self, 0))

    async def _on_no_metrics(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.no_metrics = True
        self._build_buttons()
        await self._send(interaction)

    async def _on_use_metrics(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.no_metrics = False
        self._build_buttons()
        await self._send(interaction)

    async def _on_download(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._send(interaction)

    async def refresh(self, interaction):
        self._build_buttons()
        await self._send(interaction)

    async def _send(self, interaction):
        yaml_text = self.current_yaml()
        pdf = await asyncio.to_thread(_build_pdf_sync, yaml_text)
        embed = self.status_embed()
        if pdf:
            self.last_pdf = pdf
            file = discord.File(io_bytes(pdf), filename=f"resume_{self.safe_company}.pdf")
        else:
            file = discord.File(
                io_bytes(yaml_text.encode("utf-8")),
                filename=f"tailored_resume_{self.safe_company}.yaml",
            )
            t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
            embed.add_field(
                name=t["builder_off_name"], value=t["builder_off"], inline=False,
            )
        await interaction.followup.send(
            embed=embed, file=file, view=self, ephemeral=True
        )


async def run_tailor(db, user, table, row_id, progress=None):
    """Tailor the resume for this posting as YAML (for the downstream resume
    builder). Returns (embed, file, view, error). `progress(done, total)` is
    awaited as bullet batches complete, so the caller can show a live bar."""
    row, uid = await asyncio.gather(
        asyncio.to_thread(_fetch_row, db, table, row_id),
        asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        ),
    )
    if not row:
        return None, None, None, "That posting is no longer available."
    if not uid:
        return None, None, None, NEED_RESUME

    # Cache: a repeat click (or a pre-tailored posting) returns instantly.
    cached = await asyncio.to_thread(db.get_cached_tailor, uid, table, row_id)
    blob = _load_blob(cached)
    if blob is None:
        blob = await _generate_tailor_blob(db, uid, row, progress=progress)
        if not blob:
            return None, None, None, "The AI couldn't tailor your resume right now — try again later."
        await asyncio.to_thread(
            db.set_cached_tailor, uid, table, row_id, _dump_blob(blob)
        )
    return await _finish_tailor(row, blob)


# Bump when the Tailor prompt / blob schema changes so stale caches (old voice,
# old bullet variants) are ignored and regenerated with the current prompt.
_TAILOR_BLOB_VERSION = 2


def _dump_blob(blob):
    import json
    stamped = {**blob, "_v": _TAILOR_BLOB_VERSION} if isinstance(blob, dict) else blob
    return json.dumps(stamped)


def _load_blob(cached):
    """Parse a cached tailor value into a blob dict. Returns None for empty,
    invalid, legacy, or stale-version caches so we regenerate."""
    if not cached:
        return None
    import json
    try:
        data = json.loads(cached)
    except Exception:
        return None
    if (
        isinstance(data, dict)
        and "bullets" in data
        and "structured" in data
        and data.get("_v") == _TAILOR_BLOB_VERSION
    ):
        return data
    return None


async def _generate_tailor_blob(db, uid, row, progress=None):
    """Produce a tailor blob (structured resume + per-bullet {m,n} variants) for
    (uid, row): fast variant-rewrite over the stored structure, else a full-regen
    fallback wrapped into the same blob shape. Returns blob dict or None."""
    structured = await asyncio.to_thread(resume_utils.get_structured, db, uid)
    if not structured:
        # Self-heal: build + persist the structure now so future tailors are fast.
        text = await asyncio.to_thread(resume_utils.get_resume_text, db, uid)
        if text:
            structured = await asyncio.to_thread(
                resume_utils.parse_structured, text, None
            )
            if structured:
                await asyncio.to_thread(
                    resume_utils.store_structured, db, uid, structured
                )
    if structured:
        blob = await _tailor_build(structured, row, progress=progress)
        if blob:
            return blob

    # Fallback: full-resume regeneration → wrap into a blob (no {n} variants, so
    # blanks fall back to the metric-phrase-stripped text).
    source = await _resume_source(db, uid)
    if not source:
        return None
    prompt = _TAILOR_PROMPT.format(posting=_job_context(row))
    kind, payload = source
    if kind == "text":
        full = f"{prompt}\n\n<resume>\n{payload}\n</resume>"
        answer = await asyncio.to_thread(
            gemma_client.ask_text, full, gemma_client.FAST_CHAIN
        )
    else:
        answer = await asyncio.to_thread(
            gemma_client.ask_with_image, payload, prompt, gemma_client.FAST_CHAIN
        )
    yaml_text = _strip_yaml_fence(answer) if answer else None
    if not yaml_text:
        return None
    return _blob_from_regen_yaml(yaml_text, structured)


def _blob_from_regen_yaml(yaml_text, structured):
    """Wrap a full-regen YAML into a tailor blob. Preserves stored education/
    skills/contact/name, and turns each experience/project bullet into an {m, n}
    variant (n derived by stripping the metric phrase)."""
    import yaml as _yaml

    try:
        data = _yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            return None
    except Exception:
        return None

    if structured:
        src = _apply_edu_extras(structured)
        for key in ("name", "contact", "education", "skills"):
            if key in src:
                data[key] = src[key]

    bullets = []
    for section in ("experience", "projects"):
        for i, item in enumerate(data.get(section) or []):
            desc = item.get("description") if isinstance(item, dict) else None
            if not isinstance(desc, list):
                continue
            for j, b in enumerate(desc):
                b = str(b).strip()
                if b:
                    bullets.append(
                        {"loc": [section, i, j], "m": b, "n": _strip_metric_phrase(b)}
                    )
    return {"structured": data, "bullets": bullets}


async def pretailor_job(db, uid, table, row_id):
    """Background pre-tailor: build + cache the tailor blob for (uid, job) so a
    later click is instant. Skips if already cached. Best-effort, returns bool."""
    if _load_blob(await asyncio.to_thread(db.get_cached_tailor, uid, table, row_id)):
        return True
    row = await asyncio.to_thread(_fetch_row, db, table, row_id)
    if not row:
        return False
    blob = await _generate_tailor_blob(db, uid, row)
    if not blob:
        return False
    await asyncio.to_thread(
        db.set_cached_tailor, uid, table, row_id, _dump_blob(blob)
    )
    return True


async def _finish_tailor(row, blob):
    """Build the TailorView + a VIEWABLE preview embed from a tailor blob — no
    PDF is compiled yet. The user reads the tailored bullets in the embed and
    only builds the PDF when they press Download. Returns (embed, None, view, None).
    """
    company = row.get("company_name") or "role"
    view = TailorView(blob, company)
    embed = view.preview_embed()
    return embed, None, view, None
