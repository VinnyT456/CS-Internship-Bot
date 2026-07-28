import asyncio
import logging

import discord

from commands import resume_utils


def register(bot, *, get_db, logger=None):
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="profile",
        description="View your saved jobs, resume, and alert subscriptions",
    )
    async def profile(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = get_db()
        user = interaction.user

        def gather():
            uid = db.get_or_create_user(user.id, user.name, user.display_name)
            if not uid:
                return None
            return {
                "saved": db.get_saved_jobs(uid),
                "resume": resume_utils.get_resume(db, uid),
                "subs": db.get_subscriptions(uid),
            }

        data = await asyncio.to_thread(gather)
        if data is None:
            await interaction.followup.send(
                "Couldn't load your profile — try again later.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"👤 {user.display_name}'s Profile",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        # Resume
        resume = data["resume"]
        if resume:
            embed.add_field(
                name="📄 Resume",
                value=f"`{resume['original_filename']}`\nUploaded {resume.get('uploaded_at', '')[:10]}",
                inline=True,
            )
        else:
            embed.add_field(
                name="📄 Resume",
                value="*None — upload with* `/resume upload`",
                inline=True,
            )

        # Saved
        saved = data["saved"]
        embed.add_field(
            name="🔖 Saved Jobs",
            value=f"**{len(saved)}** saved" + ("\n*View with* `/saved`" if saved else ""),
            inline=True,
        )

        # Subscriptions
        subs = data["subs"]
        if subs:
            lines = []
            for s in subs[:5]:
                cat = s.get("category") or "Any"
                kw = f" · `{s['keyword']}`" if s.get("keyword") else ""
                lines.append(f"• {cat}{kw}")
            more = f"\n*+{len(subs) - 5} more*" if len(subs) > 5 else ""
            embed.add_field(
                name="🔔 Alerts",
                value="\n".join(lines) + more,
                inline=False,
            )
        else:
            embed.add_field(
                name="🔔 Alerts",
                value="*None — set up with* `/subscribe`",
                inline=False,
            )

        embed.set_footer(text="CS Internship Bot")
        await interaction.followup.send(embed=embed, ephemeral=True)
