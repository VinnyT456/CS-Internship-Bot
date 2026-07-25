import logging

import discord


def register(bot, logger=None):
    """Register /clearinternships — purges every message in the channel it's
    run in."""
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="clearinternships",
        description="Delete all messages in the channel this command is run in",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def clearinternships(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Run this command inside a text channel.")
            return

        try:
            # purge() bulk-deletes in chunks of 100 (messages <14 days) and
            # falls back to individual deletes for older ones.
            deleted = await channel.purge(limit=None)
        except discord.Forbidden:
            await interaction.followup.send(
                "I need the **Manage Messages** permission in this channel."
            )
            return
        except discord.HTTPException:
            log.exception("Failed purging channel %s", channel.id)
            await interaction.followup.send("Failed to delete messages.")
            return

        await interaction.followup.send(f"Deleted {len(deleted)} message(s).")
