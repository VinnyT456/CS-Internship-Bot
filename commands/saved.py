import logging

import discord

from commands._browser import JobBrowser


class SavedBrowser(JobBrowser):
    """The job browser with an extra Unsave button — removes the current
    posting from the user's saved list and drops it from the view."""

    def __init__(self, rows, build_embed, unsave, get_db):
        self.unsave = unsave  # async (user, job_table, job_id) -> None
        super().__init__(
            rows, build_embed, "internship", "Saved", get_db=get_db, table="internships"
        )

    def _sync_buttons(self):
        super()._sync_buttons()
        # Append Unsave (row 0 already has 5; put Unsave on row 1).
        button = discord.ui.Button(
            label="Unsave", emoji="🗑️", style=discord.ButtonStyle.danger, row=1
        )

        async def cb(interaction):
            row = self._row()
            await self.unsave(
                interaction.user, row.get("_job_table", "internships"), row["id"]
            )
            # Drop it locally and re-render (or close if it was the last one).
            del self.rows[self.index]
            if not self.rows:
                await interaction.response.edit_message(
                    content="You have no more saved jobs.", embed=None, view=None
                )
                return
            self.index %= len(self.rows)
            self._sync_buttons()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        button.callback = cb
        self.add_item(button)


def register(bot, *, build_embed, get_db, logger=None):
    """Register /saved — view and manage the caller's saved internships."""
    log = logger or logging.getLogger("cs_internship_bot")

    async def unsave(user, job_table, job_id):
        import asyncio

        db = get_db()
        uid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        if uid:
            await asyncio.to_thread(db.toggle_saved_job, uid, job_table, int(job_id))

    @bot.tree.command(
        name="saved",
        description="View and manage your saved internships",
    )
    async def saved(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        import asyncio

        db = get_db()
        user = interaction.user
        uid = await asyncio.to_thread(
            db.get_or_create_user, user.id, user.name, user.display_name
        )
        rows = (
            await asyncio.to_thread(db.get_saved_jobs, uid) if uid else []
        )

        if not rows:
            await interaction.followup.send(
                "You haven't saved any jobs yet. Hit 🔖 **Save** on a posting to "
                "add it here.",
                ephemeral=True,
            )
            return

        view = SavedBrowser(rows, build_embed, unsave, get_db)
        try:
            await interaction.followup.send(
                embed=view.embed(), view=view, ephemeral=True
            )
        except Exception:
            log.exception("Failed sending /saved")
            await interaction.followup.send(
                "Something went wrong loading your saved jobs.", ephemeral=True
            )
