import logging
from collections import Counter

import discord


def _gather_stats(db):
    """Aggregate open-posting counts, top companies, and category breakdown
    across both tables. Paginates past Supabase's 1000-row cap."""

    def fetch_all(table):
        rows, off = [], 0
        while True:
            page = (
                db.supabase.table(table)
                .select("company_name,job_type,is_closed,job_posted_at")
                .range(off, off + 999)
                .execute()
                .data
                or []
            )
            rows.extend(page)
            if len(page) < 1000:
                return rows
            off += 1000

    stats = {}
    all_open = []
    for table in ("internships", "new_grads"):
        rows = fetch_all(table)
        open_rows = [r for r in rows if not r.get("is_closed")]
        stats[table] = {"total": len(rows), "open": len(open_rows)}
        all_open.extend(open_rows)

    companies = Counter(r["company_name"] for r in all_open if r.get("company_name"))
    categories = Counter(r["job_type"] for r in all_open if r.get("job_type"))
    latest = max(
        (r["job_posted_at"] for r in all_open if r.get("job_posted_at")),
        default=None,
    )

    return stats, companies, categories, latest


def register(bot, *, get_db, logger=None):
    """Register /stats — market trends and database statistics."""
    log = logger or logging.getLogger("cs_internship_bot")

    @bot.tree.command(
        name="stats",
        description="View internship market trends and database statistics",
    )
    async def stats(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        try:
            counts, companies, categories, latest = await __import__(
                "asyncio"
            ).to_thread(_gather_stats, get_db())
        except Exception:
            log.exception("Failed gathering stats")
            await interaction.followup.send("Couldn't load stats right now.")
            return

        total_open = counts["internships"]["open"] + counts["new_grads"]["open"]

        embed = discord.Embed(
            title="📊 Internship Market Stats",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🎓 Internships",
            value=f"```{counts['internships']['open']} open```",
            inline=True,
        )
        embed.add_field(
            name="💼 New Grad",
            value=f"```{counts['new_grads']['open']} open```",
            inline=True,
        )
        embed.add_field(
            name="🟢 Total Open",
            value=f"```{total_open}```",
            inline=True,
        )

        if companies:
            top = "\n".join(
                f"`{n}`  {c}" for c, n in companies.most_common(5)
            )
            embed.add_field(name="🏢 Top Companies", value=top, inline=False)

        if categories:
            cats = "\n".join(
                f"`{n:>3}`  {c}" for c, n in categories.most_common()
            )
            embed.add_field(name="💻 By Category", value=cats, inline=False)

        if latest:
            embed.add_field(name="🗓️ Newest Posting", value=str(latest), inline=True)

        embed.set_footer(text="CS Internship Bot")
        await interaction.followup.send(embed=embed)
