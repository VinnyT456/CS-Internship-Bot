import discord


# Grouped for the help embed. Keep in sync with the registered commands.
HELP_GROUPS = (
    (
        "🔍 Browse & Search",
        [
            ("/latest", "Browse the newest internship or new-grad postings"),
            ("/search", "Search by keyword, company, role, or location"),
            ("/stats", "Market trends and database statistics"),
        ],
    ),
    (
        "🔖 Your Stuff",
        [
            ("/saved", "View and manage your saved internships"),
            ("/profile", "Your saved jobs, resume, and preferences  *(soon)*"),
            ("/resume", "Upload or manage your resume  *(soon)*"),
        ],
    ),
    (
        "🤖 AI Tools",
        [
            ("/match", "Score your resume against a posting  *(soon)*"),
            ("/reviewresume", "AI feedback on your resume  *(soon)*"),
            ("/recommend", "Personalized recommendations  *(soon)*"),
            ("/interview", "Practice interview questions  *(soon)*"),
            ("/helpme", "Ask the AI career assistant  *(soon)*"),
        ],
    ),
    (
        "🔔 Alerts & More",
        [
            ("/subscribe", "Get alerts for matching jobs  *(soon)*"),
            ("/resources", "Curated learning resources  *(soon)*"),
        ],
    ),
)


def register(bot, **_):
    """Register /help — list all commands."""

    @bot.tree.command(
        name="help",
        description="View all available commands and how to use them",
    )
    async def help_cmd(interaction: discord.Interaction):
        embed = discord.Embed(
            title="🤖 CS Internship Bot — Commands",
            description=(
                "Fresh CS internship and new-grad postings, auto-updated every "
                "15 minutes. Commands marked *(soon)* are on the way."
            ),
            color=discord.Color.blurple(),
        )
        for name, items in HELP_GROUPS:
            value = "\n".join(f"**{cmd}** — {desc}" for cmd, desc in items)
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="CS Internship Bot")

        await interaction.response.send_message(embed=embed, ephemeral=True)
