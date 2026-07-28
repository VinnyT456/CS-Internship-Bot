import logging

import discord

from commands._browser import JobBrowser


LATEST_LIMIT = 10


def _fetch_latest(db, table, limit=LATEST_LIMIT):
    """The newest open, enriched rows from `table` (internships | new_grads) —
    joined with company_info so the rich embed has logo/website/linkedin.
    Newest posted first."""
    try:
        response = (
            db.supabase.table(table)
            .select("*, company_info(*)")
            .eq("is_closed", False)
            .not_.is_("job_summary", "null")
            .order("job_posted_at", desc=True)
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception:
        logging.getLogger("cs_internship_bot").exception("Failed fetching latest")
        return []


def register(bot, *, build_embed, get_db, logger=None):
    """Register /latest — browse the newest postings in the rich embed.

    build_embed(row, type, expanded=...) -> discord.Embed
    get_db() -> SupabaseDatabase
    """
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="latest",
        description="Browse the latest internship or new-grad opportunities",
    )
    @discord.app_commands.describe(type="Which roles to browse")
    @discord.app_commands.choices(
        type=[
            discord.app_commands.Choice(name="Internships", value="internships"),
            discord.app_commands.Choice(name="New Grad", value="new_grads"),
        ]
    )
    async def latest(
        interaction: discord.Interaction,
        type: discord.app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        table = type.value if type else "internships"
        kind_type = "new grad" if table == "new_grads" else "internship"
        prefix = "New Grad" if table == "new_grads" else "Internship"
        empty_word = "new-grad roles" if table == "new_grads" else "internships"

        rows = _fetch_latest(get_db(), table)
        if not rows:
            await interaction.followup.send(
                f"No {empty_word} available right now — check back soon."
            )
            return

        view = JobBrowser(rows, build_embed, kind_type, prefix)
        try:
            await interaction.followup.send(embed=view.embed(), view=view)
        except Exception:
            log.exception("Failed sending /latest")
            await interaction.followup.send("Something went wrong loading results.")
