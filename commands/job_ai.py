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

from commands import gemma_client, resume_utils, score_wheel
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
_SCORE_PROMPT = """<role>
You are a hiring manager with 20 years of experience in the tech industry and \
a deep working knowledge of how Applicant Tracking Systems (ATS) rank resumes. \
You have personally screened tens of thousands of resumes and know exactly what \
separates a callback from a rejection. Score this resume against the job the way \
you would when deciding whether to bring the candidate in for an interview.
</role>

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

<output_format>
Return ONLY this JSON object, no prose. Use ONLY flat string arrays exactly as \
shown — do not nest objects inside the arrays:
{{
  "score": <integer 0-100>,
  "tier": "<Excellent|Strong|Moderate|Weak>",
  "summary": "<one sentence explaining the score>",
  "technical_skills": <integer 0-100>,
  "experience": <integer 0-100>,
  "domain_fit": <integer 0-100>,
  "highest_reason": "<one sentence on the strongest subscore>",
  "lowest_reason": "<one sentence on the weakest subscore>",
  "why_not_higher": "<1-2 sentences: primary highest-impact gaps capping the score>",
  "strengths": ["<skill> — <why it matters> (<where in resume>)"],
  "gaps": ["<highest-impact missing requirement>"],
  "quick_wins": ["<realistic improvement>"]
}}
Caps: strengths<=3, gaps<=4, quick_wins<=4. Keep each item short.
</output_format>"""


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


def _subscore_block(data):
    """Render the three subscores as aligned bars with band dots. Returns None
    if no subscore is present."""
    vals = []
    for key, label in _SUBSCORES:
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


def _reason_block(data):
    """Highest/lowest one-liners under the breakdown."""
    hi = str(data.get("highest_reason") or "").strip()
    lo = str(data.get("lowest_reason") or "").strip()
    lines = []
    if hi:
        lines.append(f"▲ **Strongest** — {hi}")
    if lo:
        lines.append(f"▼ **Weakest** — {lo}")
    return "\n".join(lines) or None


def _build_score_embed(row, data):
    """Assemble a modern, scannable Score embed: brand header, headline
    match+tier, subscore breakdown, then strengths / gaps / quick wins."""
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
    # Brand line up top: company + logo.
    embed.set_author(name=f"{company} · Match Report", icon_url=logo or None)

    # Headline: big percent + tier badge, then the one-line summary. A thin
    # rule separates the headline from the breakdown for a cleaner read.
    headline = ""
    if score is not None:
        headline = f"## {_tier_emoji(tier, score)} {score}%  ·  {tier}".rstrip(" ·")
    summary = str(data.get("summary") or "").strip()
    desc_parts = [p for p in (headline, summary) if p]
    if desc_parts:
        embed.description = ("\n".join(desc_parts) + "\n―――")[:4096]

    sub = _subscore_block(data)
    if sub:
        embed.add_field(name="📊 Score Breakdown", value=sub[:1024], inline=False)
    reasons = _reason_block(data)
    if reasons:
        embed.add_field(name="​", value=reasons[:1024], inline=False)

    why_not = str(data.get("why_not_higher") or "").strip()
    if why_not:
        embed.add_field(name="💡 Why Not Higher?", value=why_not[:1024], inline=False)

    strengths = _bullets(data.get("strengths"), limit=3)
    if strengths:
        embed.add_field(name="✅ Key Strengths", value=strengths[:1024], inline=False)

    gaps = _bullets(data.get("gaps"), limit=4)
    if gaps:
        embed.add_field(name="⚠️ Critical Gaps", value=gaps[:1024], inline=False)

    wins = _bullets(data.get("quick_wins"), limit=4)
    if wins:
        embed.add_field(name="⚡ Quick Wins", value=wins[:1024], inline=False)

    location = row.get("job_location")
    foot = "AI-generated · verify before relying on it"
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
        return None, None, "That posting is no longer available."

    data = None
    if uid:
        data = await asyncio.to_thread(db.get_cached_score, uid, table, row_id)

    if data is None:
        if not uid:
            return None, None, NEED_RESUME
        source = await _resume_source(db, uid)
        if not source:
            return None, None, NEED_RESUME

        prompt = _SCORE_PROMPT.format(posting=_job_context(row))
        # Cap is generous: Gemma's JSON mode can silently burn budget and return
        # empty at a tight cap (2000) yet completes cleanly at ~270 tokens with
        # 4000. Billing is on actual output, so the headroom is free.
        data = await _ask_json_resume(source, prompt, 4000)
        if not data:
            return None, None, "The AI couldn't score the match right now — try again later."

        await asyncio.to_thread(db.set_cached_score, uid, table, row_id, data)

    embed, score = _build_score_embed(row, data)

    file = None
    if score is not None:
        try:
            png = await asyncio.to_thread(score_wheel.render, score)
            file = discord.File(io_bytes(png), filename="score.png")
            embed.set_thumbnail(url="attachment://score.png")
        except Exception:
            log.exception("Failed rendering score wheel")

    return embed, file, None


# Tailor outputs the resume as YAML in the husayni/resume_builder schema — a
# downstream builder renders it to PDF. The model REWORDS the candidate's real
# content toward the posting; it must never invent anything (see <truth_rules>).
_TAILOR_PROMPT = """<role>
You are a hiring manager with 20 years of experience in the tech industry who \
has also built and tuned the Applicant Tracking Systems (ATS) that screen \
resumes before a human sees them. You know exactly which keywords, phrasings, \
and section structures let a resume pass ATS keyword and relevance checks while \
still impressing the human reviewer.
</role>

<posting>
{posting}
</posting>

<task>
Rewrite the candidate's resume tailored to this posting so it passes the ATS. \
Reorder and emphasize the most relevant real items first, and reword existing \
bullets to mirror the posting's language and include the posting's keywords \
ONLY where they truthfully describe work the candidate already did.

Write every experience and project bullet using Google's XYZ formula: \
"Accomplished [X] as measured by [Y], by doing [Z]" — i.e. lead with the \
accomplishment/impact [X], quantify it with a metric [Y], and state how it was \
done with the tools/methods [Z]. Start each bullet with a strong past-tense \
action verb. BUT the metric [Y] must be REAL: if the original resume does not \
state a number for that bullet, do NOT invent one — write the [Y] slot as the \
literal placeholder "[ADD METRIC]" for the candidate to fill in. Never fabricate \
a figure to complete the formula.
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
    degree: <degree>
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
achievements:
  - <real achievement>
publications:
  - <real publication>
certifications:
  - name: <cert>
    link: <url or empty>
</output_format>"""


def _strip_yaml_fence(text):
    """Remove any ```yaml ... ``` fence the model wraps the YAML in."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


async def run_tailor(db, user, table, row_id):
    """Tailor the resume for this posting as YAML (for the downstream resume
    builder). Returns (embed, file, error) — file is the .yaml attachment.
    Uses the stored resume text when available (text-only, faster)."""
    row, uid = await asyncio.gather(
        asyncio.to_thread(_fetch_row, db, table, row_id),
        asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        ),
    )
    if not row:
        return None, None, "That posting is no longer available."
    if not uid:
        return None, None, NEED_RESUME
    source = await _resume_source(db, uid)
    if not source:
        return None, None, NEED_RESUME

    prompt = _TAILOR_PROMPT.format(posting=_job_context(row))
    kind, payload = source
    if kind == "text":
        full = f"{prompt}\n\n<resume>\n{payload}\n</resume>"
        answer = await asyncio.to_thread(gemma_client.ask_text, full)
    else:
        answer = await asyncio.to_thread(
            gemma_client.ask_with_image, payload, prompt
        )
    if not answer:
        return None, None, "The AI couldn't tailor your resume right now — try again later."

    yaml_text = _strip_yaml_fence(answer)
    if not yaml_text:
        return None, None, "The AI couldn't tailor your resume right now — try again later."

    company = row.get("company_name") or "role"
    n_placeholders = yaml_text.count("[ADD METRIC]")

    # Try to compile a PDF via the build service; fall back to the YAML file.
    pdf = await asyncio.to_thread(_build_pdf_sync, yaml_text)

    safe_company = re.sub(r"[^A-Za-z0-9_-]+", "_", company).strip("_") or "role"
    if pdf:
        primary = discord.File(io_bytes(pdf), filename=f"resume_{safe_company}.pdf")
        built_line = "Your tailored resume as a ready-to-send **PDF**"
    else:
        primary = discord.File(
            io_bytes(yaml_text.encode("utf-8")),
            filename=f"tailored_resume_{safe_company}.yaml",
        )
        built_line = "Your tailored resume as **YAML** for the resume builder"

    desc = (
        f"{built_line}, reworded for this posting and ATS-optimized. "
        "**Only your real content was used — nothing was invented.**\n"
        "*AI-generated from your uploaded resume — review before using.*"
    )
    if n_placeholders:
        desc += (
            f"\n\n⚠️ **{n_placeholders}× `[ADD METRIC]`** — the AI never makes up "
            "numbers. Replace each placeholder with a real metric you can back up."
        )
    embed = discord.Embed(
        title=f"✍️ Tailored resume — {company}"[:256],
        description=desc,
        color=discord.Color.blurple(),
    )
    return embed, primary, None
