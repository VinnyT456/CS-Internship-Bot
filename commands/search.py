import logging

import discord

from commands._browser import JobBrowser


SEARCH_LIMIT = 25


def _search(db, table, query, category=None, limit=SEARCH_LIMIT):
    """Open, enriched rows in `table` matching `query` across company / title /
    location (case-insensitive substring), optionally filtered to a category.
    Newest first."""
    like = f"%{query}%"
    try:
        q = (
            db.supabase.table(table)
            .select("*, company_info(*)")
            .eq("is_closed", False)
            .not_.is_("job_summary", "null")
            .or_(
                f"company_name.ilike.{like},"
                f"job_title.ilike.{like},"
                f"job_location.ilike.{like}"
            )
        )
        if category:
            q = q.eq("job_type", category)
        response = q.order("job_posted_at", desc=True).limit(limit).execute()
        return response.data or []
    except Exception:
        logging.getLogger("cs_internship_bot").exception("Search failed")
        return []


CATEGORY_CHOICES = [
    discord.app_commands.Choice(name="Software Engineering", value="Software Engineering"),
    discord.app_commands.Choice(name="AI / ML", value="AI / ML"),
    discord.app_commands.Choice(name="Data Science", value="Data Science"),
    discord.app_commands.Choice(name="Quant", value="Quant"),
    discord.app_commands.Choice(name="Product", value="Product"),
]


def register(bot, *, build_embed, get_db, logger=None):
    """Register /search — keyword search over postings, shown in the shared
    paginated browser."""
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="search",
        description="Search internships by keyword, company, role, or location",
    )
    @discord.app_commands.describe(
        query="What to search for (company, role, or location)",
        type="Internships or new-grad roles",
        category="Optionally narrow to a category",
    )
    @discord.app_commands.choices(
        type=[
            discord.app_commands.Choice(name="Internships", value="internships"),
            discord.app_commands.Choice(name="New Grad", value="new_grads"),
        ],
        category=CATEGORY_CHOICES,
    )
    async def search(
        interaction: discord.Interaction,
        query: str,
        type: discord.app_commands.Choice[str] = None,
        category: discord.app_commands.Choice[str] = None,
    ):
        await interaction.response.defer(thinking=True)

        table = type.value if type else "internships"
        kind_type = "new grad" if table == "new_grads" else "internship"
        cat = category.value if category else None

        rows = _search(get_db(), table, query, cat)
        if not rows:
            await interaction.followup.send(
                f"No matches for **{discord.utils.escape_markdown(query)}**"
                f"{f' in {cat}' if cat else ''}. Try a broader search."
            )
            return

        view = JobBrowser(
            rows, build_embed, kind_type, "Result", get_db=get_db, table=table
        )
        try:
            await interaction.followup.send(
                content=f"🔎 **{len(rows)}** result(s) for "
                f"**{discord.utils.escape_markdown(query)}**",
                embed=view.embed(),
                view=view,
            )
        except Exception:
            log.exception("Failed sending /search")
            await interaction.followup.send("Something went wrong running the search.")
