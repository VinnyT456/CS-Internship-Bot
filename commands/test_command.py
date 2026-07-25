import discord


def register(bot):
    """Register /test — dumps the caller's user attributes to stdout."""

    @bot.tree.command(
        name="test",
        description="command for testing purposes",
    )
    @discord.app_commands.default_permissions(administrator=True)
    async def test(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        print(f"{interaction.user}")
        print(f"{interaction.user.id}")
        print(f"{interaction.user.name}")
        print(f"{interaction.user.display_name}")
        print(f"{interaction.user.display_avatar.url}")
