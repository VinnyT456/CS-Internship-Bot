"""The four-agent résumé pipeline (Diagnoser → Recruiter → Rewriter → Hiring
Manager), adapted from the "4 Claude agents that get your résumé past the bot"
method into Silver Wolf's voice.

HARD RULES (product-critical, enforced here + in code downstream):
  - Every agent reads the MECHANICALLY-extracted résumé text (resume_utils
    .extract_text_from_pdf) — never a vision transcription of a text PDF. No
    parsing is done by a model, so the model can't hallucinate résumé content.
  - The model NEVER invents a number, skill, tool, title, or employer. Missing
    metrics become the literal placeholder `[NUMBER?]` (Google XYZ method). This
    is stated in every prompt AND belongs to the existing anti-fabrication
    doctrine (job_ai._reject_tech_fabrication) the Rewriter output flows through.

TOKEN DISCIPLINE:
  - Lazy: each agent is one call, run only when the user reaches that stage.
  - Compact JSON schemas (short-ish keys, capped list sizes), capped
    max_output_tokens, temperature tuned per task, and the right model tier —
    SMART (Gemma) only for the judgment stages (Diagnoser, Rewriter); FAST/LITE
    (Flash-lite) for extraction/interview.
"""

import logging

from commands import gemma_client, persona

logger = logging.getLogger("cs_internship_bot")

# XYZ formula reminder reused across the rewrite-oriented prompts.
_XYZ = (
    "Use Google's XYZ bullet formula: \"Accomplished [X], as measured by [Y], by "
    "doing [Z]\" — strong past-tense verb first; never 'Responsible for', 'Helped "
    "with', or 'Duties included'."
)

# The anti-fabrication clause, verbatim in every agent that could be tempted.
_NEVER_INVENT = (
    "NEVER invent a number, metric, skill, tool, title, employer, or date the "
    "résumé does not actually contain. If a bullet needs a metric the résumé "
    "doesn't give, write the literal token [NUMBER?] — do NOT guess a value. "
    "Quote résumé lines VERBATIM; if you can't find a line in the text, don't "
    "mention it."
)


def _gemma_then_format(reason_prompt, format_instructions, *, reason_tokens=1200,
                       format_tokens=2000, format_temp=0.1):
    """Two-call hybrid for the judgment stages: Gemma (SMART) can't emit JSON — it
    burns its budget reasoning and returns empty, and rejects thinking_budget=0 —
    so let it do what it's good at (reason in PROSE), then hand that prose to
    Flash-lite to STRUCTURE into strict JSON (no judgment, just formatting).

    Returns the parsed dict/None. Costs 2 calls; used only where Gemma's judgment
    is worth it (diagnose, rewrite). If Gemma yields nothing, we fall back to
    formatting straight from the original inputs so the stage still produces output.
    """
    reasoning = gemma_client.ask_text(reason_prompt, chain=gemma_client.SMART_CHAIN)
    reasoning = (reasoning or "").strip()
    # Flash-lite turns the analysis into JSON. If Gemma was empty, the format step
    # still runs on the instructions alone (degraded but non-empty).
    fmt_prompt = (
        format_instructions
        + "\n\n<analysis_to_structure>\n"
        + (reasoning or "(no analysis produced — infer conservatively from the "
           "instructions above; invent nothing)")
        + "\n</analysis_to_structure>"
    )
    return gemma_client.ask_json_text(
        fmt_prompt, max_output_tokens=format_tokens, temperature=format_temp,
        chain=gemma_client.FAST_CHAIN,
    )


# --- Agent 1: Diagnoser -------------------------------------------------------
# ATS parser's-eye view of the résumé. SMART tier (judgment). Text-only.
def diagnose(resume_text, *, text_source="pdf"):
    """Return a dict:
      { score:int0-100, top_fix:str,
        screen_out:[{line:str, reason:str}],   # reason ∈ the fixed code set
        missing_sections:[str], parse_notes:[str] }
    or None. `text_source='ocr'` means the PDF had no text layer (image-only) —
    we surface that as the top parse risk."""
    if not resume_text or not resume_text.strip():
        return None

    ocr_warning = ""
    if text_source == "ocr":
        ocr_warning = (
            "\nNOTE: this text came from OCR of an IMAGE-ONLY PDF — real ATS "
            "software can't read it at all. Make that the #1 parse_note and cap "
            "the score at 40 regardless of content quality.\n"
        )

    # Two-call HYBRID (your choice): Gemma judges (its strength), Flash-lite formats.
    # Step 1 — Gemma reasons in PROSE (no JSON; Gemma can't emit JSON reliably).
    reason_prompt = (
        persona.SILVER_WOLF_SYSTEM
        + "\n\n<this_task>\n"
        "You are an applicant tracking system (ATS), not a career coach — but "
        "voiced as Silver Wolf: sharp, a little smug, genuinely on their side. "
        "Read this résumé the way the machine does and analyze what would get them "
        "screened out. Be blunt; do NOT compliment; do NOT rewrite the bullets.\n"
        + _NEVER_INVENT
        + ocr_warning
        + "\nWork through it in plain prose (no JSON yet): give an overall "
        "parse-and-match readiness score 0-100 and the single highest-leverage "
        "fix; list the weak lines QUOTED VERBATIM, each tagged with exactly one of "
        "NO METRIC / PASSIVE / VAGUE / NO PROOF / DUPLICATE / DATED; list any "
        "recruiter-expected sections that are missing; and note anything a parser "
        "would garble.\n</this_task>\n\n"
        f"<resume>\n{resume_text[:8000]}\n</resume>"
    )
    # Step 2 — Flash-lite structures Gemma's analysis into strict JSON.
    format_instructions = (
        "Convert the ATS analysis below into STRICT JSON — you are only formatting, "
        "add no judgment and invent nothing; copy the résumé lines VERBATIM.\n"
        "Return ONLY this JSON (no markdown):\n"
        "{\n"
        '  "score": <int 0-100>,\n'
        '  "top_fix": "<the single highest-leverage change, one line>",\n'
        '  "screen_out": [{"line": "<résumé line VERBATIM>", "reason": "<ONE of: '
        'NO METRIC | PASSIVE | VAGUE | NO PROOF | DUPLICATE | DATED>"}],\n'
        '  "missing_sections": ["<section a recruiter expects and can\'t find>"],\n'
        '  "parse_notes": ["<what a parser would drop/garble>"]\n'
        "}\n"
        "Caps: screen_out<=8, missing_sections<=5, parse_notes<=5."
    )
    data = _gemma_then_format(
        reason_prompt, format_instructions, reason_tokens=1200, format_tokens=1400
    )
    return _clean_diagnose(data)


def _clean_diagnose(data):
    if not isinstance(data, dict):
        return None
    try:
        data["score"] = max(0, min(100, int(data.get("score", 0))))
    except (TypeError, ValueError):
        data["score"] = 0
    so = data.get("screen_out")
    if not isinstance(so, list):
        so = []
    clean = []
    for item in so[:8]:
        if isinstance(item, dict) and item.get("line"):
            clean.append({"line": str(item["line"]), "reason": str(item.get("reason", "VAGUE"))})
    data["screen_out"] = clean
    for k in ("missing_sections", "parse_notes"):
        v = data.get(k)
        data[k] = [str(x) for x in v][:5] if isinstance(v, list) else []
    data["top_fix"] = str(data.get("top_fix", "")).strip()
    return data


# --- Agent 2: Recruiter -------------------------------------------------------
# JD keyword extraction + honest match. FAST tier (extraction-shaped). Text-only.
def match(resume_text, job_description, github_projects=None):
    """Return { outcomes:[str], keywords:[{kw, phrasing, in_resume:bool, where}],
    claimable:[{kw, bullet}], missing_musthaves:[str],
    coverable:[{kw, project}] } or None. Only lists keywords the résumé can
    HONESTLY claim; flags real must-have gaps plainly.

    `github_projects` (optional) is a list of the user's real GitHub projects
    [{repo_name, tech:[..], summary}] pulled from the DB cache. When given, a
    JD must-have the RÉSUMÉ lacks but a PROJECT genuinely proves is reported in
    `coverable` (kw + which project) instead of a hard gap — still honest, since
    the project is real, but actionable ('add project X to cover this')."""
    if not resume_text or not job_description:
        return None

    gh_block = ""
    if github_projects:
        lines = []
        for p in github_projects[:12]:
            tech = ", ".join(str(t) for t in (p.get("tech") or [])[:10])
            name = str(p.get("repo_name") or "").strip()
            if name:
                lines.append(f"- {name} — {tech}" if tech else f"- {name}")
        if lines:
            gh_block = (
                "\n\nThe candidate ALSO has these real, verifiable GitHub projects "
                "(not yet all on the résumé). If a JD must-have is missing from the "
                "RÉSUMÉ but one of these projects genuinely demonstrates it, list it "
                "in `coverable` (the keyword + the project that proves it) instead of "
                "in `missing_musthaves`. NEVER claim a project proves a skill it "
                "doesn't actually use.\n<github_projects>\n"
                + "\n".join(lines)
                + "\n</github_projects>"
            )

    prompt = (
        persona.SILVER_WOLF_SYSTEM
        + "\n\n<this_task>\n"
        "You are a technical recruiter (Silver Wolf voice) filling the role below. "
        "The JOB DESCRIPTION is the answer key — score the résumé against it.\n"
        + _NEVER_INVENT
        + " Only claim what is ALREADY true in the résumé (or a listed GitHub "
        "project). If a must-have is missing, say so plainly.\n</this_task>\n\n"
        "Return ONLY this JSON:\n"
        "{\n"
        '  "outcomes": ["<the 3 outcomes this role is hired to produce>"],\n'
        '  "keywords": [\n'
        '    {"kw": "<hard skill/tool>", "phrasing": "<the JD\'s EXACT words>", '
        '"in_resume": true|false, "where": "<section it should live in>"}\n'
        "  ],\n"
        '  "claimable": [{"kw": "<keyword the résumé honestly supports>", '
        '"bullet": "<the exact existing bullet to work it into>"}],\n'
        '  "missing_musthaves": ["<a required skill neither résumé nor a project has>"],\n'
        '  "coverable": [{"kw": "<must-have the résumé lacks>", '
        '"project": "<the GitHub project that genuinely proves it>"}]\n'
        "}\n"
        "Cap keywords at the 10 most prominent (ranked by JD frequency/prominence), "
        "outcomes at 3, claimable at 8, missing_musthaves at 5, coverable at 5.\n\n"
        f"<job_description>\n{job_description[:4000]}\n</job_description>\n\n"
        f"<resume>\n{resume_text[:8000]}\n</resume>"
        + gh_block
    )
    data = gemma_client.ask_json_text(
        prompt, max_output_tokens=1700, temperature=0.1, chain=gemma_client.FAST_CHAIN
    )
    if not isinstance(data, dict):
        return None
    kws = data.get("keywords")
    data["keywords"] = kws[:10] if isinstance(kws, list) else []
    for k, cap in (("outcomes", 3), ("claimable", 8),
                   ("missing_musthaves", 5), ("coverable", 5)):
        v = data.get(k)
        data[k] = v[:cap] if isinstance(v, list) else []
    return data


# --- Agent 3: Rewriter --------------------------------------------------------
# XYZ rewrite of weak bullets, keyword-aware. SMART tier. Text-only.
def rewrite(resume_text, claimable_keywords=None, job_description=None):
    """Return { bullets:[{before, after}], numbers_needed:[str] } or None. Every
    `after` follows XYZ and contains a number OR the literal [NUMBER?]; the model
    never invents a value. `numbers_needed` lists the bullets awaiting a real
    figure from the user."""
    if not resume_text or not resume_text.strip():
        return None
    kw_line = ""
    if claimable_keywords:
        kws = ", ".join(str(k) for k in claimable_keywords[:12])
        kw_line = (
            f"\nWork in ONLY these already-true keywords where they fit, using the "
            f"JD's exact phrasing: {kws}\n"
        )
    jd_line = f"\n<job_description>\n{job_description[:2500]}\n</job_description>\n" if job_description else ""

    # Two-call HYBRID (your choice): Gemma crafts the XYZ bullets (judgment),
    # Flash-lite formats them to JSON. Step 1 — Gemma writes in prose.
    reason_prompt = (
        persona.SILVER_WOLF_SYSTEM
        + "\n\n<this_task>\n"
        "Rewrite the résumé's WEAK bullets (no metric, passive voice, or vague "
        "claims). " + _XYZ + "\n" + _NEVER_INVENT + " Every rewritten bullet must "
        "contain a number OR the literal [NUMBER?] placeholder — one line each, cut "
        "adjectives, cut 'successfully'. Keep bullets CLEAN and professional (no "
        "Silver Wolf slang inside a bullet — a recruiter reads these). For each weak "
        "bullet, write it as: ORIGINAL -> REWRITE. Then list, for every [NUMBER?] "
        "you left, what real figure the user must find. Rewrite at most the 10 "
        "weakest; leave already-strong bullets alone."
        + kw_line
        + jd_line
        + "\n</this_task>\n\n"
        f"<resume>\n{resume_text[:8000]}\n</resume>"
    )
    # Step 2 — Flash-lite structures the rewrites into strict JSON.
    format_instructions = (
        "Convert the bullet rewrites below into STRICT JSON — format only, change "
        "no wording, invent nothing, keep every [NUMBER?] token exactly as written.\n"
        "Return ONLY this JSON:\n"
        "{\n"
        '  "bullets": [{"before": "<original weak bullet, verbatim>", '
        '"after": "<the XYZ rewrite; number or [NUMBER?]>"}],\n'
        '  "numbers_needed": ["<for each [NUMBER?]: what real figure to find>"]\n'
        "}\n"
        "Cap bullets at 10."
    )
    data = _gemma_then_format(
        reason_prompt, format_instructions, reason_tokens=1600, format_tokens=2600,
        format_temp=0.1,
    )
    if not isinstance(data, dict):
        return None
    b = data.get("bullets")
    clean = []
    for item in (b if isinstance(b, list) else [])[:10]:
        if isinstance(item, dict) and item.get("after"):
            clean.append({"before": str(item.get("before", "")), "after": str(item["after"])})
    data["bullets"] = clean
    nn = data.get("numbers_needed")
    data["numbers_needed"] = [str(x) for x in nn][:12] if isinstance(nn, list) else []
    return data


# --- Agent 4: Hiring Manager --------------------------------------------------
# One skeptical interview question at a time. LITE tier (cheap, interactive).
# State (resume/JD/asked-count/last Q&A) is passed in each turn — NOT a growing
# transcript — to keep tokens flat across the 8 rounds.
_INTERVIEW_TOTAL = 8


def interview_next(resume_text, job_description, *, asked=0, last_q=None, last_a=None):
    """Drive the skeptical mock interview one turn at a time.

    Pass asked=0 (no last_q) for the opening question. On later turns pass the
    previous question + the user's answer to be graded. Returns:
      { grade:{score, landed, better} | None,   # None on the opening turn
        question:str | None,                     # None once done
        done:bool, verdict:str | None }          # verdict only on the final turn
    or None on failure. Only the LAST Q&A is sent back each turn (flat tokens)."""
    if not resume_text or not job_description:
        return None

    grading = ""
    if last_q and last_a is not None:
        grading = (
            "\nFirst GRADE their answer to your last question, then ask the next "
            "one (unless this was the 8th).\n"
            f"<your_last_question>{last_q[:500]}</your_last_question>\n"
            f"<their_answer>{str(last_a)[:800]}</their_answer>\n"
        )
    remaining = _INTERVIEW_TOTAL - asked
    is_last = remaining <= 1 and asked > 0

    prompt = (
        persona.SILVER_WOLF_SYSTEM
        + "\n\n<this_task>\n"
        "You are the SKEPTICAL hiring manager for the role below (Silver Wolf "
        "voice — sharp, unimpressed, but fair). Interview them one hard question "
        "at a time. Attack: every number on the résumé (make them prove it), any "
        "claim they can't obviously back up, and the weakest item / gap. Do NOT be "
        "encouraging. " + _NEVER_INVENT + "\n"
        f"This is question {asked + 1} of {_INTERVIEW_TOTAL}."
        + grading
        + ("\nThis is the FINAL turn: grade the last answer, then give a final "
           "hire/no-hire with the ONE reason that decided it. Set done=true and "
           "question=null.\n" if is_last else "\n")
        + "</this_task>\n\n"
        "Return ONLY this JSON:\n"
        "{\n"
        '  "grade": {"score": <int 0-10>, "landed": "<what worked, 1 line>", '
        '"better": "<what a strong candidate would have said, 1 line>"} '
        "| null (null only on the very first question),\n"
        '  "question": "<the next hard question>" | null,\n'
        '  "done": true|false,\n'
        '  "verdict": "<HIRE or NO HIRE + the one deciding reason>" | null\n'
        "}\n\n"
        f"<job_description>\n{job_description[:2500]}\n</job_description>\n\n"
        f"<resume>\n{resume_text[:6000]}\n</resume>"
    )
    data = gemma_client.ask_json_text(
        prompt, max_output_tokens=700, temperature=0.4, chain=gemma_client.LITE_CHAIN
    )
    if not isinstance(data, dict):
        return None
    data["done"] = bool(data.get("done")) or is_last
    if not isinstance(data.get("grade"), dict):
        data["grade"] = None
    return data
