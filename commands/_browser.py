"""Shared paginated job browser used by /latest and /search.

A JobBrowser renders one posting at a time in the full rich embed (always
expanded) with a single button row: Prev · Apply · Score · Tailor · Next. All
buttons are browse-owned (in-view callbacks) so a click never disturbs the
view. Per-invocation, 5-minute timeout.

Score/Tailor run the real AI when a get_db is passed; otherwise they show a
"coming soon" note.
"""

import discord

from commands import job_ai


class JobBrowser(discord.ui.View):
    def __init__(self, rows, build_embed, kind_type, footer_prefix, get_db=None, table="internships"):
        super().__init__(timeout=300)
        self.rows = rows
        self.build_embed = build_embed
        self.kind_type = kind_type        # "internship" | "new grad" (embed Type)
        self.footer_prefix = footer_prefix  # e.g. "Internship" | "Result"
        self.get_db = get_db
        self.table = table
        self.index = 0
        self._sync_buttons()

    def _row(self):
        return self.rows[self.index]

    def _row_table(self):
        # Saved rows carry their own table; others use the browser's default.
        return self._row().get("_job_table", self.table)

    def embed(self):
        embed = self.build_embed(self._row(), self.kind_type, expanded=True)
        embed.set_footer(
            text=f"{self.footer_prefix} {self.index + 1} of {len(self.rows)}"
            "  •  CS Internship Bot"
        )
        return embed

    def _sync_buttons(self):
        for item in list(self.children):
            self.remove_item(item)

        self.add_item(self._nav("Prev", "◀", -1, discord.ButtonStyle.secondary))

        url = self._row().get("job_url")
        if url:
            self.add_item(
                discord.ui.Button(
                    label="Apply", emoji="🟢",
                    style=discord.ButtonStyle.link, url=url, row=0,
                )
            )

        self.add_item(self._ai("Score", "📊", "score"))
        self.add_item(self._ai("Tailor", "✍️", "tailor"))
        self.add_item(self._nav("Next", "▶", 1, discord.ButtonStyle.primary))

    def _nav(self, label, emoji, step, style):
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=0)

        async def cb(interaction):
            self.index = (self.index + step) % len(self.rows)
            self._sync_buttons()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        button.callback = cb
        return button

    def _ai(self, label, emoji, action):
        button = discord.ui.Button(
            label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=0
        )

        async def cb(interaction):
            if self.get_db is None:
                await interaction.response.send_message(
                    f"{emoji} {label} is coming soon.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            runner = job_ai.run_score if action == "score" else job_ai.run_tailor
            row_id = self._row().get("id")
            embed, file, error = await runner(
                self.get_db(), interaction.user, self._row_table(), row_id
            )
            if error:
                await interaction.followup.send(error, ephemeral=True)
            elif file is not None:
                await interaction.followup.send(
                    embed=embed, file=file, ephemeral=True
                )
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)

        button.callback = cb
        return button
