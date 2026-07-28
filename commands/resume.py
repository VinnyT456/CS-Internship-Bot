import asyncio
import logging

import discord

from commands import resume_utils


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
            f"✅ Resume uploaded — **{discord.utils.escape_markdown(file.filename)}**. "
            "It's ready for /match, /reviewresume, and recommendations.",
            ephemeral=True,
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
        await interaction.followup.send(
            "🗑️ Resume removed." if removed else "You have no resume to remove.",
            ephemeral=True,
        )

    bot.tree.add_command(group)
