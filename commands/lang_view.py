"""A reusable language-toggle View: EN ⇄ 中文.

Wraps a builder that renders an embed for a given language. The button flips the
language and re-renders in place.

Discord DROPS a message's attachments on any edit that does not re-supply them,
so an embed thumbnail sourced from `attachment://name.png` disappears the moment
the language is toggled. To keep it, pass `make_file` — a zero-arg callable that
returns a FRESH discord.File each call (a File is single-use). The view re-sends
it via `attachments=` on every edit so the thumbnail survives the toggle.
"""

import discord


class LangToggleView(discord.ui.View):
    """A view with a single 🌐 button that flips the embed between English and
    Chinese. `build(lang)` returns a discord.Embed for lang in {"en", "zh"}.
    `make_file` (optional) returns a fresh discord.File to re-attach on each edit
    so an `attachment://` thumbnail is not lost. Extra buttons can be supplied
    via `extra_items` (added after the toggle)."""

    def __init__(self, build, lang="en", *, timeout=1800, extra_items=None, make_file=None):
        super().__init__(timeout=timeout)
        self._build = build
        self._make_file = make_file
        self.lang = lang
        self._toggle = discord.ui.Button(
            label="🌐 中文" if lang == "en" else "🌐 English",
            style=discord.ButtonStyle.secondary,
        )
        self._toggle.callback = self._on_toggle
        self.add_item(self._toggle)
        for item in (extra_items or []):
            self.add_item(item)

    def embed(self):
        return self._build(self.lang)

    async def _on_toggle(self, interaction: discord.Interaction):
        self.lang = "zh" if self.lang == "en" else "en"
        self._toggle.label = "🌐 中文" if self.lang == "en" else "🌐 English"
        kwargs = {"embed": self.embed(), "view": self}
        if self._make_file is not None:
            f = self._make_file()
            if f is not None:
                # Re-supply the attachment so the embed thumbnail is not dropped.
                kwargs["attachments"] = [f]
        await interaction.response.edit_message(**kwargs)
