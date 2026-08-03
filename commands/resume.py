import asyncio
import logging

import discord

from commands import resume_utils

log = logging.getLogger("cs_internship_bot")


async def _retry_async(fn, *args, tries=3, delay=3.0, label=""):
    """Run a blocking fn via a thread, retrying on empty/None result — the free
    Gemma tier returns None under load, so one attempt often isn't enough."""
    for attempt in range(tries):
        try:
            result = await asyncio.to_thread(fn, *args)
        except Exception:
            log.exception("prime step %s attempt %d raised", label, attempt + 1)
            result = None
        if result:
            return result
        if attempt < tries - 1:
            await asyncio.sleep(delay * (attempt + 1))
    log.warning("prime step %s produced nothing after %d tries", label, tries)
    return None


async def prime_resume(db, uuid, *, force=False):
    """Populate the resume table with everything the AI commands need — extracted
    text, the builder-structured resume, and the precomputed review — retrying
    through transient Gemma failures. Idempotent: skips fields already present
    unless force=True. Returns a dict of what's now populated.

    This is what makes Tailor fast: with structured_json present, Tailor only
    rewrites bullets instead of regenerating the whole resume.
    """
    from commands import ai_commands

    status = {"text": False, "structured": False, "review": False}
    img = await asyncio.to_thread(resume_utils.image_bytes, db, uuid)
    if not img:
        log.warning("prime_resume: no resume image for %s", uuid)
        return status

    row = await asyncio.to_thread(resume_utils.get_resume, db, uuid) or {}

    # 1. Extracted text — the basis for the fast text-only path.
    text = None if force else (row.get("extracted_text") or None)
    if text:
        status["text"] = True
    else:
        text = await _retry_async(resume_utils.extract_text, img, label="extract_text")
        if text:
            await asyncio.to_thread(resume_utils.store_text, db, uuid, text)
            status["text"] = True

    # Steps 2 and 3 both depend only on the text/image from step 1, not on each
    # other — run them CONCURRENTLY so the resume is "ready" sooner.
    have_structured = not force and isinstance(row.get("structured_json"), dict)
    have_review = not force and isinstance(row.get("review_json"), dict)

    async def _do_structured():
        # 2. Structured resume — enables the fast bullet-only Tailor path.
        if have_structured:
            return True
        structured = await _retry_async(
            resume_utils.parse_structured, text, img, label="parse_structured"
        )
        if structured:
            await asyncio.to_thread(resume_utils.store_structured, db, uuid, structured)
            return True
        return False

    async def _do_review():
        # 3. Precomputed review — makes /reviewresume instant.
        if have_review:
            return True
        review = await _retry_async(
            ai_commands.compute_review, text, img, label="compute_review"
        )
        if review:
            await asyncio.to_thread(resume_utils.store_review, db, uuid, review)
            return True
        return False

    status["structured"], status["review"] = await asyncio.gather(
        _do_structured(), _do_review()
    )

    # 4. Pre-tailor the newest few postings so the first Tailor click is instant.
    if status["structured"] or status["text"]:
        await _pretailor_newest(db, uuid, n=3)

    log.info("prime_resume %s -> %s", uuid, status)
    return status


async def _pretailor_newest(db, uuid, n=3):
    """Background: tailor + cache the newest N open internships for this user."""
    from commands import job_ai

    try:
        rows = (
            db.supabase.table("internships")
            .select("id")
            .eq("is_closed", False)
            .not_.is_("job_summary", "null")
            .order("job_posted_at", desc=True)
            .limit(n)
            .execute()
            .data
            or []
        )
    except Exception:
        log.exception("pretailor: failed listing newest internships")
        return
    async def _one(row_id):
        try:
            await job_ai.pretailor_job(db, uuid, "internships", row_id)
        except Exception:
            log.exception("pretailor job %s failed", row_id)

    # Pre-tailor the newest N concurrently — each is an independent AI job.
    await asyncio.gather(*(_one(r["id"]) for r in rows))


# Back-compat alias for the background task.
_prime_resume = prime_resume


def register(bot, *, get_db, logger=None):
    """Register /resume — upload, view, or remove your resume (PDF only).

    The uploaded PDF is rendered to an image; the image is what the AI reads
    for /match, /reviewresume, etc.
    """
    log = logger or logging.getLogger("cs_internship_bot")

    group = discord.app_commands.Group(
        name="resume", description="Upload and manage your resume"
    )

    async def _user_uuid(interaction):
        db = get_db()
        user = interaction.user
        return await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )

    @group.command(name="upload", description="Upload a PDF resume (replaces any existing one)")
    @discord.app_commands.describe(file="Your resume as a PDF")
    async def upload(interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True, thinking=True)

        name = (file.filename or "").lower()
        if not name.endswith(".pdf") and file.content_type != "application/pdf":
            await interaction.followup.send(
                "Only **PDF** files are accepted. Export your resume as a PDF and "
                "try again.",
                ephemeral=True,
            )
            return

        db = get_db()
        uuid = await _user_uuid(interaction)
        if not uuid:
            await interaction.followup.send(
                "Couldn't set up your profile — try again later.", ephemeral=True
            )
            return

        try:
            pdf_bytes = await file.read()
            await asyncio.to_thread(
                resume_utils.process_and_store, db, uuid, pdf_bytes, file.filename
            )
            # Cache invalidation moved off the reply path — it's done in the
            # background prime below so the "uploaded" ack lands sooner.
        except resume_utils.ResumeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception("Resume upload failed")
            await interaction.followup.send(
                "Something went wrong processing your resume.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ Resume uploaded — **{discord.utils.escape_markdown(file.filename)}**.\n"
            "⏳ Reading and indexing it for /match, /reviewresume, /tailor, and "
            "recommendations… this can take a minute — you'll get a note when it's "
            "ready.",
            ephemeral=True,
        )

        # Prime in the background so the reply isn't blocked. When it finishes we
        # DM-style follow up with the result so the user knows it's ready (and
        # which parts, if the AI was flaky).
        async def _prime_and_report():
            # New resume invalidates any cached match scores and tailors — do it
            # here (concurrently) instead of blocking the upload ack.
            await asyncio.gather(
                asyncio.to_thread(db.clear_score_cache, uuid),
                asyncio.to_thread(db.clear_tailor_cache, uuid),
            )
            status = await prime_resume(db, uuid, force=True)
            ready = all(status.values())
            if ready:
                msg = "🎉 Your resume is fully ready — /tailor and /match will be fast now."
            else:
                done = [k for k, v in status.items() if v]
                missing = [k for k, v in status.items() if not v]
                msg = (
                    "⚠️ Your resume is partly ready"
                    f" (done: {', '.join(done) or 'none'};"
                    f" retry later for: {', '.join(missing)}). "
                    "The AI service was busy — commands still work, just slower. "
                    "Re-run `/resume upload` or try again in a bit to finish indexing."
                )
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                log.exception("Failed sending prime-complete note")

        asyncio.create_task(_prime_and_report())

    @group.command(name="view", description="See your current resume")
    async def view(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = get_db()
        uuid = await _user_uuid(interaction)
        row = (
            await asyncio.to_thread(resume_utils.get_resume, db, uuid) if uuid else None
        )
        if not row:
            await interaction.followup.send(
                "You haven't uploaded a resume yet. Use `/resume upload`.",
                ephemeral=True,
            )
            return

        img = await asyncio.to_thread(resume_utils.image_bytes, db, uuid)
        embed = discord.Embed(
            title="📄 Your Resume",
            description=(
                f"**{discord.utils.escape_markdown(row['original_filename'])}**\n"
                f"Uploaded {row.get('uploaded_at', '')[:10]}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="CS Internship Bot")

        if img:
            import io

            file = discord.File(io.BytesIO(img), filename="resume.png")
            embed.set_image(url="attachment://resume.png")
            await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="delete", description="Remove your resume")
    async def delete(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = get_db()
        uuid = await _user_uuid(interaction)
        removed = (
            await asyncio.to_thread(resume_utils.delete_resume, db, uuid)
            if uuid
            else False
        )
        if removed:
            await asyncio.to_thread(db.clear_score_cache, uuid)
            await asyncio.to_thread(db.clear_tailor_cache, uuid)
        await interaction.followup.send(
            "🗑️ Resume removed." if removed else "You have no resume to remove.",
            ephemeral=True,
        )

    bot.tree.add_command(group)
