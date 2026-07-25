import logging

import discord


def register(bot, *, refresh_posted_embeds, logger=None):
    """Register /refreshembeds. Re-renders already-posted internship and
    new-grad messages in place with the current enriched embed.

    Needs refresh_posted_embeds(kind, progress) from the host module.
    """
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="refreshembeds",
        description="Re-render already-posted job messages with the latest enriched embeds",
    )
    @discord.app_commands.describe(
        limit="Max messages to edit this run (leave empty for all ~1300)",
        table="Restrict to one channel (default: both)",
    )
    @discord.app_commands.choices(
        table=[
            discord.app_commands.Choice(name="internships", value="internships"),
            discord.app_commands.Choice(name="new grads", value="new_grads"),
        ]
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def refreshembeds(
        interaction: discord.Interaction,
        limit: int = None,
        table: discord.app_commands.Choice[str] = None,
    ):
        # Editing every posted message takes minutes at the rate limit, well
        # past the 15-min interaction token, so we defer and drive progress
        # through followups instead of editing the original response.
        await interaction.response.defer(ephemeral=True, thinking=True)

        if limit is not None and limit <= 0:
            await interaction.followup.send("limit must be positive.", ephemeral=True)
            return

        async def progress(done, total, table_name):
            try:
                await interaction.followup.send(
                    f"…{done}/{total} processed (on {table_name})", ephemeral=True
                )
            except discord.HTTPException:
                pass

        try:
            stats = await refresh_posted_embeds(
                progress=progress,
                limit=limit,
                only_table=table.value if table else None,
            )
        except Exception:
            log.exception("refreshembeds failed")
            await interaction.followup.send(
                "Refresh failed — check the logs.", ephemeral=True
            )
            return

        await interaction.followup.send(
            "Done refreshing embeds.\n"
            f"• edited: **{stats['edited']}**\n"
            f"• missing (message deleted, id cleared): **{stats['skipped_missing']}**\n"
            f"• failed: **{stats['failed']}**\n"
            f"• total: **{stats['total']}**",
            ephemeral=True,
        )
