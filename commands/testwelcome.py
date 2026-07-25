import logging
import discord


def register(bot, send_welcome, logger=None):
    """Register /testwelcome. Needs send_welcome(member) from the host."""
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="testwelcome",
        description="Preview the Silver Wolf welcome message (uses you as the new member)",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def testwelcome(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            await send_welcome(interaction.user)
        except Exception:
            log.exception("Failed sending test welcome message")
            await interaction.followup.send("Failed to send welcome message.")
            return

        await interaction.followup.send("Welcome message fired. Check the channel.")
