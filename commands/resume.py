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
    """Populate the resume table with what the AI commands need — the mechanically
    extracted text and the builder-structured resume. Idempotent: skips fields
    already present unless force=True. Returns a dict of what's now populated.

    Text is extracted MECHANICALLY at upload (no AI, no hallucination); here we
    just resolve it (OCR-falling-back only for image-only PDFs) and derive the
    structured resume from that trusted text. That structured_json is what makes
    Tailor fast — it rewrites bullets instead of regenerating the whole resume.
    """
    status = {"text": False, "structured": False, "text_source": "none"}

    row = await asyncio.to_thread(resume_utils.get_resume, db, uuid) or {}
    if not row:
        log.warning("prime_resume: no resume row for %s", uuid)
        return status

    # 1. Trusted text — mechanical (stored at upload); OCR fallback only if the PDF
    #    had no text layer. NEVER a vision transcription of a text-based PDF.
    text = None if force else (row.get("extracted_text") or None)
    if text:
        status["text"] = True
        status["text_source"] = "pdf"
    else:
        text, source = await asyncio.to_thread(
            resume_utils.resolve_resume_text, db, uuid
        )
        status["text_source"] = source
        status["text"] = bool(text)

    if not text:
        log.warning("prime_resume: no text for %s (source=%s)", uuid, status["text_source"])
        return status

    # 2. Structured resume — derived from the trusted text (text-only, no image).
    have_structured = not force and isinstance(row.get("structured_json"), dict)
    if have_structured:
        status["structured"] = True
    else:
        structured = await _retry_async(
            resume_utils.parse_structured, text, None, label="parse_structured"
        )
        if structured:
            await asyncio.to_thread(resume_utils.store_structured, db, uuid, structured)
            status["structured"] = True

    # 3. Pre-tailor the newest few postings so the first Tailor click is instant.
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


def _github_voice(username, analyzed, total, picks):
    """Silver Wolf one-liner framing the GitHub scan result. Voice lives in this
    chat text ONLY — the attached report stays clean/professional (recruiter-
    facing). Falls back to a plain line if the AI is unavailable."""
    from commands import gemma_client, persona
    from commands.gemma_client import FAST_CHAIN

    scope = f"{analyzed} of {total}" if total > analyzed else f"{total}"
    prompt = (
        f"{persona.SILVER_WOLF_SYSTEM}\n\n"
        "Write ONE short Silver Wolf line (2 sentences max) telling the user you "
        f"scanned their GitHub (@{username}, {scope} public repos), picked their "
        f"strongest projects for the role ({picks}), and the full breakdown is in "
        "the attached file. Cocky, playful, on their side. Plain text, no markdown "
        "headers, no emoji spam. Facts only."
    )
    line = gemma_client.ask_text(prompt, FAST_CHAIN)
    if line and line.strip():
        return line.strip()[:1500]
    return (
        f"🐺 Scanned **@{username}** ({scope} repos). Pulled your best projects for "
        f"this role: {picks}. Full breakdown — scan, résumé-ready blurbs, an "
        "improvement plan, and a new-project idea — is in the attached file."
    )


# Back-compat alias for the background task.
_prime_resume = prime_resume


def register(bot, *, get_db, logger=None):
    """Register /resume — upload, view, or remove your resume (PDF only).

    The uploaded PDF is rendered to an image; the image is what the AI reads
    for /match, /resume analyze, etc.
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
            "⏳ Reading and indexing it for /match, /resume analyze, /tailor, and "
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

    @group.command(
        name="analyze",
        description="Run the 4-agent résumé pipeline: diagnose, match, rewrite, mock interview",
    )
    async def analyze(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from commands import resume_analyze

        db = get_db()
        uuid = await _user_uuid(interaction)
        if not uuid:
            await interaction.followup.send(
                "Couldn't set up your profile — try again later.", ephemeral=True
            )
            return

        text, source = await asyncio.to_thread(
            resume_utils.resolve_resume_text, db, uuid
        )
        if not text:
            await interaction.followup.send(
                "You need a resume first — upload one with `/resume upload` (a "
                "**text** PDF, not a scan).",
                ephemeral=True,
            )
            return

        try:
            await resume_analyze.start_analysis(
                interaction, db=db, uid=uuid, resume_text=text, text_source=source
            )
        except Exception:
            log.exception("resume analyze failed")
            await interaction.followup.send(
                "The résumé analyzer hit a snag — try again in a bit.", ephemeral=True
            )

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

    @group.command(
        name="github",
        description="Analyze your GitHub repos and pick the best projects for a role",
    )
    @discord.app_commands.describe(
        username="Your GitHub username or profile URL (saved for next time)",
        job="Target job description — paste text OR a message link. Optional.",
    )
    async def github_cmd(
        interaction: discord.Interaction,
        username: str = None,
        job: str = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from githubscan import repo_analyzer as ra
        from githubscan import report as gh_report

        db = get_db()
        uuid = await _user_uuid(interaction)
        if not uuid:
            await interaction.followup.send(
                "Couldn't set up your profile — try again later.", ephemeral=True
            )
            return

        # Username: arg > stored > résumé contact.github. Persist whatever we use.
        user_name = (username or "").strip()
        if user_name:
            user_name = ra._normalize_username(user_name)
            await asyncio.to_thread(db.set_github_username, uuid, user_name)
        else:
            user_name = await asyncio.to_thread(db.get_github_username, uuid)
            if not user_name:
                structured = await asyncio.to_thread(
                    resume_utils.get_structured, db, uuid
                )
                contact = (structured or {}).get("contact") or {}
                user_name = (contact.get("github") or "").strip() or None
                if user_name:
                    user_name = ra._normalize_username(user_name)
                    await asyncio.to_thread(db.set_github_username, uuid, user_name)
        if not user_name:
            await interaction.followup.send(
                "I don't have a GitHub username for you yet. Run it as "
                "`/resume github username:<your-handle>` — I'll remember it after that.",
                ephemeral=True,
            )
            return

        jd_text = (job or "").strip()
        if not jd_text:
            await interaction.followup.send(
                "No job description given, so I'll score your repos for a **generic "
                "software role**. For a tailored read, re-run with `job:` set to the "
                "posting text.",
                ephemeral=True,
            )
            jd_text = (
                "General software engineering / CS internship role: strong "
                "programming fundamentals, real projects, collaboration, and "
                "shipping working software."
            )

        token = ra.get_token()  # env only; never logged
        try:
            markdown, meta = await gh_report.build_report(user_name, jd_text, token)
        except Exception as exc:
            log.exception("github scan failed for %s", user_name)
            msg = str(exc)
            hint = (
                "Couldn't reach that GitHub profile. Double-check the username."
                if "404" in msg or "Not Found" in msg
                else "GitHub scan hit an error — try again in a bit."
            )
            await interaction.followup.send(hint, ephemeral=True)
            return

        if not markdown or not meta.get("shortlist"):
            await interaction.followup.send(
                f"Scanned **@{user_name}** but found no public, non-fork repos worth "
                "featuring. Push some original work and try again.",
                ephemeral=True,
            )
            return

        # Persist the ATS-extracted project records (best-effort — a DB hiccup
        # must not lose the report the user is waiting on).
        rows = meta.get("project_rows") or []
        if rows:
            try:
                await asyncio.to_thread(db.upsert_github_projects, uuid, rows)
            except Exception:
                log.exception("github: failed persisting project rows for %s", uuid)

        import io

        picks = ", ".join(f"**{s['name']}**" for s in meta["shortlist"][:5])
        intro = await asyncio.to_thread(
            _github_voice, user_name, meta["analyzed"], meta["total"], picks
        )
        file = discord.File(
            io.BytesIO(markdown.encode("utf-8")),
            filename=f"{user_name}-projects.md",
        )
        await interaction.followup.send(intro, file=file, ephemeral=True)

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
