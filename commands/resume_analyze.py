"""`/resume analyze` — the four-agent résumé pipeline as ONE evolving ephemeral
message the user walks through: Diagnose → (Match + Rewrite) → Mock interview.

Design (per the agreed plan):
  - ONE ephemeral embed that mutates in place; buttons drive the stages.
  - LAZY: each agent runs only when the user reaches its stage (one call per
    stage on demand — no eager fan-out), so a user who only wants the diagnosis
    pays for one agent, not four.
  - IN-MEMORY state only: the View instance holds everything (résumé text, JD,
    agent results, interview progress). Ephemeral + per-user, so no DB, no
    persistent-View plumbing. The View times out after 15 min like any ephemeral
    interaction; that's fine — the user re-runs `/resume analyze`.
  - The agents (resume_agents.py) already extract text MECHANICALLY and never
    invent numbers; this module is pure UI + orchestration, no model prompts of
    its own. Silver Wolf's voice lives only in the chat/embed CHROME here — the
    agents keep the artifact (bullets) clean.

Seeding (step 4): `AnalyzeView` accepts an optional `jd` + `jd_label` so the
Tailor button on an internship embed can drop the user straight into the
Match/Rewrite stages against that posting, skipping the JD modal.
"""

import asyncio
import logging

import discord

from commands import persona, resume_agents, resume_utils

log = logging.getLogger("cs_internship_bot")

_VIOLET = discord.Color.from_rgb(155, 109, 233)  # Silver Wolf's signature violet
_TIMEOUT = 900  # 15 min — matches Discord's ephemeral interaction lifetime

# Reason-code → short human gloss for the Diagnoser's screen-out lines.
_REASON_GLOSS = {
    "NO METRIC": "no number",
    "PASSIVE": "passive voice",
    "VAGUE": "too vague",
    "NO PROOF": "unbacked claim",
    "DUPLICATE": "repeat",
    "DATED": "stale",
}


def _sw_tag():
    """The Silver Wolf sign-off tag, or '' when the persona is toggled off."""
    return (getattr(persona, "SILVER_WOLF_TAG", "") or "").strip()


def _score_bar(score):
    """A 10-cell bar for a 0-100 score — quick visual for the diagnosis."""
    filled = max(0, min(10, round(score / 10)))
    return "█" * filled + "░" * (10 - filled)


# --- Embed builders (one per stage) ------------------------------------------

def _diagnose_embed(diag, text_source):
    e = discord.Embed(color=_VIOLET)
    if diag is None:
        e.title = "🩺 Résumé Diagnosis"
        e.description = (
            "The ATS scan came back empty — the AI service was busy. Hit "
            "**Re-run diagnosis** in a moment."
        )
        return e

    score = diag.get("score", 0)
    e.title = f"🩺 Résumé Diagnosis · {score}/100"
    head = f"`{_score_bar(score)}`  **{score}/100** — this is what the machine sees before a human ever does."
    if text_source == "ocr":
        head += "\n⚠️ Your PDF is **image-only** — real ATS software can't read it at all. Export a *text* PDF."
    top = diag.get("top_fix")
    if top:
        head += f"\n\n**Highest-leverage fix:** {top}"
    e.description = head[:4096]

    screen_out = diag.get("screen_out") or []
    if screen_out:
        lines = []
        for item in screen_out[:6]:
            reason = str(item.get("reason", "")).upper()
            gloss = _REASON_GLOSS.get(reason, reason.lower())
            line = str(item.get("line", "")).strip()
            lines.append(f"• `{line[:110]}` — *{gloss}*")
        e.add_field(name="🚩 Lines that get you screened out", value="\n".join(lines)[:1024], inline=False)

    missing = diag.get("missing_sections") or []
    if missing:
        e.add_field(name="📭 Sections a recruiter expects but can't find", value=", ".join(missing[:5])[:1024], inline=False)

    notes = diag.get("parse_notes") or []
    if notes:
        e.add_field(name="⚙️ What a parser garbles", value="\n".join(f"• {n}" for n in notes[:4])[:1024], inline=False)

    tag = _sw_tag()
    e.set_footer(text=(tag + " · Match to a job for the keyword read.") if tag else "Match to a job for the keyword read.")
    return e


def _match_embed(m):
    e = discord.Embed(title="🎯 Recruiter Match", color=_VIOLET)
    if m is None:
        e.description = "The recruiter read came back empty — try **Re-run** in a moment."
        return e

    outcomes = m.get("outcomes") or []
    if outcomes:
        e.description = "**This role is hired to produce:**\n" + "\n".join(f"• {o}" for o in outcomes[:3])

    kws = m.get("keywords") or []
    if kws:
        hit = [k for k in kws if k.get("in_resume")]
        miss = [k for k in kws if not k.get("in_resume")]
        val = ""
        if hit:
            val += "✅ " + ", ".join(k.get("kw", "") for k in hit[:10]) + "\n"
        if miss:
            val += "❌ " + ", ".join(k.get("kw", "") for k in miss[:10])
        if val:
            e.add_field(name="🔑 JD keywords", value=val[:1024], inline=False)

    coverable = m.get("coverable") or []
    if coverable:
        lines = []
        for c in coverable[:5]:
            kw = str(c.get("kw", "")).strip()
            proj = str(c.get("project", "")).strip()
            lines.append(f"• **{kw}** — covered by your `{proj}` project" if proj else f"• {kw}")
        e.add_field(
            name="🐙 Gaps your GitHub can cover",
            value=("Not on the résumé yet, but your repos prove it — put the project on:\n"
                   + "\n".join(lines))[:1024],
            inline=False,
        )

    missing_mh = m.get("missing_musthaves") or []
    if missing_mh:
        e.add_field(name="⛔ Must-haves you genuinely lack", value="\n".join(f"• {x}" for x in missing_mh[:5])[:1024], inline=False)
    return e


def _rewrite_embed(rw):
    e = discord.Embed(title="✍️ XYZ Rewrites", color=_VIOLET)
    if rw is None:
        e.description = "No rewrites came back — try **Re-run** in a moment."
        return e
    bullets = rw.get("bullets") or []
    if not bullets:
        e.description = "Your bullets are already tight — nothing weak enough to rewrite. Rare. Respect."
        return e

    e.description = (
        "Rewrote your weakest bullets with the **XYZ formula** "
        "*(Accomplished X, measured by Y, by doing Z)*. Where you owe a real "
        "number I left **`[NUMBER?]`** — fill it in yourself, I won't invent your metrics."
    )
    for i, b in enumerate(bullets[:5], 1):
        before = str(b.get("before", "")).strip()
        after = str(b.get("after", "")).strip()
        val = (f"~~{before[:180]}~~\n➜ **{after[:200]}**") if before else f"**{after[:200]}**"
        e.add_field(name=f"Bullet {i}", value=val[:1024], inline=False)

    nn = rw.get("numbers_needed") or []
    if nn:
        e.add_field(
            name="🔢 Numbers to hunt down",
            value="\n".join(f"• {n}" for n in nn[:5])[:1024],
            inline=False,
        )
    tag = _sw_tag()
    if tag:
        e.set_footer(text=tag)
    return e


def _interview_embed(view):
    """Renders the current mock-interview state from the View."""
    e = discord.Embed(title="🎤 Mock Interview", color=_VIOLET)
    total = resume_agents._INTERVIEW_TOTAL
    asked = view.iv_asked
    if view.iv_grades:
        avg = sum(view.iv_grades) / len(view.iv_grades)
        e.add_field(name="Running score", value=f"**{avg:.1f}/10** over {len(view.iv_grades)} answer(s)", inline=True)
    e.add_field(name="Progress", value=f"Question **{min(asked, total)}/{total}**", inline=True)

    if view.iv_last_grade:
        g = view.iv_last_grade
        e.add_field(
            name=f"Last answer · {g.get('score', '?')}/10",
            value=f"✅ {g.get('landed', '')}\n💡 {g.get('better', '')}"[:1024],
            inline=False,
        )
    if view.iv_verdict:
        e.description = f"**Verdict:** {view.iv_verdict}"
    elif view.iv_question:
        e.description = f"**Q{asked}.** {view.iv_question}"
    else:
        e.description = "Warming up the interrogation lamp…"
    return e


# --- The View (holds all state; drives the stages) ---------------------------

class AnalyzeView(discord.ui.View):
    """One ephemeral message, four agents, in-memory state.

    Stages: diagnose (rendered on open) → match+rewrite (JD-gated) → interview.
    """

    def __init__(self, *, user_id, db, uid, resume_text, text_source="pdf", jd=None, jd_label=None):
        super().__init__(timeout=_TIMEOUT)
        self.user_id = user_id
        self.db = db          # DB handle (for the structured résumé at PDF-build)
        self.uid = uid        # user UUID
        self.resume_text = resume_text
        self.text_source = text_source
        self.jd = (jd or "").strip() or None
        self.jd_label = jd_label

        # Cached agent results (None = not run yet).
        self.diag = None
        self.match = None
        self.rewrite = None

        # Interview state.
        self.iv_asked = 0
        self.iv_question = None
        self.iv_grades = []
        self.iv_last_grade = None
        self.iv_verdict = None
        self.iv_done = False

        self.message = None  # set by the caller after the first send

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This analysis isn't yours — run your own with `/resume analyze`.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def _refresh_anchor(self):
        """Re-render the anchor message (the diagnosis embed + current buttons).
        The anchor is a followup message, so we edit `self.message` directly."""
        if not self.message:
            return
        try:
            await self.message.edit(embed=self.diagnose_embed(), view=self)
        except Exception:
            pass

    # --- Button layout: rebuilt per stage so only valid actions show ---------
    def _build_buttons(self):
        self.clear_items()
        if not self.match:
            b = discord.ui.Button(label="Match to a job", emoji="📋", style=discord.ButtonStyle.primary)
            b.callback = self._on_match
            self.add_item(b)
        else:
            b = discord.ui.Button(label="Build tailored PDF", emoji="✨", style=discord.ButtonStyle.success)
            b.callback = self._on_build_pdf
            self.add_item(b)
        iv = discord.ui.Button(
            label="Mock interview" if not self.iv_asked else "Next question",
            emoji="🎤", style=discord.ButtonStyle.secondary,
        )
        iv.callback = self._on_interview
        iv.disabled = self.iv_done
        self.add_item(iv)

    # --- Stage 1: diagnosis (run at open) ------------------------------------
    async def run_diagnose(self):
        self.diag = await asyncio.to_thread(
            resume_agents.diagnose, self.resume_text, text_source=self.text_source
        )
        self._build_buttons()

    def diagnose_embed(self):
        return _diagnose_embed(self.diag, self.text_source)

    # --- Stage 2+3: match + rewrite (JD-gated; run together) -----------------
    async def _on_match(self, interaction):
        if not self.jd:
            await interaction.response.send_modal(_JDModal(self))
            return
        await self._run_match_and_rewrite(interaction)

    async def _run_match_and_rewrite(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        # Pull the user's GitHub projects (cache-first; live-scan against the JD
        # only when the cache is empty and we know a username). These give the
        # recruiter real evidence to cover JD gaps, and give the rewriter honest
        # extra keywords — NO extra model call: the projects fold into the
        # existing match/rewrite prompts.
        gh_projects = await self._resolve_github_projects()

        # Match first — its claimable keywords feed the rewrite (keyword-aware).
        self.match = await asyncio.to_thread(
            resume_agents.match, self.resume_text, self.jd, gh_projects
        )
        claimable = None
        if isinstance(self.match, dict):
            claimable = [c.get("kw") for c in (self.match.get("claimable") or []) if c.get("kw")]
            # Fold in the tech the GitHub projects prove, so the rewriter can
            # honestly work those keywords into bullets too.
            for p in (gh_projects or []):
                claimable.extend(str(t) for t in (p.get("tech") or [])[:6])
            # De-dupe while preserving order.
            seen = set()
            claimable = [k for k in claimable if k and not (k.lower() in seen or seen.add(k.lower()))]
        self.rewrite = await asyncio.to_thread(
            resume_agents.rewrite, self.resume_text, claimable, self.jd
        )
        self._build_buttons()
        # Deliver Match + Rewrite as two follow-up embeds; keep the diagnosis as
        # the anchor message (edited to carry the new button set).
        await self._refresh_anchor()
        await interaction.followup.send(embed=_match_embed(self.match), ephemeral=True)
        await interaction.followup.send(embed=_rewrite_embed(self.rewrite), ephemeral=True)

    async def _resolve_github_projects(self):
        """The user's GitHub projects as [{repo_name, tech, summary}], cache-first.

        Prefers github_projects already stored (from a prior /resume github or an
        earlier analyze). If the cache is empty but we can find a username (saved,
        else the résumé's contact.github), do ONE live scan against this JD — that
        both feeds this run and populates the cache for next time. Returns [] on
        any miss/failure; never raises (GitHub being down must not break analyze)."""
        try:
            cached = await asyncio.to_thread(self.db.get_github_projects, self.uid)
            if cached:
                return cached

            from githubscan import repo_analyzer as ra
            from githubscan import report as gh_report

            username = await asyncio.to_thread(self.db.get_github_username, self.uid)
            if not username:
                structured = await asyncio.to_thread(
                    resume_utils.get_structured, self.db, self.uid
                )
                contact = (structured or {}).get("contact") or {}
                username = (contact.get("github") or "").strip() or None
                if username:
                    username = ra._normalize_username(username)
                    await asyncio.to_thread(self.db.set_github_username, self.uid, username)
            if not username:
                return []

            token = ra.get_token()  # env only; never logged
            # Scan scored against THIS posting's JD so the persisted rows reflect it.
            _markdown, meta = await gh_report.build_report(
                username, self.jd or "General software engineering role", token
            )
            rows = (meta or {}).get("project_rows") or []
            if rows:
                await asyncio.to_thread(self.db.upsert_github_projects, self.uid, rows)
                return await asyncio.to_thread(self.db.get_github_projects, self.uid)
            return []
        except Exception:
            log.exception("analyze: GitHub project resolve failed")
            return []

    # --- Build tailored PDF (Rewriter → resume_service builder) --------------
    async def _on_build_pdf(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from commands import job_ai

        bullets = (self.rewrite or {}).get("bullets") or []
        if not bullets:
            await interaction.followup.send(
                "No rewritten bullets to build from — run **Match to a job** first.",
                ephemeral=True,
            )
            return
        embed, file, err = await job_ai.build_pdf_from_bullets(
            interaction, self.db, self.uid, bullets, self.jd_label
        )
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
        await interaction.followup.send(
            content="📄 Aether-Edited résumé, XYZ-rewritten and compiled. `[NUMBER?]` "
            "placeholders are yours to fill — I don't fabricate your metrics.",
            embed=embed, file=file, ephemeral=True,
        )

    # --- Stage 4: mock interview (sequential, 8 rounds) ----------------------
    async def _on_interview(self, interaction):
        if not self.jd:
            await interaction.response.send_modal(_JDModal(self, then_interview=True))
            return
        # First question: run it, then render. Subsequent: open the answer modal.
        if self.iv_asked == 0:
            await interaction.response.defer()
            await self._advance_interview(interaction, last_a=None)
        else:
            await interaction.response.send_modal(_AnswerModal(self))

    async def _advance_interview(self, interaction, *, last_a):
        data = await asyncio.to_thread(
            resume_agents.interview_next,
            self.resume_text, self.jd,
            asked=self.iv_asked,
            last_q=self.iv_question,
            last_a=last_a,
        )
        if not data:
            await interaction.followup.send(
                "The interviewer stepped out — the AI service was busy. Hit **Next question** again.",
                ephemeral=True,
            )
            return
        grade = data.get("grade")
        if isinstance(grade, dict):
            self.iv_last_grade = grade
            try:
                self.iv_grades.append(int(grade.get("score", 0)))
            except (TypeError, ValueError):
                pass
        if data.get("done"):
            self.iv_done = True
            self.iv_verdict = data.get("verdict")
            self.iv_question = None
        else:
            self.iv_question = data.get("question")
            self.iv_asked += 1
        self._build_buttons()
        await self._refresh_anchor()
        await interaction.followup.send(embed=_interview_embed(self), ephemeral=True)


# --- Modals ------------------------------------------------------------------

class _JDModal(discord.ui.Modal, title="Paste the job description"):
    jd = discord.ui.TextInput(
        label="Job description",
        style=discord.TextStyle.paragraph,
        placeholder="Paste the posting text — requirements, responsibilities, the works.",
        max_length=4000,
        required=True,
    )

    def __init__(self, view, *, then_interview=False):
        super().__init__()
        self._view = view
        self._then_interview = then_interview

    async def on_submit(self, interaction):
        self._view.jd = str(self.jd.value).strip() or None
        if not self._view.jd:
            await interaction.response.send_message("Empty JD — try again.", ephemeral=True)
            return
        if self._then_interview:
            await interaction.response.defer()
            await self._view._advance_interview(interaction, last_a=None)
        else:
            await self._view._run_match_and_rewrite(interaction)


class _AnswerModal(discord.ui.Modal, title="Your answer"):
    answer = discord.ui.TextInput(
        label="Answer the interviewer",
        style=discord.TextStyle.paragraph,
        placeholder="Make it land. They're skeptical.",
        max_length=1500,
        required=True,
    )

    def __init__(self, view):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction):
        await interaction.response.defer()
        await self._view._advance_interview(interaction, last_a=str(self.answer.value))


# --- Entry point: build + send the analysis (used by /resume analyze + Tailor) --

async def start_analysis(interaction, *, db, uid, resume_text, text_source="pdf",
                         jd=None, jd_label=None, seed_match=False):
    """Run Stage 1 (diagnose) and send the anchored ephemeral message with the
    stage buttons. When `seed_match` is set (Tailor button path) and a JD is
    supplied, also fire Match+Rewrite immediately so the user lands on the
    keyword read for that posting. `interaction` must already be deferred
    (ephemeral). Returns the AnalyzeView."""
    view = AnalyzeView(
        user_id=interaction.user.id, db=db, uid=uid, resume_text=resume_text,
        text_source=text_source, jd=jd, jd_label=jd_label,
    )
    await view.run_diagnose()
    msg = await interaction.followup.send(embed=view.diagnose_embed(), view=view, ephemeral=True)
    view.message = msg
    if seed_match and view.jd:
        # Reuse the same interaction's followup channel for the match/rewrite
        # embeds (the anchor message is already sent; edits target it).
        try:
            await view._run_match_and_rewrite(interaction)
        except Exception:
            log.exception("start_analysis: seeded match/rewrite failed")
    return view
