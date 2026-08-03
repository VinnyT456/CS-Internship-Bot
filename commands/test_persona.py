"""Admin test commands to preview the Silver Wolf embeds + voice.

Two kinds:
  • FAKE-DATA previews (instant, no AI, no DB) — see the embed layout + voice
    baked into the sample data / prompts:
      /testscore       → Score embed (EN/中文 toggle, wheel, Level-Up Plan)
      /testtailor      → Tailor preview + Résumé Review together (both EN/中文)
      /testguide       → command guide (EN/中文 toggle)
      /testscore_layout → instant Score layout with fake data
  • LIVE voice checks (call the REAL model, so you hear the ACTUAL Silver Wolf
    voice — needs the AI key configured):
      /testtailor_live         → really tailors the sample résumé (real AI
                                 bullets + image + review + before→after diff)
      /testhelpme <question>   → runs /helpme's real prompt
      /testinterview <role>    → runs /interview's real prompt
      /testrecommend           → runs /recommend's real prompt on a sample menu
"""

import asyncio

import discord

from commands import (
    ai_commands, commands_board, gemma_client, job_ai, lang_view, persona,
    resume_utils,
)


# --- fake data ------------------------------------------------------------
# Same scenario as _SAMPLE_RESUME/_SAMPLE_POSTING (Jane Doe → Stripe backend
# intern) so the fake layout preview replicates what the LIVE /testscore produces.
_FAKE_ROW = {
    "job_title": "Backend Software Engineer Intern",
    "company_name": "Stripe",
    "job_url": "https://example.com/job",
    "job_location": "Remote",
    "company_info": {},
}

_FAKE_SCORE = {
    "score": 62,
    "tier": "Moderate",
    # Text fields written in the ENRICHED Silver Wolf voice (direct address +
    # 刀子嘴豆腐心 + light gaming refs + clean ZH), scoring the _SAMPLE_RESUME
    # (Jane Doe) against _SAMPLE_POSTING (Stripe backend intern) — so the layout
    # preview replicates what the LIVE /testscore produces. Strong-fit case (~80):
    # Python/REST/SQL must-haves are all met; only cloud/CI-CD nice-to-haves miss.
    "score": 80,
    "tier": "Strong",
    "summary_en": "Cracked open your file — 80, clean run. Your Python, REST APIs, and SQL line up almost dead-on with what this backend boss wants, and you've got the shipped projects to back it. Only thing between you and top tier is cloud deploy. Honestly? Queue for it.",
    "summary_zh": "翻了下你的档——80，干净。你的 Python、REST API、SQL 跟这个后端 boss 要的几乎一模一样，而且你有真落地的项目撑着。你离顶档就差个云部署。说真的？直接上。",
    # Five subscores the model picked for this backend role: 3 core + impact + recency.
    "technical_skills": 85,
    "experience": 80,
    "domain_fit": 78,
    "impact": 66,
    "recency": 82,
    "highest_reason_en": "Your stack's dead-on — REST APIs in Python plus a real PostgreSQL schema is exactly their must-have list, and you've actually built with all of it. Strongest card on your sheet.",
    "highest_reason_zh": "你的技术栈正中靶心——用 Python 写 REST API，还有真的 PostgreSQL schema，正好是他们的 must-have 清单，而且你全都真做过。你面板上最硬的一张牌。",
    "lowest_reason_en": "Impact's your lowest — the work's real but the bullets don't quantify it. No numbers means recruiters can't see the scale. Easy patch, not a real weakness.",
    "lowest_reason_zh": "成果这块最低——活是真的，但要点没量化。没数字，招聘的就看不出规模。好补，不是真短板。",
    "why_not_higher_en": "Only thing capping you is cloud — you've built the services but never shipped them to AWS/GCP, and CI/CD's nowhere on the sheet. Both are nice-to-haves here, not must-haves, so it's the last polish, not a wall.",
    "why_not_higher_zh": "卡你的就一个云——服务你写了，但没推到 AWS/GCP 上，CI/CD 也一处没写。这两个在这儿是加分项、不是硬性要求，所以是最后打磨，不是墙。",
    "strengths_en": [
        "REST APIs in Python + Flask — the role's core ask, and it's right there in your internship work.",
        "PostgreSQL schema design — the data layer's fully handled, exactly what a backend team wants.",
        "Shipped projects (the scraper bot, the tracker) — proves you build real things, not just coursework.",
    ],
    "strengths_zh": [
        "用 Python + Flask 写 REST API——岗位的核心要求，而且就摆在你实习经历里。",
        "PostgreSQL schema 设计——数据层完全拿下，正是后端团队想要的。",
        "落地的项目（爬虫 bot、追踪器）——证明你是真造东西，不是光上课。",
    ],
    "gaps_en": [
        "No cloud deploy anywhere — you built the services but never shipped them to AWS/GCP. Small 缺陷, easy patch, and it's only a nice-to-have here.",
        "No CI/CD on the sheet — worth adding if any project had a pipeline, even a basic one.",
        "Bullets don't quantify impact — real work, no numbers, so the scale doesn't land.",
    ],
    "gaps_zh": [
        "云部署一个没有——服务你写了，但没推到 AWS/GCP 上。小【缺陷】，好补，而且在这儿只是加分项。",
        "面板上没 CI/CD——哪个项目有流水线的话值得写上，哪怕很基础的。",
        "要点没量化成果——活是真的，但没数字，规模就体现不出来。",
    ],
    "quick_wins_en": [
        "Slap real numbers on your top two bullets — API latency, requests handled, anything true. Proof hits way harder.",
        "Name 'REST API' as the exact phrase where you built one — the scan looks for that literal string.",
        "List Git + your test work explicitly — you did it, so make the ATS see it.",
    ],
    "quick_wins_zh": [
        "给你最强的两条要点加上真实数字——API 延迟、处理的请求数，只要是真的都行。有证据狠多了。",
        "在你写过 REST API 的地方，就用「REST API」这个原词——筛选就找这个字符串。",
        "把 Git 和你写测试的经历明确列出来——你做了，就让 ATS 看见。",
    ],
    "improvements_en": [
        "Here's the easy one, I've got you — deploy one existing project to a free cloud tier (AWS/GCP). Same code, one new line, closes the cloud gap.",
        "Add a basic CI/CD pipeline to a repo (GitHub Actions is free) — small grind, ticks a box they like.",
        "Grab the AWS Cloud Practitioner cert if you want the domain airtight — optional, but it seals the one soft spot.",
    ],
    "improvements_zh": [
        "这个最简单，我带你——把现有的一个项目部署到免费的云上（AWS/GCP）。代码不用改，多一行，把云缺口补上。",
        "给某个仓库加个基础的 CI/CD 流水线（GitHub Actions 免费）——肝一下不多，勾上他们喜欢的一项。",
        "想把领域彻底焊死的话，去考个 AWS Cloud Practitioner——可选，但能把这唯一的软肋封上。",
    ],
}

# Sample /reviewresume output — written in Silver Wolf's voice so /testreview
# shows the persona in the résumé-review embed (matches the real JSON schema:
# impression + strengths/improvements/ats_gaps, with the run-3 caps).
_FAKE_REVIEW = {
    "impression_en": (
        "Scanned your build — solid core, no fluff. You ship real projects and the "
        "Python's legit; the résumé just undersells it. Tighten the wording and this "
        "reads a full tier stronger."
    ),
    "impression_zh": (
        "扫了眼你的档——底子扎实，没水分。你是真做项目的，Python 也是真的；就是简历"
        "把你写弱了。措辞收紧一下，这份能强一整档。"
    ),
    "strengths_en": [
        "Python + full-stack projects prove you actually build, not just study",
        "Clean, quantifiable side project (the scraper) — recruiters love a shipped thing",
        "SQL + Supabase shows you can handle the data layer end-to-end",
    ],
    "strengths_zh": [
        "Python + 全栈项目证明你是真造东西，不是光学",
        "干净、能量化的个人项目（那个爬虫）——招聘的就爱看落地的东西",
        "SQL + Supabase 说明你能从头到尾扛住数据层",
    ],
    "improvements_en": [
        "Lead bullets with the outcome, not the task — 'Built X that did Y', not 'Responsible for X'",
        "Drop a real metric on your top two bullets (users, latency, time saved)",
        "Cut the 'familiar with' filler — list only tools you actually used",
        "One page, most role-relevant project first — bury the weakest",
    ],
    "improvements_zh": [
        "要点先讲成果、别讲任务——写「做了 X，达成 Y」，别写「负责 X」",
        "给你最强的两条要点加个真实数字（用户数、延迟、省下的时间）",
        "把「熟悉某某」这种水词删掉——只列你真用过的工具",
        "压成一页，最相关的项目放最前，最弱的往后放",
    ],
    "ats_gaps_en": [
        "No 'REST API' as an exact phrase — add it where true, ATS scans for it",
        "Cloud keyword missing (AWS/GCP) — name it if you touched it",
        "'CI/CD' absent — include it if your project had any pipeline",
    ],
    "ats_gaps_zh": [
        "没有「REST API」这个原词——真做过就加上，ATS 就扫这个",
        "缺云关键词（AWS/GCP）——碰过就写上",
        "没有「CI/CD」——项目里有流水线的话就写进去",
    ],
}

_FAKE_BLOB = {
    "structured": {
        "name": "Jane Doe",
        "contact": {"email": "jane@example.com", "github": "janedoe"},
        "education": [
            {"school": "State University", "location": "City, ST",
             "degree": "BSc Computer Science", "dates": "2022-2026"}
        ],
        "experience": [
            {"company": "Acme Corp", "role": "SWE Intern", "location": "Remote",
             "dates": "Jun 2024-Aug 2024",
             "description": ["orig bullet 1", "orig bullet 2"]}
        ],
        "projects": [
            {"name": "Internship Bot", "technologies": ["Python", "Supabase"],
             "date": "2024", "link": "",
             "description": ["orig project bullet"]}
        ],
        "skills": [{"category": "Languages", "list": ["Python", "SQL"]}],
    },
    "bullets": [
        {"loc": ["experience", 0, 0],
         "before": "Worked on REST APIs in Python",
         "m": "Built REST APIs as measured by [ADD METRIC] by developing in Python",
         "n": "Built REST APIs by developing in Python",
         "why": {"en": "Led with impact, reshaped to XYZ form, added a metric slot.",
                 "zh": "先讲成果，改成 XYZ 格式，留了数字槽位。"}},
        {"loc": ["experience", 0, 1],
         "before": "Responsible for writing unit tests",
         "m": "Improved reliability as measured by [ADD METRIC] by writing unit tests",
         "n": "Improved reliability by writing unit tests",
         "why": {"en": "Swapped 'responsible for' for a real outcome + metric slot.",
                 "zh": "把「负责」换成真实成果，加了数字槽位。"}},
        {"loc": ["projects", 0, 0],
         "before": "Scraped job boards with Python",
         "m": "Automated posting discovery as measured by [ADD METRIC] by scraping with Python",
         "n": "Automated posting discovery by scraping with Python",
         "why": {"en": "Named the outcome (automation) + XYZ form + metric slot.",
                 "zh": "点明成果（自动化）+ XYZ 格式 + 数字槽位。"}},
    ],
    "overview": {
        "intro_en": "...Alright, I ran Aether Editing on your build and dragged it past the whole review panel — ATS, recruiter, hiring manager, tech lead. Nothing slipped. Read it below.",
        "intro_zh": "……行吧，我给你的配装跑了遍以太编辑，还拖着整个评审团过了一轮——ATS、HR、招聘经理、技术面，一个死角都没留。下面自己看。",
        "strong_en": "Your Python and REST API work is dead-on for this role — that's the core of what they want, and it's real.",
        "strong_zh": "你的 Python 和 REST API 正对这个岗位——这是他们要的核心，而且是真本事。",
        "changed_en": "I surfaced 'REST API' as an exact keyword, front-loaded your Python work, and tightened the bullets to one clean line each.",
        "changed_zh": "我把「REST API」作为原词提了上来，Python 的活往前挪了，每条要点也压成干净的一行。",
        "todo_en": "Fill the [ADD METRIC] slots with real numbers — proof hits way harder than plain claims. That's the only thing left.",
        "todo_zh": "把 [ADD METRIC] 的位置填上真实数字——有证据比空说狠多了。就差这一步。",
    },
    # Silver Wolf's fuller review, folded into the same tailor preview embed.
    "review": _FAKE_REVIEW,
}


# A realistic sample résumé (plain text) + posting, fed to the REAL model so the
# live test commands show the ACTUAL Silver Wolf output — not hardcoded data.
_SAMPLE_RESUME = """Jane Doe — jane@example.com — github.com/janedoe

EDUCATION
State University — B.S. Computer Science, GPA 3.6 (2022-2026)
Relevant coursework: Data Structures, Algorithms, Databases, Operating Systems

EXPERIENCE
Acme Corp — Software Engineering Intern (Jun 2024 - Aug 2024)
- Built REST APIs in Python and Flask for an internal analytics dashboard
- Wrote unit tests that improved reliability and caught regressions before release
- Collaborated with a team of 4 using Git and Agile sprints

PROJECTS
CS Internship Bot — Python, Supabase, discord.py
- Automated internship-posting discovery by scraping and parsing job boards
- Designed a PostgreSQL schema and caching layer for fast lookups

Job Match Tracker — React, Node.js
- Built a full-stack web app to track applications with a REST API backend

SKILLS
Languages: Python, JavaScript, SQL
Tools: Git, Flask, React, Node.js, PostgreSQL, Supabase
"""

_SAMPLE_POSTING = {
    "job_title": "Backend Software Engineer Intern",
    "company_name": "Stripe",
    "job_url": "https://example.com/job",
    "job_location": "Remote",
    "job_summary": (
        "Backend intern to build and scale payment APIs. Must-haves: Python, REST "
        "APIs, SQL/relational databases, Git. Nice to have: cloud (AWS), CI/CD, "
        "distributed systems, unit testing. You'll ship production code with a team."
    ),
    "job_tags": ["Python", "REST APIs", "SQL", "AWS", "CI/CD"],
    "company_info": {},
}


def register(bot, *, logger=None):
    admin = discord.app_commands.default_permissions(administrator=True)

    def _render_score(row, data):
        """Build the real Score embed + wheel + toggle from score JSON `data`."""
        from commands import score_wheel

        _e0, score = job_ai._build_score_embed(row, data, "en")
        png = None
        try:
            png = score_wheel.render(score) if score is not None else None
        except Exception:
            if logger:
                logger.exception("testscore wheel render failed")
        have_wheel = png is not None

        def make_file():
            return job_ai.discord.File(job_ai.io_bytes(png), filename="score.png") if have_wheel else None

        def build(lang):
            embed, _ = job_ai._build_score_embed(row, data, lang)
            if have_wheel:
                embed.set_thumbnail(url="attachment://score.png")
            return embed

        view = lang_view.LangToggleView(build, lang="en", make_file=make_file)
        return build("en"), make_file(), view

    @bot.tree.command(
        name="testscore",
        description="[test] LIVE: real AI scores a sample résumé (see the actual Silver Wolf voice + 5 subscores)",
    )
    @admin
    async def testscore(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        # Run the REAL score prompt against the sample résumé — actual model output.
        prompt = job_ai._SCORE_PROMPT.format(posting=job_ai._job_context(_SAMPLE_POSTING))
        data = await job_ai._ask_json_resume(("text", _SAMPLE_RESUME), prompt, 4000)
        if not data:
            await interaction.followup.send(
                "No score came back — is the AI key configured? (This hits the real model.)",
                ephemeral=True,
            )
            return
        embed, file, view = _render_score(_SAMPLE_POSTING, data)
        kwargs = {"embed": embed, "view": view, "ephemeral": True}
        if file is not None:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)

    @bot.tree.command(
        name="testscore_layout",
        description="[test] Instant layout preview with fake data (no AI)",
    )
    @admin
    async def testscore_layout(interaction: discord.Interaction):
        embed, file, view = _render_score(_FAKE_ROW, _FAKE_SCORE)
        kwargs = {"embed": embed, "view": view, "ephemeral": True}
        if file is not None:
            kwargs["file"] = file
        await interaction.response.send_message(**kwargs)

    @bot.tree.command(
        name="testtailor",
        description="[test] Preview the Tailor embed — build + Silver Wolf's review in one (bilingual)",
    )
    @admin
    async def testtailor(interaction: discord.Interaction):
        # One embed: the tailored résumé IMAGE + Silver Wolf's full review folded in.
        await interaction.response.defer(ephemeral=True, thinking=True)
        tailor = job_ai.TailorView(_FAKE_BLOB, "Palantir Technologies")
        embed, file = await tailor.preview_render()
        kwargs = {"embed": embed, "view": tailor, "ephemeral": True}
        if file is not None:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)

    @bot.tree.command(
        name="testtailor_live",
        description="[test] LIVE: really tailor the sample résumé — one embed: image + review + before→after+why",
    )
    @admin
    async def testtailor_live(interaction: discord.Interaction):
        # End-to-end REAL tailor: parse the sample résumé → run the actual
        # _tailor_build pipeline (rewrite → multi-actor review → overview/review)
        # → real TailorView + rendered image. This is what an actual user gets, so
        # you can SEE the tailoring effect (original bullets → AI-rewritten ones).
        await interaction.response.defer(ephemeral=True, thinking=True)
        structured = await asyncio.to_thread(
            resume_utils.parse_structured, _SAMPLE_RESUME, None
        )
        if not structured:
            await interaction.followup.send(
                "Couldn't parse the sample résumé — is the AI key configured? "
                "(This hits the real model.)",
                ephemeral=True,
            )
            return
        blob = await job_ai._tailor_build(structured, _SAMPLE_POSTING)
        if not blob or not blob.get("bullets"):
            await interaction.followup.send(
                "The tailor build came back empty — check the AI key / logs.",
                ephemeral=True,
            )
            return
        company = _SAMPLE_POSTING.get("company_name") or "role"
        tailor = job_ai.TailorView(blob, company)
        # preview_render now shows image + review + the per-bullet before→after+why
        # (from the blob) — identical to what a real tailor-button user gets.
        embed, file = await tailor.preview_render()
        kwargs = {"embed": embed, "view": tailor, "ephemeral": True}
        if file is not None:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)

    @bot.tree.command(name="testguide", description="[test] Preview the bilingual command guide")
    @admin
    async def testguide(interaction: discord.Interaction):
        view = lang_view.LangToggleView(
            lambda lang: commands_board.build_board_embed(bot, lang), lang="en"
        )
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    # --- /reviewresume — LIVE (real AI on the sample résumé) --------------
    @bot.tree.command(
        name="testreview",
        description="[test] LIVE: real AI reviews a sample résumé (actual Silver Wolf voice)",
    )
    @admin
    async def testreview(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await asyncio.to_thread(
            ai_commands.compute_review, _SAMPLE_RESUME, None
        )
        if not data:
            await interaction.followup.send(
                "No review came back — is the AI key configured? (This hits the real model.)",
                ephemeral=True,
            )
            return
        view = lang_view.LangToggleView(
            lambda lang: ai_commands._review_embed(data, lang), lang="en"
        )
        await interaction.followup.send(
            embed=ai_commands._review_embed(data, "en"), view=view, ephemeral=True
        )

    # --- LIVE voice checks (call the real model) --------------------------
    async def _live(interaction, prompt, title, color):
        """Run a real prompt and show the answer — the true Silver Wolf voice."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        answer = await asyncio.to_thread(gemma_client.ask_text, prompt)
        if not answer:
            await interaction.followup.send(
                "No answer — is the AI key configured? (This one hits the real model.)",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=ai_commands._answer_embed(title[:256], answer, color), ephemeral=True
        )

    @bot.tree.command(name="testhelpme", description="[test] LIVE: run /helpme's real prompt")
    @admin
    @discord.app_commands.describe(question="Ask something (default: a sample question)")
    async def testhelpme(interaction: discord.Interaction, question: str = None):
        q = question or "How do I stand out for a backend SWE internship as a sophomore?"
        prompt = (
            persona.SILVER_WOLF_SYSTEM
            + "\n\n<this_task>\nA CS student is asking you for career help. Answer as "
            "Silver Wolf — real, specific, practical advice FIRST; the personality "
            "colors HOW you say it, never replaces the substance. Tight, useful, "
            "truthful, natural voice (a beat or two of flavor).\n</this_task>"
            + ai_commands._LANG_MIRROR
            + f"\n\n<question>\n{q}\n</question>"
        )
        await _live(interaction, prompt, f"💬 /helpme — {q[:60]}", discord.Color.blurple())

    @bot.tree.command(name="testinterview", description="[test] LIVE: run /interview's real prompt")
    @admin
    @discord.app_commands.describe(role="Role/company (default: a sample role)")
    async def testinterview(interaction: discord.Interaction, role: str = None):
        r = role or "SWE intern at Stripe"
        prompt = (
            persona.SILVER_WOLF_SYSTEM
            + "\n\n<this_task>\n"
            f"You're running interview drills for a CS student prepping for: {r}. "
            "The interview is the boss fight; you're the friend who's cleared it and "
            "is training them."
            + ai_commands._LANG_MIRROR
            + "\n\nGenerate a REALISTIC set: 3 behavioral + 4 technical + 1 "
            "'why this company/role' question, each with a one-line hint. Questions "
            "stay substantive; your voice is in the framing only, light.\n</this_task>"
        )
        await _live(interaction, prompt, f"🎤 /interview — {r}", discord.Color.teal())

    @bot.tree.command(name="testrecommend", description="[test] LIVE: run /recommend on a sample menu")
    @admin
    async def testrecommend(interaction: discord.Interaction):
        menu = (
            "1. Backend Engineer Intern @ Stripe (Remote) — Python, APIs, Postgres\n"
            "2. ML Intern @ OpenAI (SF) — PyTorch, NLP, research\n"
            "3. Frontend Intern @ Vercel (Remote) — React, TypeScript, Next.js\n"
            "4. Data Engineer Intern @ Databricks (SF) — Spark, SQL, pipelines\n"
            "5. Full-Stack Intern @ Notion (NYC) — React, Node, Postgres"
        )
        prompt = (
            persona.SILVER_WOLF_SYSTEM
            + "\n\n<this_task>\nA CS student's résumé is strong in Python, full-stack "
            "web (React/Node), and SQL, with a scraping/automation side project. From "
            "the list below, pick the 5 best fits, ranked, each with one line on WHY "
            "it fits their real background. Silver Wolf voice, light; picks honest.\n"
            "</this_task>"
            + ai_commands._LANG_MIRROR
            + "\n\nOPEN INTERNSHIPS:\n" + menu
        )
        await _live(interaction, prompt, "🧭 /recommend (sample)", discord.Color.gold())
