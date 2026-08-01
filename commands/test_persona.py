"""Admin test commands to preview the bilingual Silver Wolf embeds with fake
data — no AI calls, no DB writes. Each posts the REAL embed + its language
toggle so you can eyeball EN ⇄ 中文 and the layout.

/testwelcome_sw  → the welcome embed (EN/中文 toggle)
/testscore       → a Score embed with sample bilingual data (EN/中文 toggle)
/testtailor      → the Tailor preview + metric buttons (EN/中文 toggle)
/testguide       → the command guide (EN/中文 toggle)
"""

import discord

from commands import commands_board, job_ai, lang_view


# --- fake data ------------------------------------------------------------
_FAKE_ROW = {
    "job_title": "Software Engineer Intern",
    "company_name": "Palantir Technologies",
    "job_url": "https://example.com/job",
    "job_location": "Chicago, IL",
    "company_info": {},
}

_FAKE_SCORE = {
    "score": 62,
    "tier": "Moderate",
    "summary_en": "Solid engineering foundation, but your stack's off-meta for this enterprise SaaS run.",
    "summary_zh": "工程底子扎实，但你的技术栈对这个企业级 SaaS 副本来说偏离 meta 了。",
    "technical_skills": 56,
    "experience": 81,
    "domain_fit": 37,
    "highest_reason_en": "Strong project loadout — multiple full-stack builds carry your Experience score.",
    "highest_reason_zh": "项目装备很强——好几个全栈项目把你的经验分抬起来了。",
    "lowest_reason_en": "Domain Fit's low: the run needs enterprise SaaS + low-code automation, none on your sheet.",
    "lowest_reason_zh": "领域匹配偏低：这场需要企业级 SaaS 和低代码自动化，你面板上一个都没有。",
    "why_not_higher_en": "The boss wants Salesforce, Workday, and declarative automation. Grab any of those and the score jumps.",
    "why_not_higher_zh": "Boss 要的是 Salesforce、Workday 和声明式自动化。摸到其中任意一个，分数就往上跳。",
    "strengths_en": [
        "Python — core language for the role (Skills, Internship Bot)",
        "Full-stack builds — proves you ship (Projects)",
        "SQL — data layer covered (Skills)",
    ],
    "strengths_zh": [
        "Python — 岗位核心语言（技能、实习 Bot）",
        "全栈项目 — 证明你能交付（项目）",
        "SQL — 数据层拿下（技能）",
    ],
    "gaps_en": [
        "No enterprise SaaS (Salesforce/Workday)",
        "No low-code / declarative automation",
        "No large-scale data handling shown",
    ],
    "gaps_zh": [
        "缺企业级 SaaS（Salesforce/Workday）",
        "缺低代码 / 声明式自动化",
        "没有展示大规模数据处理",
    ],
    "quick_wins_en": [
        "List any SaaS integration coursework you already have",
        "Name the exact cloud services you used",
        "Quantify a project's impact with a real metric",
    ],
    "quick_wins_zh": [
        "把你已有的 SaaS 集成相关课程列出来",
        "写清楚你具体用过哪些云服务",
        "用一个真实数字量化某个项目的影响",
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
         "m": "Built REST APIs as measured by [ADD METRIC] by developing in Python",
         "n": "Built REST APIs by developing in Python"},
        {"loc": ["experience", 0, 1],
         "m": "Improved reliability as measured by [ADD METRIC] by writing unit tests",
         "n": "Improved reliability by writing unit tests"},
        {"loc": ["projects", 0, 0],
         "m": "Automated posting discovery as measured by [ADD METRIC] by scraping with Python",
         "n": "Automated posting discovery by scraping with Python"},
    ],
}


def register(bot, *, logger=None):
    admin = discord.app_commands.default_permissions(administrator=True)

    @bot.tree.command(name="testscore", description="[test] Preview the bilingual Score embed")
    @admin
    async def testscore(interaction: discord.Interaction):
        from commands import score_wheel

        _e0, score = job_ai._build_score_embed(_FAKE_ROW, _FAKE_SCORE, "en")
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
            embed, _ = job_ai._build_score_embed(_FAKE_ROW, _FAKE_SCORE, lang)
            if have_wheel:
                embed.set_thumbnail(url="attachment://score.png")
            return embed

        view = lang_view.LangToggleView(build, lang="en", make_file=make_file)
        kwargs = {"embed": build("en"), "view": view, "ephemeral": True}
        f = make_file()
        if f is not None:
            kwargs["file"] = f
        await interaction.response.send_message(**kwargs)

    @bot.tree.command(name="testtailor", description="[test] Preview the bilingual Tailor flow")
    @admin
    async def testtailor(interaction: discord.Interaction):
        view = job_ai.TailorView(_FAKE_BLOB, "Palantir Technologies")
        await interaction.response.send_message(
            embed=view.preview_embed(), view=view, ephemeral=True
        )

    @bot.tree.command(name="testguide", description="[test] Preview the bilingual command guide")
    @admin
    async def testguide(interaction: discord.Interaction):
        view = lang_view.LangToggleView(
            lambda lang: commands_board.build_board_embed(bot, lang), lang="en"
        )
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
