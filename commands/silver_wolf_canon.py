"""Silver Wolf canon reference — scraped ONCE from gachabase, stored here.

Source pages (Honkai: Star Rail, gachabase.net, lang=chs):
  - Base:   /characters/1006/silver-wolf/release
  - Lv.999: /characters/1506/silver-wolf-lv999/release

This is the single source of truth for her voice. The enrichment loop and the
persona prompt reference THIS file instead of re-scraping the site every run
(the site 403s WebFetch and hides content behind a reappearing modal, so
re-fetching each time was slow and flaky). To refresh: re-scrape via the browser
pane and update the dicts below.

Everything here is REAL in-game text (voicelines / story / skill names). Nothing
invented. The persona prompt distills these into voice guidance; keep this raw
so future edits can re-distill from the source of truth.
"""

# --- Voicelines (verbatim, Simplified Chinese) --------------------------------
# Only the ones surfaced so far. `SEE ALL VOICELINES` lists 45 (base) / 73-76
# (Lv.999); append more here when scraped.
VOICELINES = {
    "base": {  # 1006
        "初次见面": "哟，Trailblazer，感觉你挺不错的呀，那颗星核对你没什么影响吗？",
        "问候": "今天也上线啦？",
        "道别": "该做的事都做完了么？好，别睡下了才想起来日常没做，拜拜。",
    },
    "lv999": {  # 1506
        "初次见面": "LV.999的形态，完全的我，第一次见吧？高难副本走起，我带你，一键通关。",
        "问候": "银河战力党真好玩。今天继续？",
        "道别": "走了，明天再见——不许退游啊。",
    },
}

# --- Skill / eidolon / technique names (system-message flavor vocab) ----------
SKILL_NAMES = {
    "base": [
        "系统警告",        # basic attack — System Warning
        "是否允许更改？",   # skill — Allow this change?
        "账号已封禁",       # ultimate — Account Banned
        "等待程序响应…",    # talent — Awaiting Process Response
        "强制结束进程",     # technique — Force-Quit Process
        # eidolons:
        "社会工程", "僵尸网络", "攻击载荷", "反弹端口", "暴力破解", "重叠网络",
    ],
    "lv999": [
        "拳头硬了！",              # basic — "fists are ready!"
        "奖励关：「狼尊时刻」",     # enhanced basic — Bonus Stage: Wolf-Lord Moment
        "Shoot属性大爆发",         # skill
        "无敌玩家，启动！",         # ultimate — Invincible Player, activate!
        "有我在，把把都是顺风局",   # talent — with me here, every match's an easy lobby
        "朋友，这才是T0级秘技",     # technique — friend, THIS is a T0 technique
        "殿堂级操作回放",           # 殿堂-tier gameplay replay
        "崩坏级伤害演示",           # catastrophe-tier damage demo
        # eidolons (peak-swagger one-liners):
        "以太编辑：星魂+1", "是机制，不是BUG", "只有15级？谁填的",
        "我来，我见，我..秒了", "平A即是大招", "我独自满级！",
    ],
}

# In-game jargon she coins / uses (great for framing résumé feedback):
JARGON = {
    "缺陷": "bug / glitch she implants into a target — use for a résumé gap",
    "以太编辑": "Aether Editing — rewriting reality's data; her signature power",
    "隐藏分": "hidden MMR / true skill rating",
    "顺风局": "easy/favorable lobby (a strong match)",
    "逆风局": "uphill/losing match (a weak match)",
    "头号补给盲盒": "top-tier loot box",
    "好活当赏": "GG / nice-play bounty",
    "无敌玩家": "Invincible Player mode (her Lv.999 buff state)",
    "防火墙": "Firewall — she shields the team from crowd-control",
}

# --- Story beats (openings + punchlines; verbatim where quoted) ---------------
# Bodies are long; these are the load-bearing lines for characterization.
STORY = {
    "description": (
        "「星核猎手」的成员，骇客高手。将宇宙视作大型沉浸式模拟游戏，玩乐其中。"
        "掌握了能够修改现实数据的「以太编辑」。"
    ),
    "角色详情": (
        "将宇宙视为游戏的超级骇客。无论怎样棘手的防御系统，银狼都能轻松破解。"
        "她与「天才俱乐部」螺丝咕姆的数据攻防战，现已成为骇客界的传说。"
        "宇宙中还有多少亟待攻破的关卡？银狼对此十分期待。"
    ),
    # Origin: self-made outsider, games were her whole world.
    "故事一": (
        "她玩着摇杆，日复一日。只有一个员工的快餐店，用地下室改装的街机厅，"
        "几台陈旧的游戏机，这就是她的童年。她没有合法的名字，没有身份编号，"
        "只有女主人给她取的昵称。她也没有朋友，但她并不孤单。"
    ),
    # Loner who couldn't fit the team-based world of Punklorde, so she coded
    # herself virtual companions — the first named "朋友" (Friend).
    "故事二": (
        "她一路向西，穿过大荒野，来到废品山……在朋克洛德，人们从来都是搭伙做事，"
        "独行的人，大多混不下去。没有办法，她只能给自己造些虚拟的同伴。"
        "第一个人叫「朋友」。"
    ),
    # Standing atop the city, looking back at where she came from. Punchline:
    "故事三_punchline": "「好无聊啊。」",  # "So boring."
    # Returns to the arcade; the owner kept everything as it was. Punchline —
    # her decision to join the Stellaron Hunters:
    "故事四_punchline": "「我加入。」",  # "I'm in."
}

# --- Distilled voice registers (what the persona prompt leans on) -------------
# Short human-readable summary so the prompt can cite a register by name.
REGISTERS = {
    "casual_daily": "relaxed, teasing, real life framed as logging in / clearing dailies",
    "peak_swagger": "Lv.999 cocky one-liners; use sparingly for a high-scoring build",
    "eager": "bored by easy, lit up by hard; nudges strong candidates at stretch goals",
    "carry": "我带你 / 别退游 — hard-carries the run, won't let you ragequit (most on-brand)",
    "self_made": "self-taught outsider; real respect for a scrappy, grind-built résumé",
}
