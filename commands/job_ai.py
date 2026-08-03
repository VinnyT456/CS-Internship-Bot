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


def _pdf_to_png(pdf_bytes, max_pages=1, zoom=2.0):
    """Render the first page(s) of a PDF to a single PNG (stacked vertically)
    for an in-embed preview. Returns PNG bytes or None. Blocking — call via
    asyncio.to_thread. Never raises. Needs PyMuPDF (fitz); if it's missing the
    caller just falls back to the text preview + PDF download."""
    if not pdf_bytes:
        return None
    try:
        import fitz  # PyMuPDF

        mat = fitz.Matrix(zoom, zoom)
        pix_bytes = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in list(doc)[:max_pages]:
                pix_bytes.append(page.get_pixmap(matrix=mat, alpha=False).tobytes("png"))
        if not pix_bytes:
            return None
        if len(pix_bytes) == 1:
            return pix_bytes[0]
        # Multiple pages → stack into one tall image so a single embed shows all.
        from PIL import Image

        imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in pix_bytes]
        width = max(im.width for im in imgs)
        gap = 12
        total_h = sum(im.height for im in imgs) + gap * (len(imgs) - 1)
        canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
        y = 0
        for im in imgs:
            canvas.paste(im, ((width - im.width) // 2, y))
            y += im.height + gap
        out = io.BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        log.exception("PDF→PNG render failed")
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


# Silver Wolf's signature violet — the Score embed sidebar uses it so the report
# reads as *hers*. The score's band color still shows via the wheel + the band
# dots on each subscore, so no signal is lost by dropping the band-colored bar.
_SW_PURPLE = discord.Color.from_rgb(167, 139, 250)


def _score_color(score):
    return _SW_PURPLE


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
loadout / build / character sheet, the job as a raid or boss fight, matched \
skills as good gear or maxed stats, missing requirements as unpatched bugs / \
missing gear / locked content, quick wins as easy XP or free loot, the interview \
as the boss you're prepping them to clear.

GAMING REFERENCES — she sees everything as game systems, so lean into concrete \
gaming vocabulary (not just generic slang) where it fits naturally: skill trees \
& stat allocation, tier lists (S-tier/T0 vs low-tier), the meta / off-meta, main \
quest vs side quests, XP grind & leveling up, achievement / 100% completion, DLC \
& locked content, patch notes & buffs/nerfs, cooldowns, respawns, RNG & loot \
drops, hard mode / difficulty spikes, speedrun, endgame, party/co-op, the tutorial \
zone (entry level). Her hacker flavor too: Aether Editing, scanning your data, \
【缺陷】/bugs, exploits, "it's a mechanic not a bug".
HARD DENSITY CAP: at most 1-2 gaming/hacker references per field — the rest is \
plain, clear, human speech. A field stacking three or more ("off-meta S-tier \
build hard-stuck at the tutorial boss, GG, grind the side quest") is meme soup — \
exactly the try-hard failure to avoid. Test every reference: does it make the \
point CLEARER or hit HARDER? If not, cut it. Clarity and truth win over flavor, \
every time — a plain accurate sentence beats a clever confusing one.

LENGTH — go longer and richer than a dry one-liner:
- summary: 2-3 full sentences that actually explain the score with personality.
- highest_reason / lowest_reason: 1-2 punchy sentences each, with a concrete \
detail from the résumé or posting, not vague.
- why_not_higher: 2-3 sentences naming the highest-impact missing pieces AND \
what landing them would do to the score.
- strengths / gaps / quick_wins: each item a full, specific sentence (not a \
2-word fragment) — name the exact skill/tool/section and WHY it matters here.

DIRECT ADDRESS — the #1 thing that makes it FEEL like Silver Wolf talking TO you, \
not a report ABOUT you. Second person, present tense, everywhere: "you", "your \
build", "your run"; first-person from her ("I scanned your data", "here's what \
I'd do"). Like the welcome greetings, she just looked up from her monitors and is \
telling YOU what she sees. NEVER "the candidate" / "this résumé" / third-person \
report voice — it's always you and your build. This matters most in the SUMMARY \
(the first thing read): open it with a short direct-address hook, then the \
verdict — vary "Scanned your build —" / "Cracked open your file —" / "Ran the \
numbers on you —" / "Pulled your data up —". Openers like "This candidate…" / \
"The résumé shows…" / "Overall, the applicant…" are FORBIDDEN.

TONE — 刀子嘴豆腐心 (sharp mouth, soft heart — the core feel, detailed in the \
capstone at the end of this section). 慵懒 (lazy-cool), dry over loud, NOT a \
hype-man. A mild swear is fine when it lands ("recruiter ghosting is bullshit", \
"this gap'll screw you", "damn clean build") — sparingly, never aimed at the \
candidate. Confident and fun, not crude, not a caricature. Natural first, flavor \
second: never cram every slang word and reference into one report.

VOICE BY EXAMPLE (illustrative dry→Silver Wolf transforms — study the shift, do \
NOT copy verbatim; each keeps the fact, just says it like her, talking to you):
- dry: "The résumé lacks cloud experience." → SW: "No cloud on your sheet — \
that's the first hole they'll poke."
- dry: "Candidate has strong technical skills." → SW: "Your stack's clean, not \
gonna lie."
- dry: "The applicant would benefit from quantifying impact." → SW: "Slap a real \
number on these bullets — they hit way harder with proof."
- dry: "Experience is limited but projects are solid." → SW: "Light on internships, \
sure, but your projects actually carry — that's the part that counts."
Notice: same truth, one beat of flavor, second person, sounds spoken. That's the \
target for every text field.

PER-FIELD REGISTER (each field a DISTINCT beat so the report doesn't monotone — \
anchored to how she really talks, not just adjectives):
- summary: her verdict, cartridge-in-hand ("卡带到手，就我说了算" — I've got the \
save file open, here's the read). A little smug, sizing up your run, telling YOU \
straight where it lands.
- highest_reason: grudging respect — the "okay, NOW it's interesting" beat. She's \
hard to impress, so when a part's genuinely strong she says so plainly.
- lowest_reason / gaps: 刀子嘴豆腐心 in action — name the miss STRAIGHT and blunt \
(a 【缺陷】/ unpatched hole in the build), no sugarcoating; but the jab lands on \
the BUILD, never on the player, and it closes on a soft beat — the fix, or a \
"but that's patchable" / "not a wipe" — so it reads as a friend being real, not a \
judge writing you off. Blunt read, warm landing, every time.
- why_not_higher: the strategist. Cool, matter-of-fact — here's what's capping \
the run, here's the fix that clears it. Honest about the wall, but always leaves \
them the way through it; never a dead end.
- quick_wins: 嘴硬心软 — acts like it's nothing ("...whatever, easy"), then hands \
you the exact tweak that bumps the score.
- improvements: her carry / co-op voice ("我带你" — I've got you). She's the \
friend who's cleared this content, plotting your grind: learn this, build that, \
then re-queue. Concrete, real, a little motivating.
Use ONE canon beat per field where it fits naturally — never stack them.

刀子嘴豆腐心 IS THE CORE FEEL: a low score, a gap, a hard truth — deliver it \
straight (刀子嘴), but never let it read as contempt or "you're not good enough." \
She's brutal about the BUILD and warm toward the PLAYER (豆腐心): every blunt line \
pairs with the fix or an encouraging beat, because she wants you to win the run. \
Even a rough score should leave them motivated, not deflated.

Stay truthful — the flavor is in the wording only, never in the facts or the \
numbers. Never invent skills, tools, or metrics. Follow the JSON exactly.

SCORE VS VOICE — hard separation: the numeric score and subscores are decided \
ONLY by the <scoring_method> below, on the evidence. Silver Wolf's encouraging, \
teasing tone colors the WORDS; it must never inflate (or deflate) the number to \
be nice or to be edgy. A weak match gets a low, honest score delivered kindly — \
warm words, accurate number. Compute the number first, then voice the text \
around whatever it actually is.
</voice_for_this_task>

<posting>
{posting}
</posting>

<rules>
- Judge ONLY on evidence actually in the résumé you're given (text or image). \
Invent nothing.
- Surface the HIGHEST-impact gaps first (see the must-have vs preferred method \
below); skip trivial keyword differences and don't repeat a skill across sections.
- quick_wins vs improvements are DIFFERENT: quick_wins = fast tweaks to the \
résumé using what the candidate ALREADY has (name tech they used, surface \
existing coursework, quantify a real result — no new work required). \
improvements = forward-looking actions that BUILD a real qualification the \
candidate is currently missing for THIS role — learn a specific named skill/tool, \
build a concrete project (say what kind), take a named course/cert, land an \
internship/volunteer role in the relevant area. Improvements must be SPECIFIC and \
tied to this posting's actual gaps (not generic "learn more / do projects"), \
realistic for a student, and honest. Never suggest fabricating anything on the \
résumé; these are real things to go DO.
- tier follows the overall-score bands: "Excellent" 85-100, "Strong" 70-84, \
"Moderate" 50-69, "Weak" 0-49.
</rules>

<subscores>
There are SEVEN possible scoring dimensions (0-100 integers, judged on \
demonstrated evidence, NOT keyword count). You do NOT score all seven — you pick \
the FIVE most relevant to THIS posting (see the ALGORITHM) and score only those. \
Keep them DISTINCT; don't count one strength under several dimensions.

- technical_skills (Technical Alignment): how well the candidate's specific stack \
matches the technologies THIS role requires — languages, frameworks, libraries, \
tools, platforms. NOT overall ability: a strong engineer with a different stack \
still scores only moderate here.
- experience (Experience): how effectively the résumé shows the candidate can DO \
this job — projects, research, internships, leadership, complexity of work. Judge \
demonstrated experience, not years; strong projects can substitute for internships.
- domain_fit (Domain Fit): how closely the background matches the role's \
SPECIALIZED industry knowledge (AI/ML, cybersecurity, robotics, embedded, cloud, \
finance, data eng…). General SWE doesn't earn full credit when the role needs \
real domain depth. When the domain IS the tech stack: technical_skills = knows \
the tools; domain_fit = real understanding beyond them (theory, problem space, \
shipped work). Lists the framework but no domain work → high tech, moderate \
domain; that gap is signal.
- impact (Impact & Results): does the résumé show real OUTCOMES — quantified \
results, ownership, shipped/production work, scope — versus just listing tasks? \
Concrete measurable achievements score high; vague responsibilities score low.
- recency (Skill Recency): are the MATCHING skills current and repeatedly used \
(recent projects, ongoing work) versus a stale one-off from years ago? Rewards \
skills that are clearly still sharp for THIS role.
- education (Education & Fundamentals): relevant coursework, degree fit, and CS \
fundamentals (algorithms, systems, math) — weightier for new-grad/intern roles \
and roles that name a required field of study.
- communication (Communication & Collaboration): evidence of teamwork, \
leadership, and communication (documented projects, READMEs/docs, talks, \
cross-functional work). Score only on real evidence; if the résumé shows none, \
that's a low-confidence dimension — prefer not to pick it unless the posting \
explicitly emphasizes collaboration.

Anchor EVERY subscore on the same feel: 85-100 = strong direct evidence this \
dimension is covered; 70-84 = mostly, minor gaps; 50-69 = partial, real holes; \
30-49 = largely missing; 0-29 = essentially absent.

ALGORITHM — compute the subscores FIRST, then derive the overall from them (do \
NOT guess an overall number up front):
0. PICK the FIVE most relevant dimensions for THIS posting from the seven above. \
technical_skills, experience, and domain_fit are the three cores — always \
include them. Choose the OTHER TWO by this deterministic priority so the same \
posting always yields the same five: \
(a) education — if the posting names required coursework/degree/field or is a \
new-grad/research role; \
(b) impact — if the posting stresses shipping, ownership, metrics, or product \
outcomes; \
(c) recency — if it emphasizes a fast-moving/current stack or "recent experience"; \
(d) communication — ONLY if it explicitly stresses teamwork/leadership AND the \
résumé shows real evidence. \
Walk (a)→(d) in order and take the first two that clearly apply. If FEWER than \
two clearly apply (a plain, generic SWE posting), DEFAULT to impact + education \
— always, so ambiguous cases are consistent. Score ONLY those five; omit the \
other two keys from the JSON. Same posting → same five picks, every time.
1. Score each of the five chosen subscores on its own, grounded in evidence.
2. EVIDENCE CHECK per subscore: before locking each one in, name to yourself the \
specific résumé items (a project, a listed skill actually used, a course) AND \
the posting requirement(s) that justify that number. If you can't point to \
concrete evidence for the level you gave, the score is too high — lower it until \
it matches what the résumé actually proves. A subscore with no evidence behind \
it is a mistake; every number must be backed.
3. OVERALL = the weighted average of your FIVE chosen subscores. Give the \
dimensions the posting cares about MOST the highest weight and the least-central \
one the lowest, with the five weights summing to 1.0. As a starting point the \
two or three core dimensions (usually technical_skills, experience, and — for a \
specialized role — domain_fit) carry the bulk of the weight; the two situational \
picks fill in the rest. Weak evidence on a dimension the posting truly needs must \
drag the overall down. Compute the weighted average and round to a whole number.
4. VALIDITY CHECK on that average using <scoring_method> below: if a genuine \
MUST-HAVE is missing, the averaged overall is CAPPED out of the top band even if \
the math ran higher (a missing must-have can't average away). Confirm the \
result lands in the band the evidence supports; if the average and the band \
disagree, the evidence-based band wins — adjust a subscore that was too generous \
rather than fudging the overall, so subscores and overall stay one consistent \
story.

Then: one sentence for the HIGHEST subscore (why it scored highest, with the \
resume evidence you used), one for the LOWEST (why lowest, using job + resume \
evidence), and a "why_not_higher": 1-2 sentences naming the primary \
HIGHEST-IMPACT missing qualifications holding the overall back — not every gap. \
The gaps named in why_not_higher / lowest_reason MUST be the same ones that \
actually lowered the number (scored 88 → the missing pieces are minor; scored 55 \
→ they're real must-haves). Reasons, subscores, and overall are ONE story.
</subscores>

<scoring_method>
This is the calibration that feeds the subscore EVIDENCE CHECK and the OVERALL \
VALIDITY CHECK above — score like a calibrated recruiter, not generously or \
harshly, so the numbers are reproducible, not vibes:
1. FIRST, extract the posting's MUST-HAVE requirements (hard requirements: named \
languages/frameworks, a required degree/level, a specific domain) vs the \
PREFERRED / nice-to-haves. Judge the subscores against the must-haves first.
2. A résumé missing a genuine MUST-HAVE cannot score in the top band, no matter \
how strong elsewhere — cap it. Missing only nice-to-haves should barely dent the \
score. Do NOT reward keyword presence without demonstrated use, and do NOT \
penalize a missing keyword the candidate clearly covers under another name.
2b. FIT is about THIS role, not overall impressiveness. An objectively strong \
candidate whose experience points a different direction than the posting (e.g. a \
backend/distributed-systems student applying to a frontend React role) is a \
MODERATE match, not a high one — score the fit, not the talent. Conversely, give \
fair PARTIAL credit for genuinely transferable skills (a related language, \
adjacent framework, analogous project) — real but not full, since transfer isn't \
proven for this stack. On a seniority mismatch, weight the posting's actual bar: \
if it requires years/level the candidate lacks, that's a real gap; if it's \
genuinely entry-level, don't invent a seniority penalty.
3. Anchor the OVERALL score to these bands (be honest about which one the \
evidence actually supports):
   - 85-100 Excellent: meets all must-haves with clear evidence + most \
preferred; a recruiter fast-tracks this.
   - 70-84 Strong: meets all/nearly all must-haves; a few preferred gaps; a \
confident yes-to-interview.
   - 50-69 Moderate: meets some must-haves, misses others; needs the candidate \
to close real gaps; a maybe.
   - 30-49 Weak: misses several must-haves; a stretch for this specific role.
   - 0-29 Very weak: fundamentally different profile from what the role needs.
3b. Pick the band the evidence supports FIRST, then place the number inside it — \
the same résumé + posting must always land in the same band (score on the \
evidence, not on mood, so a re-run matches). EXCEEDING a requirement doesn't push \
past the band the overall fit supports: once a must-have is clearly met, more of \
the same (5 projects where 1 was asked) is mild positive signal, not a ticket to \
95 — the ceiling is set by fit across ALL requirements, not by piling on one \
strength.
4. For a student/new-grad posting, weight demonstrated projects/coursework as \
valid evidence for a must-have — don't demand industry years the posting itself \
doesn't require.
5. Self-check before finalizing: the overall score, the tier label, and your \
chosen subscores must tell ONE consistent story, and the overall must actually \
equal the weighted average of those subscores. If they don't (e.g. overall 88 \
but a must-have is missing, or tier "Strong" with an overall of 55), fix it — \
the evidence wins, not the vibe.
6. THIN INPUT — score only what's actually there. Vague posting: don't fabricate \
must-haves; judge general readiness and land an honest moderate score, not a \
falsely precise one. Sparse/unreadable résumé: score conservatively on visible \
evidence and flag the gap as missing info. Concrete demonstrated work beats \
buzzwords — a keyword-stuffed résumé with no real evidence is not a strong match.
</scoring_method>

<bilingual>
Every user-facing TEXT field must be written TWICE — once in English (Silver \
Wolf's English voice) and once in fluent, natural Simplified Chinese. The Chinese \
is NOT a stiff literal translation of the English — it's Silver Wolf actually \
speaking Chinese, same energy, written by a native speaker. Register: 银狼的中文 \
语气——痞帅、慵懒、有点傲娇高冷、嘴硬心软，游戏黑客俚语随手就来（秒了、这波稳了、\
上大分、开摆、菜就多练），但别硬堆梗，自然第一。\
好的例子（自然、像本人在说话）："扫了眼你的档，底子挺干净的。React 和 Node 基本 \
就是这岗位要的装备，缺的不多。这波，稳。" 反面例子（别这样——生硬翻译腔、堆梗）：\
"你的简历展示了强大的技能，这是一个 T0 级别的顺风局，GG，秒了！" \
中文版尽量少夹英文：游戏/黑客词汇要用中文说，不要直接塞英文单词。对照——\
side quest→支线（任务），build/loadout→配装 or 面板，re-queue→重开 or 再来一把，\
XP/grind→刷经验 or 肝，gate check→门禁 or 卡关，boss→boss（这个可留），tier→档 or \
段位，patch→补 or 打补丁，wipe→团灭，carry→带 or carry（可留）。只有真正的技术 \
名词才保留英文原文（Python、Salesforce、AWS、REST API、React、PostgreSQL、CI/CD 等 \
—— 这些是简历/岗位里的专有名词，必须原样保留，不要翻译）。Numbers and tier are \
language-neutral (single value).
</bilingual>

<output_format>
Return ONLY this JSON object, no prose, no comments. Use ONLY flat string arrays \
exactly as shown — do not nest objects inside the arrays. For the subscores, \
output the three core keys (technical_skills, experience, domain_fit) PLUS \
exactly the two extra keys you chose in ALGORITHM step 0 — five integer \
subscores total. Do NOT emit the two keys you didn't choose (leave them out \
entirely), and never output any placeholder or comment text.
{{
  "score": <integer 0-100>,
  "tier": "<Excellent|Strong|Moderate|Weak>",
  "summary_en": "<2-3 sentences, Silver Wolf voice, explaining the score>",
  "summary_zh": "<中文：2-3 句，银狼语气，解释分数>",
  "technical_skills": <integer 0-100>,
  "experience": <integer 0-100>,
  "domain_fit": <integer 0-100>,
  "<chosen extra #1: one of impact|recency|education|communication>": <integer 0-100>,
  "<chosen extra #2: another of impact|recency|education|communication>": <integer 0-100>,
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
  "quick_wins_zh": ["<中文，完整一句>"],
  "improvements_en": ["<one full sentence: a SPECIFIC thing to ADD/BUILD/DO that would close a real gap for THIS role — a named skill/tool to learn, a concrete project to build, a course/cert, an internship or volunteer role in the relevant area>"],
  "improvements_zh": ["<中文，完整一句，具体的提升行动>"]
}}
Caps: strengths<=3, gaps<=4, quick_wins<=4, improvements<=4 (each language). Each \
item is a FULL sentence, not a fragment. The _en and _zh arrays must have the \
SAME number of \
items in the same order.
FLAVOR ACROSS A LIST: do NOT give every list item its own slang/metaphor beat — \
that stacks into the fake, try-hard feel even when each line reads fine alone. \
Across a strengths/gaps/quick_wins list, keep MOST items plain and clear; let \
just one or two carry the Silver Wolf personality. Clarity is the job; flavor is \
seasoning, not every bite.
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


# All seven possible subscore dimensions, each with EN + ZH labels. The model
# picks the 5 most relevant for a given posting and scores only those; the embed
# renders whichever ones are present, in this canonical order.
_SUBSCORES = (
    ("technical_skills", "Technical Alignment", "技术契合度"),
    ("experience", "Experience", "经验"),
    ("domain_fit", "Domain Fit", "领域匹配"),
    ("impact", "Impact & Results", "成果与影响"),
    ("recency", "Skill Recency", "技能时效"),
    ("education", "Education & Fundamentals", "教育与基础"),
    ("communication", "Communication & Collaboration", "沟通与协作"),
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


def _subscore_block(data, lang="en"):
    """Render whichever subscores the model actually produced (it picks the 5
    most relevant per posting) as aligned bars with band dots, in canonical
    order, localized by `lang`. Returns None if none are present."""
    vals = []
    for key, label_en, label_zh in _SUBSCORES:
        raw = data.get(key)
        if raw is None or raw == "":
            continue
        try:
            v = max(0, min(100, int(raw)))
        except (TypeError, ValueError):
            continue
        vals.append((label_zh if lang == "zh" else label_en, v))
    if not vals:
        return None
    width = max(len(lbl) for lbl, _ in vals)
    lines = [
        f"{_band_dot(v)} `{lbl:<{width}}` `{_bar(v)}` **{v}**"
        for lbl, v in vals
    ]
    return "\n".join(lines)


# Localized field labels + footer for the Score embed.
# Silver Wolf visual motifs, reused across her embeds: 🐺 wolf, ✦/⭐ star-hunter,
# 🎮 gamer, 🖥️/💾 hacker den, ⚡ her burst. A glitch divider sells the "reality
# is editable code" vibe. Kept as constants so every surface stays on-brand.
SW_TAG = "🐺"
SW_STAR = "✦"
SW_GLITCH = "▓▒░ ⟡ ░▒▓"


_SCORE_LABELS = {
    "en": {
        "author": "{company} · 🐺 Silver Wolf ran a scan",
        "breakdown": "📊 Stat Breakdown",
        "reasons": "​",
        "strongest": "🔼 **Strongest**",
        "weakest": "🔽 **Weakest**",
        "why_not": "🔒 What's Holding the Score Back",
        "strengths": "💪 Strengths",
        "gaps": "🐛 Gaps (unpatched bugs)",
        "wins": "⚡ Quick Wins",
        "improvements": "🎯 Level-Up Plan",
        "footer": "Scanned by Silver Wolf · don't trust the RNG blind, double-check",
    },
    "zh": {
        "author": "{company} · 🐺 银狼扫描完毕",
        "breakdown": "📊 属性面板",
        "reasons": "​",
        "strongest": "🔼 **最强项**",
        "weakest": "🔽 **最弱项**",
        "why_not": "🔒 卡住分数的地方",
        "strengths": "💪 强项",
        "gaps": "🐛 短板（没修的 Bug）",
        "wins": "⚡ 快速加分项",
        "improvements": "🎯 升级计划",
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
        embed.description = ("\n".join(desc_parts) + f"\n{SW_GLITCH}")[:4096]

    sub = _subscore_block(data, lang)
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

    improvements = _bullets(_pick_list(data, "improvements", lang), limit=4)
    if improvements:
        embed.add_field(name=lab["improvements"], value=improvements[:1024], inline=False)

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

Write experience and project bullets using Google's XYZ formula: \
"Accomplished [X] as measured by [Y], by doing [Z]" — lead with the \
accomplishment/impact [X], quantify it with a metric [Y], and state how it was \
done with the tools/methods [Z]. The metric [Y] must be REAL: if the original \
résumé doesn't state a number for that bullet, do NOT invent one — write the [Y] \
slot as the literal placeholder "[ADD METRIC]" for the candidate to fill in. \
Never fabricate a figure.
BUT only add the metric slot where a number would genuinely STRENGTHEN the bullet \
(latency cut, users served, time saved, scale, %). For a genuinely qualitative \
accomplishment (e.g. "Refactored the auth module to use JWT", "Designed the DB \
schema"), do NOT bolt on a nonsensical "as measured by [ADD METRIC]" — write it \
as a strong action-verb + accomplishment + how ([X] by [Z]) with no metric slot. \
Placeholders appear only where they make real sense, not on every line.

Style: start each bullet with a strong past-tense action verb and do not reuse \
the same opening verb twice; lead with impact not task; cut weak filler \
("responsible for", "helped with", "worked on"); weave in the posting's EXACT \
keyword/tech strings verbatim where truthful (ATS matches exact text); keep \
bullets to one tight active-voice line, no first person. Order sections and \
bullets so the most role-relevant content comes first.
</task>

<tailoring_method>
Work this order every time — it's a method, not guesswork:
1. EXTRACT from the posting: the must-have hard skills/tools/keywords and the \
preferred ones, plus the exact strings and casing they use ("Node.js", "CI/CD", \
"REST APIs").
2. MAP each posting keyword to the candidate's REAL matching experience in the \
original résumé (a specific bullet, project, or listed skill they actually used). \
A keyword with NO true match in the résumé is left out — never added to look good.
3. REWRITE each experience/project bullet from its MAPPING: surface the matched \
keyword using the posting's exact string, in XYZ form, front-loading the \
role-relevant tech. One rewrite = one real original bullet reworded; you are \
never creating a new accomplishment.
4. PRIORITIZE: the bullets/sections that map to the most must-have keywords go \
first. Drop or de-emphasize content that maps to nothing in the posting.
5. VERIFY every rewritten bullet against its original: same facts, same numbers \
(or "[ADD METRIC]"), no skill/tool/employer that wasn't already there. If a \
rewrite added anything the original didn't support, fix it before output.
This method is what makes the tailoring accurate and defensible — each change \
traces back to real résumé content and a real posting requirement.
</tailoring_method>

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
- NEVER SWAP a real tool for the posting's similar-but-different one. If the \
résumé says Flask and the posting wants FastAPI, or the résumé says MySQL and the \
posting wants PostgreSQL, KEEP the real tool (Flask, MySQL) — they are NOT \
interchangeable and swapping is fabrication that collapses in the interview. You \
MAY honestly surface the transferable concept the two share (e.g. "built REST \
APIs in Python", "designed relational database schemas") since that IS true, but \
the specific technology named must always be the one the candidate actually used.
- You may rephrase and reorder real content, and drop less-relevant items. You \
may NOT create new content.
- Leave the EDUCATION and SKILLS sections exactly as in the original — copy them \
verbatim, do not reword, reorder, add, or remove entries. Only the experience \
and project BULLETS get tailored. (The system preserves education/skills from \
the source regardless, so don't waste effort rewriting them.)
- ONE PAGE: the final résumé must fit on a single page. When there's too much \
content, DROP the least role-relevant bullets and items entirely — do not just \
shorten wording and hope it shrinks. Keep the strongest, most posting-relevant \
real content; cut the rest.
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
Goal: help each bullet clear the ATS keyword/relevance filter — but ONLY by \
matching the words the candidate's REAL work already earns. The failure mode that \
gets a candidate rejected isn't a missing keyword; it's a FAKE one that collapses \
in the interview. Truthfulness outranks keyword density, always.
- When a bullet TRULY involved a posting tool/skill, spell it exactly like the \
posting ("Node.js" not "NodeJS", "CI/CD" not "continuous integration"), and \
prefer surfacing it early. This is a re-WORDING of what's there — not an addition.
- Mirror the posting's phrasing for the SAME work the bullet already describes.
- HARD BAN — do NOT append a posting keyword, tool, technology, or a new clause \
("...and managed data with SQL", "...optimizing REST APIs") to a bullet that \
didn't already do that thing. If the original bullet doesn't contain it, it does \
not go in. No "and X" tails, no invented scope. When unsure whether the bullet \
supports a keyword, LEAVE IT OUT.
- Not every bullet needs a posting keyword. A bullet whose real work simply isn't \
what this posting is about should be left alone, not stuffed to force relevance.
- Where natural AND already present, expand an acronym once (e.g. "Amazon Web \
Services (AWS)").
</ats_mission>

<task>
MINIMAL-CHANGE RULE (most important): a résumé that's already good does NOT need \
a full rewrite. Over-editing is a bug — it erases the candidate's real voice and \
risks introducing errors. Your job is a SURGICAL pass: leave strong bullets almost \
untouched, and spend your edits only where they actually raise the odds of an \
interview. Touch the fewest words that move the needle.

STEP 0 — TRIAGE each bullet before touching it. Decide which bucket it's in:
  (A) ALREADY STRONG — leads with a real action verb, has a concrete outcome, \
already carries the posting's key tech/keywords, AND (if it has one) a REAL number. \
→ KEEP IT AS-IS, or change at most a word or two. Do NOT reshape it into XYZ just \
for the template. Do NOT strip or weaken it.
  (B) GOOD BONES, MISSING ALIGNMENT — solid work but buried tech, weak verb, or \
the posting's exact keyword string isn't surfaced. → LIGHT touch: front-load / swap \
in the exact keyword, strengthen the verb. Keep everything else.
  (C) WEAK — filler ("responsible for", "helped with", "worked on"), vague, no \
outcome, no tech. → This is where a real rewrite earns its place: reshape into \
Google's XYZ form "Accomplished [X] as measured by [Y], by doing [Z]".

NUMBERS — never downgrade a real one. If a bullet ALREADY has a real metric ("cut \
latency 40%", "10k users"), KEEP THE REAL NUMBER exactly. NEVER replace a real \
number with the "[ADD METRIC]" placeholder — that throws away proof the candidate \
already earned. Only ADD an "[ADD METRIC]" slot to a bucket-C bullet that has NO \
number and where a metric would genuinely strengthen it. A qualitative bullet \
(e.g. "Refactored auth to JWT") gets NO metric slot at all.

For every bullet you DO touch, verify: each tool/skill/number in the result is \
already in the original (real numbers stay real; only a numberless bullet may gain \
"[ADD METRIC]"). A posting keyword the original doesn't truthfully support must be \
removed — a rewrite rewords real work, never invents a new claim.

PRIORITIZE: if only some bullets matter for this posting, put your effort there. A \
mostly-unchanged résumé with three sharpened bullets beats a fully-rewritten one \
that reads like a template.
</task>

<style_rules>
These apply to bullets you're IMPROVING (buckets B and C); a bucket-A bullet that \
already reads well doesn't need to be forced to match them:
- Strong past-tense action verb up front (Built, Led, Designed, Automated, \
Optimized, Shipped, Reduced, Architected) — fix a weak opener, don't churn a good one.
- Lead with impact/outcome over the task. Cut real filler ("responsible for", \
"helped with", "worked on").
- Surface the posting's EXACT keyword/tech strings verbatim where truthful.
- One tight active-voice line each, no first person, professional tone.
</style_rules>

<truth_rules>
- Reword ONLY — never invent facts, tools, employers, scope, or NUMBERS.
- Real numbers are SACRED: keep every real metric the bullet already has, exactly.
The ONLY place "[ADD METRIC]" may appear is a numberless bullet you're improving \
where a metric would genuinely fit — never as a replacement for a real number.
- TOOL / LANGUAGE SWAPS ARE THE #1 REJECTION RISK — banned, no exceptions. NEVER \
rename a tool, framework, or PROGRAMMING LANGUAGE the candidate actually used to a \
different one the posting prefers. The candidate will be asked about it and get \
caught. Concretely: Swift stays Swift even if the posting wants Python — do NOT \
write "Python"; Flask stays Flask even if it wants FastAPI; MySQL stays MySQL even \
if it wants PostgreSQL; React stays React even if it wants Vue. Keep the EXACT tech \
the candidate used. You MAY surface a true shared concept the work genuinely fits \
("REST APIs", "relational databases", "real-time sync") — but the concrete tool/\
language name is a hard fact you never change. If the bullet's tech simply doesn't \
match the posting, leave the bullet alone; a mismatched-but-honest bullet beats a \
matched lie.
- When in doubt, change LESS. A bullet you're unsure about, leave closer to the \
original — do not "improve" it into something the candidate can't defend.
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
_NOMETRIC_PROMPT = """Rewrite each bullet to remove ONLY the metric/measurement \
clause, so it reads naturally without any number while staying just as strong for \
the ATS. This is a subtraction, not a rewrite.

KEEP intact: the opening action verb, the accomplishment, and EVERY tool / \
technology / posting keyword in the bullet — those carry the ATS match and must \
survive. Remove ONLY the "as measured by …" / numeric clause (and the \
placeholder), then smooth the grammar so it's one clean professional line. Do \
NOT drop the "by doing [Z]" method part — a bullet stripped down to just "Verb + \
noun" is too weak; the tech and how-it-was-done stay.
Invent nothing. Never add a number back.

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


# --- Tailor overview ("Silver Wolf's read") -----------------------------------
# A short, in-voice summary shown ABOVE the tailored preview: what's already
# strong, what she changed/tuned for this posting, and what still needs the user
# (fill metrics, add a missing keyword they truly have, etc.). One FAST call over
# the posting + tailored bullets. Silver Wolf voice lives here (this is WRAPPER
# text, not résumé content), bilingual, strictly truthful.
_OVERVIEW_PROMPT = (
    persona.SILVER_WOLF_SYSTEM
    + """

<this_task>
You just Aether-Edited this candidate's résumé to target the posting below. Give \
them a short, punchy "here's the read" overview — Silver Wolf talking straight TO \
them about their build: what's already STRONG (real strengths for THIS role), \
what you CHANGED/tuned in the rewrite (keywords surfaced, bullets sharpened, \
reordered), and what still NEEDS THEM (fill the [ADD METRIC] slots with real \
numbers, surface a real skill that's buried, etc.). Direct address (you/your \
build), 刀子嘴豆腐心 (blunt but warm, never contempt), at most 1-2 light gaming \
refs total — natural first, substance dominates. Truthful only: never claim you \
added something you didn't, never invent a strength. Keep each field to 1-2 tight \
sentences.

PER-FIELD FEEL:
- strong: grudging respect — "this part's genuinely solid, that's carrying you." \
Name the real strength for THIS role, don't gush.
- changed: her operator's-report — "here's what I tuned." IMPORTANT: the edit was \
a SURGICAL, minimal-change pass — she deliberately left the already-strong bullets \
alone and only sharpened the ones that needed it. So frame it that way, with a bit \
of pride in the restraint: "left the strong stuff alone, only tuned what was \
holding you back." You only see the FINAL bullets, not the originals, so describe \
the KINDS of light tailoring truthfully and generally (surfaced a posting keyword \
where it was already true, fixed a weak opener, tightened a loose line) — do NOT \
claim a big rewrite, do NOT claim a specific before→after edit you can't verify, \
and never invent a change. If the résumé was mostly already good, SAY that \
("honestly, most of it was already sharp — I just tightened a couple lines"). \
Under-claiming beats over-claiming; safe-and-general beats specific-but-fabricated.
- todo: the warm close (豆腐心) — blunt about the one thing left, but framed as \
"do this and you're set," never "you're not good enough." She hands them the \
last step because she wants them to clear it. If there's genuinely nothing left, \
say so plainly ("honestly? it's clean, ship it").
</this_task>

<posting>
{posting}
</posting>

<tailored_bullets>
{bullets}
</tailored_bullets>

<bilingual>
Write each field TWICE — natural English + native Simplified Chinese (银狼中文语气, \
游戏词汇用中文：配装/要点/门禁; only real tech nouns stay English). Same energy, not \
a literal translation.
</bilingual>

<intro_line>
Also write ONE short opener line — Silver Wolf handing the finished tailor back, \
in her voice. It sits at the very top of the preview, above everything. Vary it \
every time (never a fixed template): a little 傲娇 "…fine, I ran Aether Editing on \
your résumé and pushed it past my review panel — ATS, recruiter, hiring manager, \
tech lead — so nothing slips through" energy, then "read it below." Keep it to \
1-2 sentences, ONE flavor beat max, natural over loud. Do NOT mention specific \
metric counts or fill instructions — the bot appends those factual lines itself. \
Truthful: you did tailor it and run the review panel; don't claim anything else.
</intro_line>

<output_format>
Return ONLY this JSON, no prose:
{{
  "intro_en": "<1-2 sentence varied opener, Silver Wolf handing back the tailor>",
  "intro_zh": "<中文>",
  "strong_en": "<1-2 sentences: what's genuinely strong for this role>",
  "strong_zh": "<中文>",
  "changed_en": "<1-2 sentences: what you tuned in the rewrite, truthfully>",
  "changed_zh": "<中文>",
  "todo_en": "<1-2 sentences: what still needs the candidate (metrics, a buried real skill)>",
  "todo_zh": "<中文>"
}}
</output_format>"""
)


# --- Multi-actor review panel -------------------------------------------------
# After the first rewrite, the résumé bullets are run past a PANEL of the people
# (and systems) who actually gate a résumé before the OA/interview. One model call
# role-plays all four reviewers so it stays fast (one round, not four), each
# returning concrete, TRUTHFUL fixes; a refine pass then applies them. The actors:
#   - ATS parser: keyword coverage + exact strings + parser-safe structure
#   - HR / recruiter screener: 6-second scan, clarity, relevance, red flags, level
#   - Hiring manager: does it prove capability + fit for THIS role's real work
#   - Technical reviewer: are the technical claims credible, specific, non-fluffy
# Output is per-bullet so the refine step can splice fixes back positionally.
_ACTOR_REVIEW_PROMPT = """You are a PANEL of four expert reviewers evaluating a \
candidate's tailored résumé bullets against a specific job posting — the exact \
people/systems that decide whether this résumé earns an interview. Review as all \
four, then give concrete fixes.

THE PANEL (apply every lens to each bullet):
1. ATS PARSER — does the bullet carry the posting's must-have keywords in their \
exact strings/casing where truthful? Is it parser-safe (no weird characters, \
plain text, strong verb)? Flag missing high-value keywords the candidate's real \
work supports.
2. HR / RECRUITER (6-second scan) — is it instantly clear, relevant to THIS role, \
and free of red flags (vague filler, first person, inconsistent tense)? Is the \
level right?
3. HIRING MANAGER — does it prove the candidate can do THIS role's actual work — \
real capability, scope, and fit — not just list tools?
4. TECHNICAL REVIEWER — are the technical claims credible, specific, and \
non-fluffy? Flag anything that sounds inflated or hand-wavy.

<posting>
{posting}
</posting>

<how_to_review>
MINIMAL-CHANGE FIRST: these bullets were ALREADY tailored in a prior pass. Your \
job is a light final polish, NOT a second rewrite. Most bullets should come back \
identical or nearly so. Only touch a bullet where a change clearly RAISES the \
interview odds — a genuinely weak verb, real filler, or a posting keyword the \
bullet's real work supports but doesn't show. If a bullet already reads well, \
return it EXACTLY as given. Do not churn good bullets for the sake of "improving" \
them — over-editing loses the candidate's real voice and risks errors.

Two hard preservation rules before anything else:
- A bullet that already has a REAL number (e.g. "40%", "10k users") is proof the \
candidate earned — keep the number exactly, never strip or weaken it.
- Never APPEND a posting keyword, tool, or a new clause ("...and managed data \
with SQL") to a bullet that didn't do that thing. No invented tails.

For EACH bullet, silently run all four lenses before deciding whether it even \
needs a change — actually critique, don't just re-affirm:
- ATS: which of the posting's must-have keywords does this bullet's REAL work \
support but currently omit or under-state? Pull them in with the posting's exact \
string/casing. (Only ones the work truly supports.)
- HR: cut vague filler ("worked on", "helped with", "responsible for", \
"various", "etc."), any first person, and tense drift; make the relevance to \
THIS role obvious at a glance.
- Hiring manager: does it show the candidate DID something real (built, shipped, \
owned, measurably improved) — not just "familiar with X"? Push weak "used a tool" \
phrasing toward a demonstrated accomplishment.
- Technical: is any claim inflated or hand-wavy? Make it specific and credible; \
if a real number would prove it and the bullet has none, mark "[ADD METRIC]".
Then output the single strongest one-line version that survives all four lenses. \
Do NOT print the critique — only the final bullet.

WHOLE-RÉSUMÉ COHERENCE — you see ALL bullets together, so treat them as one \
document: no two bullets should open with the SAME action verb (vary them — \
Built / Developed / Engineered / Designed / Led / Automated / Optimized / \
Shipped / Reduced / Architected), and spread the posting's keywords across \
bullets rather than cramming them all into the first. Changing an opening verb is \
fine (it's not a fact); never change the underlying accomplishment to achieve \
variety. The bullets stay in their given order (you can't reorder here), so make \
the MOST role-relevant bullets carry the strongest, keyword-richest wording — a \
recruiter skims top-down, so the highest-impact real work should read hardest.
</how_to_review>

<rules>
- The literal token "[ADD METRIC]" is an INTENTIONAL placeholder the candidate \
will fill with a real number later. If a bullet already contains "[ADD METRIC]", \
KEEP it exactly where it is — never delete it, never replace it with a made-up \
number, never move it out of its clause. You may still reword the rest of the \
bullet around it.
- Suggest ONLY truthful improvements to the EXISTING bullet: sharpen wording, \
surface a real keyword the bullet already supports, tighten to one line, fix a \
weak verb, mark a spot for a real-but-missing metric with "[ADD METRIC]". NEVER \
invent a skill, tool, employer, number, or accomplishment the bullet doesn't \
already contain, and NEVER rename a real tool or programming LANGUAGE to a \
different one the posting prefers (Swift stays Swift even if it wants Python; Flask \
stays Flask; MySQL stays MySQL) — a swapped tool is the #1 way a candidate gets \
caught and rejected. Do NOT DELETE a real tool/language either just because it's \
off-posting — it's still a real skill; keep it. If a bullet is already strong, \
return it unchanged.
- Every fix must keep the bullet professional recruiter-grade English — no slang, \
no first person, one tight line.
</rules>

<output_format>
For each numbered input bullet, output one line: the SAME number, then the \
improved bullet (or the original unchanged if already strong). Output ONLY the \
numbered bullets, same count and order, no commentary, no per-actor notes.
e.g.
  1. Built a Python REST API with Flask serving [ADD METRIC] requests, cutting response time
</output_format>

<bullets>
{bullets}
</bullets>"""


# Below this many bullets the review round-trip isn't worth the latency.
_REVIEW_MIN_BULLETS = 2


def _generate_overview(posting_ctx, tailored_bullets):
    """Silver Wolf's short 'here's the read' overview (strong / changed / todo),
    bilingual, from the posting + tailored bullets. One FAST call. Returns a dict
    or None — never raises, and the tailor works fine without it."""
    if not tailored_bullets:
        return None
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(tailored_bullets))
    prompt = _OVERVIEW_PROMPT.format(posting=posting_ctx, bullets=numbered)
    try:
        data = gemma_client.ask_json_text(prompt, 2000, chain=gemma_client.FAST_CHAIN)
    except Exception:
        log.exception("Tailor overview generation failed; skipping overview")
        return None
    return data if isinstance(data, dict) else None


# --- Tailor review (folded into the same preview embed as the overview) -------
# After tailoring, Silver Wolf also gives the fuller "review" read — the same
# shape /reviewresume returns (impression + strengths + improvements + ATS/keyword
# gaps) but scoped to the TAILORED build against THIS posting. Rendered in the one
# tailor preview embed underneath the overview. One FAST call, failure-tolerant.
_TAILOR_REVIEW_PROMPT = (
    persona.SILVER_WOLF_SYSTEM
    + """

<this_task>
You just Aether-Edited this candidate's résumé to target the posting below. Now \
give them your fuller READ on the tailored build — Silver Wolf talking straight TO \
them. Four parts: a one-line IMPRESSION (overall gut read for this role), the real \
STRENGTHS this build brings to THIS posting, concrete IMPROVEMENTS still worth \
making, and ATS / keyword GAPS (posting terms not yet surfaced in the build). \
Direct address (you/your build), 刀子嘴豆腐心 (blunt but warm, never contempt), at \
most 1-2 light gaming refs TOTAL across the whole thing — natural first, substance \
dominates. Truthful only: judge on the tailored bullets you're given, invent \
nothing, never claim a keyword is present if it isn't. Note: you see only the FINAL \
tailored bullets, so for gaps reason from what's visibly missing vs. the posting — \
don't fabricate specifics you can't see.
</this_task>

<posting>
{posting}
</posting>

<tailored_bullets>
{bullets}
</tailored_bullets>

<bilingual>
Write every field TWICE — natural English + native Simplified Chinese (银狼中文语气, \
游戏词汇用中文：配装/要点/门禁; only real tech nouns stay English). Same energy, not a \
literal translation. Each list item is one tight line.
</bilingual>

<output_format>
Return ONLY this JSON, no prose:
{{
  "impression_en": "<one punchy line: overall read of this build for this role>",
  "impression_zh": "<中文>",
  "strengths_en": ["<real strength for this posting>", "<...>"],
  "strengths_zh": ["<中文>", "<...>"],
  "improvements_en": ["<concrete improvement still worth making>", "<...>"],
  "improvements_zh": ["<中文>", "<...>"],
  "ats_gaps_en": ["<posting keyword/term not yet surfaced in the build>", "<...>"],
  "ats_gaps_zh": ["<中文>", "<...>"]
}}
Keep each list to 2-4 items. If a list is genuinely empty (e.g. no ATS gaps), \
return [].
</output_format>"""
)


def _generate_review(posting_ctx, tailored_bullets):
    """Silver Wolf's fuller review of the TAILORED build (impression / strengths /
    improvements / ats_gaps), bilingual, for the same preview embed. One FAST call.
    Returns a dict or None — never raises; the tailor works fine without it."""
    if not tailored_bullets:
        return None
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(tailored_bullets))
    prompt = _TAILOR_REVIEW_PROMPT.format(posting=posting_ctx, bullets=numbered)
    try:
        data = gemma_client.ask_json_text(prompt, 1800, chain=gemma_client.FAST_CHAIN)
    except Exception:
        log.exception("Tailor review generation failed; skipping review")
        return None
    return data if isinstance(data, dict) else None


def _review_and_refine(posting_ctx, metric_bullets):
    """Run the tailored bullets past the multi-actor review panel and return the
    refined list (same length/order). One FAST-tier call role-plays all four
    reviewers. Falls back to the input bullets for anything the model drops, so a
    flaky review can never lose or corrupt a bullet. Blocking — call via
    asyncio.to_thread.

    Skips the round-trip entirely for a very short résumé (<= _REVIEW_MIN_BULLETS):
    the rewrite already tailored those, and a whole extra model call isn't worth
    the latency for one or two lines."""
    if not metric_bullets:
        return metric_bullets
    # Too short to justify the review round-trip — but still apply the cheap
    # deterministic verb-dedup so even a 2-bullet résumé doesn't repeat an opener.
    if len(metric_bullets) <= _REVIEW_MIN_BULLETS:
        return _dedupe_opening_verbs(metric_bullets)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(metric_bullets))
    prompt = _ACTOR_REVIEW_PROMPT.format(posting=posting_ctx, bullets=numbered)
    try:
        text = gemma_client.ask_text(prompt, chain=gemma_client.FAST_CHAIN)
    except Exception:
        log.exception("Actor-review pass failed; keeping unreviewed bullets")
        return metric_bullets
    parsed = _parse_numbered(text, len(metric_bullets))
    out = []
    for i, original in enumerate(metric_bullets):
        cand = (parsed[i] if i < len(parsed) else "") or ""
        cand = cand.strip()
        if _review_acceptable(cand, original):
            # Deterministic guard: drop the panel's version too if it swapped or
            # invented a technology the pre-panel bullet didn't have.
            out.append(_reject_tech_fabrication(original, cand))
        else:
            out.append(original)
    # Whole-résumé coherence safety net: no two bullets should open with the same
    # action verb (the model is told this too, but code guarantees it).
    return _dedupe_opening_verbs(out)


# Interchangeable strong résumé action verbs, grouped by rough meaning so a swap
# keeps the sense. Used ONLY to vary a repeated OPENING verb — the rest of the
# bullet (all facts) is never touched.
_VERB_ALTS = {
    "built": ["Developed", "Engineered", "Created", "Constructed"],
    "developed": ["Built", "Engineered", "Created", "Programmed"],
    "created": ["Built", "Developed", "Designed", "Produced"],
    "designed": ["Architected", "Engineered", "Modeled", "Structured"],
    "led": ["Directed", "Headed", "Coordinated", "Drove"],
    "managed": ["Led", "Directed", "Oversaw", "Coordinated"],
    "improved": ["Enhanced", "Boosted", "Strengthened", "Refined"],
    "optimized": ["Streamlined", "Tuned", "Accelerated", "Refined"],
    "reduced": ["Cut", "Lowered", "Decreased", "Trimmed"],
    "implemented": ["Built", "Delivered", "Deployed", "Engineered"],
    "automated": ["Streamlined", "Scripted", "Orchestrated"],
    "analyzed": ["Assessed", "Evaluated", "Examined", "Investigated"],
}


def _dedupe_opening_verbs(bullets):
    """Return the bullets with duplicate OPENING verbs varied. Only the first word
    is ever swapped, and only for a same-meaning alternative — every fact in the
    bullet is preserved. A verb with no known alternative, or a bullet where no
    fresh alternative is available, is left as-is rather than risk a bad edit."""
    seen = set()
    out = []
    for b in bullets:
        if not b:
            out.append(b)
            continue
        parts = b.split(" ", 1)
        first = parts[0]
        key = first.lower().strip(".,:;")
        if key not in seen:
            seen.add(key)
            out.append(b)
            continue
        # Duplicate opener — try a same-meaning verb not already used.
        alt = next(
            (a for a in _VERB_ALTS.get(key, []) if a.lower() not in seen), None
        )
        if alt and len(parts) > 1:
            seen.add(alt.lower())
            out.append(f"{alt} {parts[1]}")
        else:
            # No safe swap available — keep the original (variety isn't worth a
            # broken or repeated verb).
            out.append(b)
    return out


def _review_acceptable(candidate, original):
    """Deterministic over-reach guard: accept the reviewed bullet ONLY if it's a
    plausible, in-scope rewrite of the original — else the caller keeps the
    original. Rejects an empty result, a multi-line bullet, a bloated one (the
    review shouldn't balloon length), and — importantly — a bullet that INTRODUCES
    a "[ADD METRIC]" slot the original didn't have (metrics belong only where they
    genuinely fit; the review must not metric-slot every line). This can't catch
    every possible fabrication, but it blocks the deterministic over-reach shapes;
    the prompt's truth_rules handle the rest."""
    if not candidate:
        return False
    if "\n" in candidate or "\r" in candidate:
        return False
    # Bloat guard: a legit sharpen stays close in length. Allow generous headroom
    # for adding a real keyword, but reject a run-on (padding / hallucinated scope).
    if len(candidate) > max(60, int(len(original) * 1.6) + 25):
        return False
    # Do not let the review turn a plain bullet into a metric bullet…
    if _METRIC_TOKEN in candidate and _METRIC_TOKEN not in original:
        return False
    # …and do not let it DROP a metric placeholder the original had (that slot is
    # the candidate's spot to fill a real number; losing it silently is data loss).
    if _METRIC_TOKEN in original and _METRIC_TOKEN not in candidate:
        return False
    return True


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


# The build service's LaTeX template does HARD (non-optional) lookups on these
# fields — a missing key returns HTTP 422 "Render error: '<field>'" and NO pdf, so
# the preview image silently fails. Backfill every required field (empty
# string / empty list) so ANY parsed résumé still builds. Verified against the
# live service by probing each field. Truly optional (never break the build):
#   contact: location, website, name · projects: technologies, date, link.
_REQUIRED_CONTACT = ("email", "phone", "github", "linkedin")
_REQUIRED_STR = {
    "education": ("school", "degree", "dates", "location"),
    "experience": ("company", "role", "dates", "location"),
    "projects": ("name",),
    "skills": ("category",),
}
_REQUIRED_LIST = {
    "experience": ("description",),
    "projects": ("description",),
    "skills": ("list",),
}


# Minimal filler item per section, used only when a section is empty/missing (the
# template 500s on an empty section list and 422s on a missing key). Carries every
# required subfield so it renders as a near-invisible placeholder row.
_SECTION_FILLER = {
    "education": {"school": "", "degree": "", "dates": "", "location": ""},
    "experience": {"company": "", "role": "", "dates": "", "location": "",
                   "description": [""]},
    "projects": {"name": "", "description": [""]},
    "skills": {"category": "", "list": [""]},
}


def _ensure_builder_fields(out):
    """In-place: guarantee every field the build service's template hard-requires
    exists, so a résumé missing (say) a phone, an education location, or a whole
    section still renders instead of 422/500-ing. Empty string / list for unknown
    scalar/list fields; a single filler item for an otherwise-empty section."""
    contact = out.get("contact")
    if not isinstance(contact, dict):
        contact = out["contact"] = {}
    for key in _REQUIRED_CONTACT:
        contact.setdefault(key, "")

    # Every top-level section must exist AND hold at least one item.
    for section, filler in _SECTION_FILLER.items():
        items = out.get(section)
        if not isinstance(items, list) or not items:
            import copy as _copy
            out[section] = [_copy.deepcopy(filler)]

    for section, keys in _REQUIRED_STR.items():
        for item in out.get(section) or []:
            if isinstance(item, dict):
                for key in keys:
                    item.setdefault(key, "")
    for section, keys in _REQUIRED_LIST.items():
        for item in out.get(section) or []:
            if isinstance(item, dict):
                for key in keys:
                    val = item.get(key)
                    if not isinstance(val, list) or not val:
                        item[key] = [val] if isinstance(val, str) and val else [""]


def _apply_edu_extras(structured):
    """The resume builder's education schema is fixed (school/location/degree/
    dates) — it has no GPA or coursework fields. Fold any captured GPA into the
    degree string and surface coursework as a 'Relevant Coursework' skills
    category, then drop the non-schema keys so the builder doesn't warn. Returns
    a modified deep copy."""
    import copy

    out = copy.deepcopy(structured)

    _ensure_builder_fields(out)

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
    out = []
    for i, original in enumerate(chunk):
        rw = parsed[i] if i < len(parsed) and parsed[i] else original
        # Deterministic safety net: if the model swapped/invented a technology to
        # match the posting, discard the rewrite and keep the original bullet.
        out.append(_reject_tech_fabrication(original, rw))
    return out


# Curated tech / language / framework tokens. A DETERMINISTIC guard against the
# model's #1 failure mode: swapping or fabricating a technology to match the
# posting (e.g. Swift→Python) — which gets the candidate caught in the interview.
# If a rewrite introduces any of these that the ORIGINAL bullet didn't contain,
# the rewrite is a fabrication and we discard it, keeping the original bullet.
# Multi-word / punctuated names first so they match before their bare stems.
_TECH_TOKENS = (
    "node.js", "next.js", "vue.js", "react native", "objective-c", "c++", "c#",
    "ci/cd", "rest api", "graphql", "postgresql", "mysql", "mongodb", "sqlite",
    "redis", "dynamodb", "swiftui", "jetpack compose", "kotlin", "swift",
    "python", "javascript", "typescript", "java", "golang", "rust", "ruby",
    "php", "scala", "flask", "django", "fastapi", "express", "spring", "rails",
    "react", "angular", "svelte", "flutter", "tensorflow", "pytorch", "keras",
    "pandas", "numpy", "aws", "gcp", "azure", "docker", "kubernetes", "terraform",
    "supabase", "firebase", "postgres", "tailwind", "graphql",
)


def _tech_set(text):
    """The curated tech tokens present in `text` (lowercased, word-ish match)."""
    low = (text or "").lower()
    found = set()
    for tok in _TECH_TOKENS:
        # Word-boundary-ish: token surrounded by non-alphanumerics (or ends).
        if re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", low):
            found.add(tok)
    return found


def _reject_tech_fabrication(original, rewritten):
    """Return the safe bullet: if the rewrite introduced any curated tech token
    the original didn't have (a swap or invented tool), it's a fabrication that
    would sink the candidate in the interview — discard it and keep the ORIGINAL.
    Otherwise the rewrite is clean (it may drop or reword, never invent tech)."""
    if not rewritten or not rewritten.strip():
        return original
    added = _tech_set(rewritten) - _tech_set(original)
    # "postgres"/"postgresql" and "rest api" overlaps are handled by exact tokens;
    # a genuinely new tech name in `added` means the rewrite invented/swapped it.
    if added:
        log.info(
            "Tailor: rejecting rewrite (fabricated tech %s) — keeping original",
            sorted(added),
        )
        return original
    return rewritten


def _strip_metric_phrase(text):
    """Rough no-metric version: remove an 'as measured by [ADD METRIC]' clause,
    plus any stray placeholder anywhere else, then tidy the spacing/punctuation so
    the fallback bullet still reads as a clean professional line (never a fragment
    with a double space or a dangling comma left where the metric used to be)."""
    if not text:
        return text
    # Drop the whole "as measured by [ADD METRIC]" clause (the common shape)…
    t = re.sub(r"(?i)\s*,?\s*as measured by\s*\[ADD METRIC\]", "", text)
    # …a "by/to/of [ADD METRIC]" tail (a dangling preposition left behind reads
    # worse than nothing)…
    t = re.sub(r"(?i)\s+(?:by|to|of|at)\s*\[ADD METRIC\]", "", t)
    # …and any placeholder that slipped in elsewhere.
    t = t.replace(_METRIC_TOKEN, "")
    # Tidy leftovers: collapse doubled spaces, fix a space-before-punct, and
    # clean up a dangling comma/space at the ends.
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    return t.strip(" ,;:").strip()


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
    # +1 for the multi-actor review pass, +1 for the no-metric polish at the end.
    total = len(chunks) + 2
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

    # Multi-actor review panel (ATS / HR / hiring manager / technical): one FAST
    # pass that refines the bullets past everyone who gates the résumé before the
    # interview. Guarded so it can only ever return a same-length list of real
    # bullets; on any mismatch we keep the pre-review bullets (never lose one).
    reviewed = await asyncio.to_thread(
        _review_and_refine, posting_ctx, metric_bullets
    )
    if isinstance(reviewed, list) and len(reviewed) == len(metric_bullets):
        metric_bullets = [
            (r.strip() if isinstance(r, str) and r.strip() else orig)
            for r, orig in zip(reviewed, metric_bullets)
        ]
    if progress:
        await progress(len(chunks) + 1, total)

    # One cheap batched pass for the clean no-metric variants, plus Silver Wolf's
    # overview and her fuller review — run concurrently so they add no extra
    # wall-clock. (The per-bullet before→after "change notes" call was dropped: the
    # diff is no longer shown in the embed, so generating it was wasted latency.)
    nometrics, overview, review = await asyncio.gather(
        asyncio.to_thread(_polish_nometrics, posting_ctx, metric_bullets),
        asyncio.to_thread(_generate_overview, posting_ctx, metric_bullets),
        asyncio.to_thread(_generate_review, posting_ctx, metric_bullets),
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
    return {
        "structured": structured,
        "bullets": bullets,
        "overview": overview,
        "review": review,
    }


_METRIC_TOKEN = "[ADD METRIC]"
_MODAL_PAGE = 5  # Discord modal hard limit is 5 inputs


def _blob_to_yaml(blob, values=None, keep_token=False):
    """Assemble resume YAML from a tailor blob. For each tailored bullet: if a
    metric value is provided (non-empty, non-skip), use the metric version with
    [ADD METRIC] replaced by that value; otherwise use the clean no-metric
    version. `values` is a dict {bullet_index: value}. Education/skills untouched.

    keep_token=True is the PREVIEW mode: an unfilled (blank, not explicitly
    skipped) slot keeps the literal [ADD METRIC] in the metric version so the
    preview image SHOWS the user where to drop real numbers. This mode must NEVER
    be used for the final downloadable PDF — a placeholder must never reach a
    recruiter/ATS — only for the on-screen preview.
    """
    import copy

    import yaml as _yaml

    structured = copy.deepcopy(blob.get("structured") or {})
    values = values or {}
    for idx, b in enumerate(blob.get("bullets") or []):
        sec, i, j = b["loc"]
        m = b.get("m") or ""
        has_slot = _METRIC_TOKEN in m
        val = str(values.get(idx, "") or "").strip()
        if not has_slot:
            # No [ADD METRIC] placeholder → this bullet either already has a REAL
            # number or is qualitative. Keep the metric version verbatim: NEVER
            # strip it to the no-metric variant (that would delete a real number
            # the candidate earned). Nothing to fill here.
            text = m or b.get("n") or ""
        elif val and val != _SKIP:
            # Slot + a value the user typed → drop the number in.
            text = (m or b.get("n") or "").replace(_METRIC_TOKEN, val)
        elif keep_token and val != _SKIP:
            # Preview: blank (not skipped) slot → show the metric version WITH the
            # [ADD METRIC] placeholder intact so the user sees where numbers go.
            text = m or b.get("n") or ""
        else:
            # Slot left blank / skipped → the clean no-metric version.
            text = b.get("n") or _strip_metric_phrase(m)
        # Safety: outside preview mode a placeholder must NEVER reach a
        # recruiter/ATS. If the no-metric fallback somehow still carries the
        # token, strip it clean.
        if _METRIC_TOKEN in text and not keep_token:
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


def make_progress_updater(interaction, verb="Rewriting your résumé"):
    """Return an async progress(done, total) that edits the interaction's
    deferred response with a status line + live bar. Failure-tolerant."""
    async def progress(done, total):
        bar = progress_bar(done, total)
        content = (
            f"✍️ **{verb}…** — tailoring your build to the posting, then running it "
            "past my review panel (ATS, recruiter, hiring manager, tech lead) so "
            "nothing slips. Sit tight, I'll **DM** you the preview the second it's "
            f"clean — go grab a drink, I've got this.\n{bar}"
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
                content="📬 Done — dropped your Aether-Edited résumé in your **DMs**. Go read it, then go get that interview.",
                embed=None,
                attachments=[],
                view=None,
            )
        except Exception:
            pass
        return

    # DMs closed — send the result ephemerally instead (best effort).
    # A failed DM send may have already consumed the file's stream; reset it so
    # the fallback re-upload starts from the top instead of sending 0 bytes.
    if file is not None:
        try:
            file.reset(seek=True)
        except Exception:
            pass
    kwargs = {"ephemeral": True}
    if embed is not None:
        kwargs["embed"] = embed
    if file is not None:
        kwargs["file"] = file
    if view is not None:
        kwargs["view"] = view
    try:
        await interaction.edit_original_response(
            content="📎 Your DMs are locked — firewall's up on your end, not mine. Fine, here it is:",
            embed=embed, attachments=[file] if file else [], view=view,
        )
    except Exception:
        try:
            await interaction.followup.send(
                content="📎 Your DMs are locked — firewall's up on your end, not mine. Fine, here it is:", **kwargs
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
        # Modal titles are capped ~45 chars and can't render markdown, so keep
        # the Silver Wolf flavor to a light touch here.
        title = "⚡ Load your real numbers"
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
                placeholder=(shown[:97] + "…") if len(shown) > 98 else (shown or "your real number — e.g. 40%, 10k users, 3x"),
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
        # Fallback opener (used only if the AI intro didn't come back). The live
        # opener is overview["intro_en"], varied per tailor.
        "preview_intro_fallback": (
            "...Fine, I ran **Aether Editing** on your résumé, then ran it past my "
            "review panel — ATS parser, recruiter, hiring manager, tech lead — so "
            "nothing slips. Don't make it weird."
        ),
        # Factual meta lines the bot always appends after the (AI or fallback)
        # opener — accurate counts + instructions, never AI-authored.
        "preview_meta_have": (
            "Preview's below — **{filled}/{total} metrics filled**. Real numbers "
            "buff the bullets; leave 'em blank and it falls back to the clean "
            "no-number wording. Every word's yours — I didn't invent a thing.\n"
            "Hit **📄 PDF** when you want the 1-page file."
        ),
        "preview_meta_none": (
            "Preview's below; hit **📄 PDF** for the 1-page file. Only your real "
            "stuff in there, nothing made up."
        ),
        "ov_name": "🐺 Silver Wolf's Read",
        "ov_strong": "💪 **Strong:**", "ov_changed": "🔧 **I tuned:**",
        "ov_todo": "🎯 **Still needs you:**",
        "rv_impression": "🔍 My read",
        "rv_strengths": "✅ Strengths", "rv_improvements": "🔧 Still worth fixing",
        "rv_ats_gaps": "🤖 ATS / keyword gaps",
        "name": "👤 Name", "education": "🎓 Education", "experience": "💼 Experience",
        "projects": "🛠️ Projects", "skills": "🧩 Skills",
        "footer": "🐺 Aether-Edited by Silver Wolf • it's your build, give it one last read before you ship",
        "add": "Metrics ({n})", "edit": "Metrics",
        "build_no": "No metrics", "download": "PDF",
        "use_metrics": "Metrics", "delete": "Delete",
        "builder_off_name": "⚠️ Builder offline",
        "builder_off": ("Couldn't reach the PDF builder — here's the YAML. Press "
                        "**Download PDF** to retry."),
        "img_off_name": "🖼️ Preview image unavailable",
        "img_off_pdf": ("Couldn't render the image this time — I attached the "
                        "**PDF** instead, open it to read your build."),
        "img_off_none": ("Couldn't reach the résumé builder for a preview right "
                         "now — hit **📄 PDF** to try building the file."),
        "author": "🐺 Silver Wolf · Aether-Edited résumé",
        "mb_name": "📊 Metrics · {filled}/{total}",
        "mb_todo": ("**{left}** slot(s) left. Real numbers hit harder — tap **📊 "
                    "Metrics** to load them, or leave blank for the clean version."),
        "mb_done": "All loaded. Clean run — go download it.",
        "status_ready": ("Your **1-page** résumé's patched and ready — clean run, "
                         "cleared every gate. Take it and go get that interview; "
                         "I did my part. Preview below, click to download.\n"),
        "status_no_metrics": ("*Shipped without metrics — every bullet uses its "
                              "clean, number-free version. Still solid, don't stress.*"),
        "status_real": "*Only your real stats — I don't do fabricated loot.*",
        "status_metrics": ("Numbers are your crit buff — drop in the real ones and "
                           "the bullets hit way harder. Leave any blank and I've got "
                           "you: it falls back to the clean no-number wording. Or "
                           "just hit **Build without metrics**.\n​"),
    },
    "zh": {
        "title": "✍️ 以太编辑过的简历 — {company}",
        # 备用开场白（仅当 AI 没返回时用）。实际开场是 overview["intro_zh"]，每次不同。
        "preview_intro_fallback": (
            "……行吧，我用**以太编辑**把你简历改了改，还让我的评审团——ATS、HR、"
            "招聘经理、技术面——都过了一遍，保证没死角。别搞得怪怪的。"
        ),
        # 机器人总会附在开场后的事实信息——数字准确，非 AI 生成。
        "preview_meta_have": (
            "预览在下面——**已填 {filled}/{total} 个数据**。填真实数字给要点加暴击，"
            "留空的会用干净的无数字版本。每个字都是你的，我一个都没编。\n"
            "想要一页 PDF 就点 **📄 PDF**。"
        ),
        "preview_meta_none": (
            "预览在下面，点 **📄 PDF** 拿一页文件。只装了你的真实数据，绝不刷假装备。"
        ),
        "ov_name": "🐺 银狼的点评",
        "ov_strong": "💪 **强项：**", "ov_changed": "🔧 **我改了：**",
        "ov_todo": "🎯 **还需要你：**",
        "rv_impression": "🔍 我的判断",
        "rv_strengths": "✅ 强项", "rv_improvements": "🔧 还能再改",
        "rv_ats_gaps": "🤖 ATS / 关键词短板",
        "name": "👤 姓名", "education": "🎓 教育", "experience": "💼 经历",
        "projects": "🛠️ 项目", "skills": "🧩 技能",
        "footer": "🐺 银狼以太编辑完成 • 这是你的配装，提交前自己再过一遍",
        "add": "数据（{n}）", "edit": "数据",
        "build_no": "不填数据", "download": "PDF",
        "use_metrics": "数据", "delete": "删除",
        "builder_off_name": "⚠️ 生成器离线",
        "builder_off": "连不上 PDF 生成器——先给你 YAML。点 **下载 PDF** 重试。",
        "img_off_name": "🖼️ 预览图暂不可用",
        "img_off_pdf": "这次没能渲染成图片——我把 **PDF** 附上了，打开就能看你的配装。",
        "img_off_none": "现在连不上简历生成器做预览——点 **📄 PDF** 试试生成文件。",
        "author": "🐺 银狼 · 以太编辑简历",
        "mb_name": "📊 数据 · {filled}/{total}",
        "mb_todo": ("还剩 **{left}** 个槽位。真实数字更有杀伤力——点 **📊 数据** 填上，"
                    "或者留空用干净版本。"),
        "mb_done": "全填好了。干净通关——下载走人。",
        "status_ready": "你的**一页**简历补丁打好了——干净通关，每道门都过了。拿去把面试拿下，我这边做完了。下方预览，点击下载。\n",
        "status_no_metrics": "*没填数据直接出——每条要点用的都是干净的无数字版本。照样能打，别慌。*",
        "status_real": "*只用你的真实数据，我不刷假装备。*",
        "status_metrics": ("数字就是你的暴击——把真实的填进去，要点狠一大截。"
                           "留空的我带你：自动用干净的无数字版本。或者直接点 **不填数据直接生成**。\n​"),
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
    def _add_metric_banner(self, embed, t):
        """A prominent metric-status field near the top — surfaces the [ADD METRIC]
        call-to-action instead of burying it in the description. Skipped in
        no-metrics mode / when there are no slots."""
        total = self.n_total
        if not total or self.no_metrics:
            return
        filled = self.n_filled
        blocks = "🟪" * filled + "⬜" * (total - filled)
        if filled >= total:
            body = t["mb_done"]
        else:
            body = t["mb_todo"].format(left=total - filled)
        embed.add_field(
            name=t["mb_name"].format(filled=filled, total=total),
            value=f"{blocks}\n{body}",
            inline=False,
        )

    def _add_read_field(self, embed, t):
        """Silver Wolf's read — merged from overview (strong/tuned/todo). One tight
        block so the user gets the story at a glance, no wall of overlapping text.
        Impression from the review is intentionally dropped (it duplicates the
        description opener)."""
        ov = self.blob.get("overview")
        if not isinstance(ov, dict):
            return
        lang = self.lang
        parts = []
        for key, label in (
            ("strong", t["ov_strong"]),
            ("changed", t["ov_changed"]),
            ("todo", t["ov_todo"]),
        ):
            val = str(ov.get(f"{key}_{lang}") or ov.get(f"{key}_en") or "").strip()
            if val:
                parts.append(f"{label} {val}")
        if parts:
            embed.add_field(
                name=t["ov_name"], value="\n".join(parts)[:1024], inline=False
            )

    def _add_scan_fields(self, embed, t):
        """The recruiter/ATS scan as a scannable INLINE trio — Strengths · Fix ·
        ATS gaps sit side-by-side (Discord lays out up to 3 inline fields per row)
        so the whole read is one glance, not three stacked walls."""
        rv = self.blob.get("review")
        if not isinstance(rv, dict):
            return
        lang = self.lang

        def _pick(base):
            return rv.get(f"{base}_{lang}") or rv.get(f"{base}_en") or rv.get(base)

        rendered = 0
        for base, label in (
            ("strengths", t["rv_strengths"]),
            ("improvements", t["rv_improvements"]),
            ("ats_gaps", t["rv_ats_gaps"]),
        ):
            items = _pick(base)
            if isinstance(items, list) and items:
                block = "\n".join(
                    f"• {str(x).strip()}" for x in items[:3] if str(x).strip()
                )
                if block:
                    embed.add_field(name=label, value=block[:1024], inline=True)
                    rendered += 1
        # Discord pads the last row; a 2-field row looks lopsided, so add a spacer
        # to keep the inline grid even.
        if rendered == 2:
            embed.add_field(name="​", value="​", inline=True)

    def preview_embed(self, with_body=False):
        """Preview embed for the tailored résumé. Defaults to with_body=False:
        the résumé is shown as the rendered IMAGE (or attached PDF), never as raw
        text sections — the embed carries only the title, Silver Wolf's varied
        opener/read, and the review.

        with_body=True re-enables the legacy text sections (name/edu/experience/
        projects/skills). It is intentionally NOT used by the live preview path —
        kept only for any explicit text-only caller."""
        import yaml as _yaml

        try:
            data = _yaml.safe_load(self.current_yaml()) or {}
        except Exception:
            data = self.blob.get("structured") or {}

        t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
        embed = discord.Embed(
            title=t["title"].format(company=self.company)[:256],
            color=_SW_PURPLE,  # Silver Wolf violet, matching the Score embed
        )
        # Identity line up top so every tailor reads unmistakably as Silver Wolf.
        embed.set_author(name=t["author"])
        # Opener: only the AI-varied intro (the factual metric/download details now
        # live in their own scannable banner field, not crammed into the blurb).
        ov = self.blob.get("overview")
        intro = ""
        if isinstance(ov, dict):
            intro = str(ov.get(f"intro_{self.lang}") or ov.get("intro_en") or "").strip()
        if not intro:
            intro = t["preview_intro_fallback"]
        embed.description = intro[:4096]

        if not with_body:
            # Résumé shown as the attached image. Field order = reading order:
            #   1. metric CTA banner (what to do next)      — surfaced, not buried
            #   2. Silver Wolf's read (strong / tuned / todo)
            #   3. recruiter/ATS scan as an inline trio       — one-glance scan
            #   (image sits below all fields, footer closes it)
            # The per-bullet before→after diff is intentionally NOT shown here —
            # the résumé image is the source of truth; a change log just adds noise.
            self._add_metric_banner(embed, t)
            self._add_read_field(embed, t)
            self._add_scan_fields(embed, t)
            embed.set_footer(text=t["footer"])
            return embed

        # Legacy text-body path keeps the old overview+review helpers.
        self._add_read_field(embed, t)

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

        # Recruiter/ATS scan trio (legacy text path).
        self._add_scan_fields(embed, t)

        embed.set_footer(text=t["footer"])
        return embed

    async def preview_render(self):
        """Async preview: render the CURRENT résumé to a real PDF, rasterize page 1
        to a PNG, and return (embed, file) with that image set on the embed — so
        the user sees the résumé as an IMAGE, never the raw text sections. The
        preview keeps the literal [ADD METRIC] placeholders on any unfilled slot so
        the user can SEE where to drop real numbers (filled slots show the value).
        If the image can't be produced (builder offline / raster failed) we attach
        the PDF and say so — but we STILL never dump the résumé text into the embed.
        Never raises."""
        png = None
        pdf = None
        try:
            # keep_token=True → the preview image shows [ADD METRIC] on unfilled
            # slots. Respects self.values so already-filled numbers appear. In
            # no_metrics mode every slot is skipped → clean, no placeholders.
            if self.no_metrics:
                vals = {i: _SKIP for i in self.metric_indices}
            else:
                vals = self.values
            yaml_text = _blob_to_yaml(self.blob, vals, keep_token=True)
            pdf = await asyncio.to_thread(_build_pdf_sync, yaml_text)
            if pdf:
                png = await asyncio.to_thread(_pdf_to_png, pdf)
        except Exception:
            log.exception("Tailor preview render failed")
            png = None

        # Always with_body=False — the résumé itself is shown as the image (or the
        # attached PDF on fallback), NEVER as text fields in the embed.
        embed = self.preview_embed(with_body=False)
        if png:
            embed.set_image(url="attachment://resume_preview.png")
            return embed, discord.File(io_bytes(png), filename="resume_preview.png")
        if pdf:
            # Couldn't rasterize but the PDF built — attach it, tell them to open it.
            t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
            embed.add_field(
                name=t["img_off_name"], value=t["img_off_pdf"], inline=False
            )
            return embed, discord.File(
                io_bytes(pdf), filename=f"resume_{self.safe_company}.pdf"
            )
        # Builder fully offline — no image, no PDF. Note it; still no text dump.
        t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
        embed.add_field(
            name=t["img_off_name"], value=t["img_off_none"], inline=False
        )
        return embed, None

    def status_embed(self):
        t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
        total = self.n_total
        if self.no_metrics or total == 0:
            embed = discord.Embed(
                title=t["title"].format(company=self.company)[:256],
                color=_SW_PURPLE,
                description=(
                    t["status_ready"]
                    + (t["status_no_metrics"] if self.no_metrics else t["status_real"])
                ),
            )
            return embed

        filled = self.n_filled
        color = _SW_PURPLE
        zh = self.lang == "zh"
        embed = discord.Embed(
            title=t["title"].format(company=self.company)[:256], color=color
        )
        blocks = "🟩" * filled + "⬜" * (total - filled)
        head = (f"**已填数据：{filled}/{total}**  {blocks}\n" if zh
                else f"**Metrics: {filled}/{total} filled**  {blocks}\n")
        embed.description = head + t["status_metrics"]
        SHOW = 9
        st_skip = "无数字版本" if zh else "no-number version"
        st_need = "等你填数字" if zh else "waiting on your number"
        st_added = "锁定" if zh else "locked in"
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
        """Read-only preview embed WITHOUT the text résumé body (the résumé is
        only ever shown as the rendered image / PDF, never dumped as text)."""
        return self.preview_embed(with_body=False)

    def _build_buttons(self):
        self.clear_items()
        t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])

        # Language toggle — flips chat labels/notes (résumé stays English).
        # Compact + high-contrast: plain text, no faint globe glyph.
        lang_btn = discord.ui.Button(
            label="中文 / EN",
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

        # 🗑️ Delete — remove this bot message (works in the DM copy + ephemeral).
        # Lets the user clear the tailored résumé from their DMs when done.
        delete = discord.ui.Button(
            emoji="🗑️", style=discord.ButtonStyle.danger
        )
        delete.callback = self._on_delete
        self.add_item(delete)

    # --- actions ----------------------------------------------------------
    async def _on_delete(self, interaction):
        """Delete the message this view is attached to. In a DM the bot owns the
        message, so message.delete() works. Ephemeral messages can't be truly
        deleted, so fall back to collapsing them to a tombstone."""
        msg = interaction.message
        try:
            if msg is not None:
                await msg.delete()
                # DM delete succeeds without needing an interaction response.
                if not interaction.response.is_done():
                    try:
                        await interaction.response.defer()
                    except Exception:
                        pass
                return
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        except Exception:
            log.exception("Tailor delete-button failed")
        # Ephemeral (can't delete) → blank it out instead.
        try:
            await interaction.response.edit_message(
                content="🗑️ *Cleared.*", embed=None, attachments=[], view=None
            )
        except Exception:
            pass

    async def _on_lang(self, interaction):
        self.lang = "zh" if self.lang == "en" else "en"
        self._build_buttons()
        # Re-render the image preview in the new language (labels/overview/review
        # localize; the résumé image itself is English either way). Defer first —
        # the PDF build + raster is too slow for a 3s interaction response.
        await interaction.response.defer()
        embed, file = await self.preview_render()
        kwargs = {"embed": embed, "view": self}
        kwargs["attachments"] = [file] if file is not None else []
        try:
            await interaction.edit_original_response(**kwargs)
        except Exception:
            # Fallback: no-body embed, no image (e.g. attachment edit rejected).
            # Still never dumps the résumé text.
            await interaction.edit_original_response(
                embed=self.preview_embed(with_body=False), view=self, attachments=[]
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
        files = []
        if pdf:
            self.last_pdf = pdf
            files.append(
                discord.File(io_bytes(pdf), filename=f"resume_{self.safe_company}.pdf")
            )
            # Render page 1 to a PNG so the résumé is viewable right in the embed
            # (not just a text list). Falls through gracefully if fitz is missing.
            png = await asyncio.to_thread(_pdf_to_png, pdf)
            if png:
                files.append(discord.File(io_bytes(png), filename="resume_preview.png"))
                embed.set_image(url="attachment://resume_preview.png")
        else:
            files.append(
                discord.File(
                    io_bytes(yaml_text.encode("utf-8")),
                    filename=f"tailored_resume_{self.safe_company}.yaml",
                )
            )
            t = _TAILOR_TEXT.get(self.lang, _TAILOR_TEXT["en"])
            embed.add_field(
                name=t["builder_off_name"], value=t["builder_off"], inline=False,
            )
        await interaction.followup.send(
            embed=embed, files=files, view=self, ephemeral=True
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
_TAILOR_BLOB_VERSION = 28


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
    # Render the unfinished résumé to a viewable image (falls back to text preview).
    embed, file = await view.preview_render()
    return embed, file, view, None
