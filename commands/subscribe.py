import asyncio
import logging

import discord

from commands import resume_utils


CATEGORY_CHOICES = [
    discord.app_commands.Choice(name="Any category", value=""),
    discord.app_commands.Choice(name="Software Engineering", value="Software Engineering"),
    discord.app_commands.Choice(name="AI / ML", value="AI / ML"),
    discord.app_commands.Choice(name="Data Science", value="Data Science"),
    discord.app_commands.Choice(name="Quant", value="Quant"),
    discord.app_commands.Choice(name="Product", value="Product"),
]


def _describe(sub):
    if sub.get("smart"):
        base = "**🎯 Smart match** (scored against your résumé)"
        cat = sub.get("category")
        return base + (f" · in **{cat}**" if cat else "")
    cat = sub.get("category") or "Any category"
    kw = sub.get("keyword")
    return f"**{cat}**" + (f" · keyword `{kw}`" if kw else "")


def register(bot, *, get_db, logger=None):
    log = logger or logging.getLogger("cs_internship_bot")

    async def _uuid(interaction):
        db = get_db()
        u = interaction.user
        return await asyncio.to_thread(
            db.get_or_create_user, u.id, u.name, u.display_name
        )

    @bot.tree.command(
        name="subscribe",
        description="Get DM'd when new matching internships are posted",
    )
    @discord.app_commands.describe(
        category="Category to follow (or Any)",
        keyword="Optional keyword matched against company/role",
        smart="Smart match: AI-score new roles against your résumé, DM only strong fits",
    )
    @discord.app_commands.choices(category=CATEGORY_CHOICES)
    async def subscribe(
        interaction: discord.Interaction,
        category: discord.app_commands.Choice[str] = None,
        keyword: str = None,
        smart: bool = False,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = get_db()
        uuid = await _uuid(interaction)
        if not uuid:
            await interaction.followup.send(
                "Couldn't set up your profile — try again later.", ephemeral=True
            )
            return

        cat = (category.value if category else "") or None
        kw = (keyword or "").strip() or None

        # A smart subscription needs a résumé to score against — if there isn't one,
        # prompt them to upload it now (the sub is still created; it just goes
        # live once a résumé exists).
        resume_note = ""
        if smart:
            has_resume = await asyncio.to_thread(resume_utils.get_resume, db, uuid)
            if not has_resume:
                resume_note = (
                    "\n\n📄 **Heads up:** smart match needs your résumé. Upload one "
                    "with `/resume upload` and I'll start matching you to new roles."
                )

        sub = await asyncio.to_thread(
            db.add_subscription, uuid, interaction.user.id, cat, kw, smart
        )
        if not sub:
            await interaction.followup.send(
                "Couldn't create the subscription — try again later.", ephemeral=True
            )
            return

        # Make sure DMs will actually reach them.
        note = ""
        try:
            await interaction.user.send(
                "🔔 You're subscribed to internship alerts! New matching roles "
                "will arrive here."
            )
        except discord.Forbidden:
            note = (
                "\n\n⚠️ I couldn't DM you — enable **direct messages** from server "
                "members so alerts can reach you."
            )

        await interaction.followup.send(
            f"🔔 Subscribed to {_describe(sub)}. I'll DM you new matches.{note}{resume_note}",
            ephemeral=True,
        )

    @bot.tree.command(
        name="unsubscribe",
        description="Manage or remove your internship alert subscriptions",
    )
    async def unsubscribe(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = get_db()
        uuid = await _uuid(interaction)
        subs = (
            await asyncio.to_thread(db.get_subscriptions, uuid) if uuid else []
        )
        if not subs:
            await interaction.followup.send(
                "You have no active subscriptions. Use `/subscribe` to add one.",
                ephemeral=True,
            )
            return

        view = _UnsubView(db, subs)
        await interaction.followup.send(
            "Your subscriptions — pick one to remove:",
            view=view,
            ephemeral=True,
        )


class _UnsubView(discord.ui.View):
    """A dropdown of the user's subscriptions; selecting one deletes it."""

    def __init__(self, db, subs):
        super().__init__(timeout=120)
        self.db = db

        options = [
            discord.SelectOption(
                label=(
                    ("🎯 Smart match" if s.get("smart")
                     else (s.get("category") or "Any category"))
                )[:100],
                description=(
                    "scored against your résumé" if s.get("smart")
                    else (f"keyword: {s['keyword']}" if s.get("keyword") else "no keyword")
                )[:100],
                value=s["id"],
            )
            for s in subs[:25]
        ]
        select = discord.ui.Select(placeholder="Choose a subscription to remove", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction):
        sub_id = interaction.data["values"][0]
        await asyncio.to_thread(self.db.delete_subscription, sub_id)
        await interaction.response.edit_message(
            content="🗑️ Subscription removed.", view=None
        )
