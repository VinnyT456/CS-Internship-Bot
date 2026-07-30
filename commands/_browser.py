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
            row_id = self._row().get("id")
            if action == "score":
                embed, file, view, error = await job_ai.run_score(
                    self.get_db(), interaction.user, self._row_table(), row_id
                )
                if error:
                    await interaction.followup.send(error, ephemeral=True)
                    return
                kwargs = {"ephemeral": True}
                if embed is not None:
                    kwargs["embed"] = embed
                if file is not None:
                    kwargs["file"] = file
                if view is not None:
                    kwargs["view"] = view
                await interaction.followup.send(**kwargs)
                return

            progress = job_ai.make_progress_updater(interaction)
            embed, file, view, error = await job_ai.run_tailor(
                self.get_db(), interaction.user, self._row_table(), row_id,
                progress=progress,
            )
            if error:
                await interaction.edit_original_response(
                    content=error, embed=None, view=None
                )
                return
            await job_ai.deliver_tailor(interaction, embed, file, view)

        button.callback = cb
        return button
