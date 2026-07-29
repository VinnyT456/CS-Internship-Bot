"""Shared AI actions for the Score / Tailor buttons on a posting.

Both the posted-message buttons (main.py's jobsec:* handler) and the /latest
browser call these. Each fetches the clicking user's resume image + the posting
row, runs Gemma, and returns a ready-to-send ephemeral embed — or a "need a
resume" prompt when the user hasn't uploaded one.
"""

import asyncio
import logging

import discord

from commands import gemma_client, resume_utils
from commands.ai_commands import _answer_embed, _job_context

log = logging.getLogger("cs_internship_bot")


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


NEED_RESUME = "You need a resume first — upload one with `/resume upload`."


async def run_score(db, user, table, row_id):
    """Resume-vs-this-posting match. Returns (embed | None, error_text | None)."""
    img = await _resume_image(db, user)
    if not img:
        return None, NEED_RESUME

    row = await asyncio.to_thread(_fetch_row, db, table, row_id)
    if not row:
        return None, "That posting is no longer available."

    prompt = (
        "You are an ATS and technical recruiter. The image is a candidate's "
        "resume. Compare it against this posting and respond with:\n"
        "1. MATCH SCORE: a single 0-100 number with a one-line reason.\n"
        "2. STRONG MATCHES: aligning skills/experience (bullets).\n"
        "3. GAPS: top missing/weak requirements (bullets).\n"
        "4. QUICK WINS: 2-3 truthful resume tweaks to improve the match.\n\n"
        f"POSTING:\n{_job_context(row)}"
    )
    answer = await asyncio.to_thread(gemma_client.ask_with_image, img, prompt)
    if not answer:
        return None, "The AI couldn't score the match right now — try again later."

    title = f"🎯 Match — {row.get('job_title')} @ {row.get('company_name')}"
    return _answer_embed(title[:256], answer, discord.Color.gold()), None


async def run_tailor(db, user, table, row_id):
    """Tailor the resume for this posting. Returns (embed | None, error | None)."""
    img = await _resume_image(db, user)
    if not img:
        return None, NEED_RESUME

    row = await asyncio.to_thread(_fetch_row, db, table, row_id)
    if not row:
        return None, "That posting is no longer available."

    prompt = (
        "Act as an expert technical recruiter and resume writer. The image is "
        "the candidate's resume. Tailor it for the posting below. Respond with:\n"
        "1. GAP ANALYSIS: top 5 keywords/requirements from the posting that are "
        "missing or weak in the resume.\n"
        "2. REWRITTEN BULLETS: rewrite 3-5 experience bullets to match the "
        "posting's language and priorities. Stick strictly to the truth of the "
        "candidate's background — invent nothing.\n"
        "3. TAILORED SUMMARY: a punchy 3-4 sentence professional summary for "
        "this specific role.\n"
        "4. INTERVIEW TIPS: 2-3 things to emphasize for this role.\n\n"
        f"POSTING:\n{_job_context(row)}"
    )
    answer = await asyncio.to_thread(gemma_client.ask_with_image, img, prompt)
    if not answer:
        return None, "The AI couldn't tailor your resume right now — try again later."

    title = f"✍️ Tailored for {row.get('company_name')}"
    return _answer_embed(title[:256], answer, discord.Color.blurple()), None
