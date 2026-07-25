import discord
from discord.ext import commands


# Example internship data
internships = [
    {
        "company": "Google",
        "role": "Software Engineer Intern",
        "location": "Mountain View, CA",
        "category": "Software Engineering",
        "apply_url": "https://careers.google.com",
    },
    {
        "company": "NVIDIA",
        "role": "AI Software Intern",
        "location": "Santa Clara, CA",
        "category": "AI / ML",
        "apply_url": "https://nvidia.com/careers",
    },
    {
        "company": "Jane Street",
        "role": "Quantitative Research Intern",
        "location": "New York, NY",
        "category": "Quant",
        "apply_url": "https://janestreet.com",
    },
]


# Create Embed
def create_internship_embed(internship, index):

    embed = discord.Embed(
        title=internship["company"],
        description=f"**{internship['role']}**",
        color=discord.Color.blue(),
    )

    embed.add_field(name="📍 Location", value=internship["location"], inline=True)

    embed.add_field(name="💼 Category", value=internship["category"], inline=True)

    embed.set_footer(text=f"Internship {index + 1}/{len(internships)}")

    return embed


# Button View
class InternshipView(discord.ui.View):
    def __init__(self, internships):
        super().__init__(timeout=300)

        self.internships = internships
        self.index = 0

    # Previous Button
    @discord.ui.button(
        label="Previous", 
        emoji="⬅️", 
        style=discord.ButtonStyle.secondary
    )
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):

        self.index -= 1

        if self.index < 0:
            self.index = len(self.internships) - 1

        embed = create_internship_embed(self.internships[self.index], self.index)

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Apply",
        emoji="🚀",
        style=discord.ButtonStyle.success,
    )
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):

        internship = self.internships[self.index]

        await interaction.response.send_message(
            f"Application link: {internship['apply_url']}", ephemeral=True
        )

    # Next Button
    @discord.ui.button(label="Next", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):

        self.index += 1

        if self.index >= len(self.internships):
            self.index = 0

        embed = create_internship_embed(self.internships[self.index], self.index)

        await interaction.response.edit_message(embed=embed, view=self)


def register(bot):
    # Slash Command
    @bot.tree.command(
        name="latest",
        description="Browse the latest internship opportunities",
    )
    async def latest(interaction: discord.Interaction):

        embed = create_internship_embed(internships[0], 0)

        view = InternshipView(internships)

        await interaction.response.send_message(embed=embed, view=view)
