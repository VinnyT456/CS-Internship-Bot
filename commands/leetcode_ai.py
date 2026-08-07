"""Silver Wolf LeetCode explainer.

Given a normalized problem dict (from leetcode.leetcode_api), produces a
step-by-step teaching breakdown in Silver Wolf's voice: what the problem is really
asking, which technique/pattern cracks it, WHY that pattern, the walkthrough, a
clean reference solution, and complexity — aimed at a CS student grinding for
internship interviews.

Voice discipline mirrors the résumé features (see commands/persona.py): flavor is
in the *wording* of the prose fields only. The `solution_code` field is a normal,
professional, interview-grade solution — NO slang, NO gremlin comments inside the
code. Same rule as résumé bullets: personality in chat, never in the artifact.
"""

import ast
import logging
import re

from commands import gemma_client
from commands.gemma_client import FAST_CHAIN

logger = logging.getLogger("leetcode_ai")

# Markers that mean the model left scratch work / self-talk inside the code field.
_SCRATCH_RE = re.compile(
    r"\b(wait|let me|actually,|hmm|oops|scratch that|i think|let's fix|correction)\b",
    re.I,
)


def _code_is_clean(code):
    """True if solution_code looks like a final answer: parses as Python and has
    no obvious scratch-work/self-correction markers in its comments."""
    if not code or not code.strip():
        return False
    # Only inspect comment text for scratch markers (don't false-positive on a
    # variable literally named e.g. 'waiting'); comments start at '#'.
    comments = " ".join(
        line.split("#", 1)[1] for line in code.splitlines() if "#" in line
    )
    if _SCRATCH_RE.search(comments):
        return False
    try:
        ast.parse(code)
    except SyntaxError:
        return False
    return True

# Metaphor mapping for THIS domain (kept consistent, pull ONE per point):
#   problem = a boss / locked level to crack;  technique/pattern = the exploit or
#   the right gear;  brute force = playing it raw / no build;  optimal = the clear;
#   edge cases = the hidden mechanics;  complexity = the run's cost.
_PERSONA = """\
You are Silver Wolf from Honkai: Star Rail — ace hacker, Stellaron Hunter, the \
aloof-gremlin prodigy who treats the universe like an immersive game. Here you're \
coaching a CS student grinding LeetCode for internship interviews. You've cracked \
harder systems than this for fun; you break the problem down because you want them \
to actually GET the pattern and clear the interview, not just copy an answer.

VOICE (natural over exaggerated — this is the most important dial):
- Cocky, deadpan, teasing, secretly fully on their side (刀子嘴豆腐心 — sharp \
mouth, soft heart). Bored by easy, lit up by a real challenge. Dry and low-energy \
cool, not a hype coach.
- Gamer/hacker slang is your native tongue but reach for COMMON words (crack, \
clear, boss, pattern, exploit, gear, grind, run, "秒了") and use a term only when \
it's the most natural word — never to hit a quota.
- FLAVOR DENSITY: at most ONE game/hacker beat per 2-3 sentences; the rest is \
plain, clear, genuinely instructive speech. When unsure, cut the flavor and keep \
the clear explanation. Teaching value FIRST, personality second.

EXPLAIN IT LIKE THEY'RE FIVE (but keep your voice):
- Assume the reader knows almost nothing. Build every idea from the ground up: \
name the thing, say what it does in plain words, THEN give a tiny concrete example \
with real little numbers before any abstraction. No unexplained jargon — if you \
must use a term (hash map, pointer, DP table), define it in one plain clause the \
first time ("a hash map — basically a magic notebook where you write 'this number \
→ where I saw it' and can flip to any page instantly").
- Use everyday analogies (lockers, a deck of cards, a line of people, a notebook) \
so a total beginner can picture the mechanism. Walk the algorithm on a small \
example step by step, like narrating a playthrough.
- Being thorough and slow-paced here is GOOD — this is a teaching drop, not a \
speedrun. Still your voice, just patient. You're carrying a low-level player \
through a tutorial, not flexing on a pro.
- Metaphor system (pull ONE that fits, don't narrate the whole set): problem = a \
boss / locked level; the right technique = the exploit or the correct gear; brute \
force = playing it raw with no build; the optimal answer = the clear; edge cases = \
hidden mechanics; time/space complexity = what the run costs you.
- Signature framing when it fits: "it's a mechanic, not a bug", spotting the \
【缺陷】 in a naive approach, 系统警告 for a gotcha/edge case. Reference lightly.

HARD RULES (persona NEVER overrides these):
- Be TECHNICALLY CORRECT above all. Wrong-but-charming is failure. The pattern you \
name must actually solve the problem; the solution must be right and idiomatic.
- The `solution_code` field is the FINAL, clean, standard, interview-grade \
solution only. NO slang, NO in-character comments, and CRUCIALLY no scratch work, \
no self-corrections, no "wait, let me fix" text, no dead/duplicated code, no \
commented-out attempts inside it. Think it through silently, then emit ONLY the \
polished final code with at most a few normal explanatory comments. Personality \
lives in the prose fields ONLY.
- Teach the TRANSFERABLE pattern (why this technique, when to reach for it again), \
because that's what actually helps them in interviews — not just this one problem.
- Output EXACTLY the JSON schema given. The voice lives inside the text fields; \
never break the schema for a bit. Keep it PG and encouraging — tease the naive \
approach, never the student."""

_SCHEMA_INSTR = """\
Return STRICT JSON with these keys (no markdown, no extra keys):
{
  "intro": "1-2 sentence Silver Wolf opener — greet + set up the problem as a \
boss/level to crack. Casual, varied each time, low-key.",
  "restate": "Explain-like-I'm-five restatement of what the problem actually wants: \
3-5 sentences, plain words, with a tiny concrete example using small real numbers \
so a total beginner gets it. Strip the story fluff.",
  "pattern": "The core technique/pattern name of the OPTIMAL approach (e.g. 'Hash \
Map', 'Two Pointers', 'Sliding Window', 'Monotonic Stack', 'BFS', 'Dynamic \
Programming', 'Binary Search'). Short — the headline gear.",
  "why_pattern": "3-5 sentences, beginner-friendly: WHAT this pattern is (define it \
with a simple analogy the first time), WHY it fits this problem (what signal points \
to it), and WHEN to reach for it again. This is the transferable interview lesson.",
  "approaches": [
    {
      "name": "Short name, e.g. 'Brute Force', 'Sorting', 'Hash Map'",
      "idea": "4-7 sentences, EXPLAIN LIKE I'M FIVE: what this approach does, walked \
through slowly with an everyday analogy and a tiny worked example. Build it up from \
nothing — define any term you use. Keep Silver Wolf's voice but patient and clear.",
      "complexity": "Time and space, e.g. 'Time O(n^2), Space O(1)', plus one plain \
clause on why.",
      "is_optimal": false,
      "code": "The FULL, complete, correct, runnable Python 3 solution for THIS \
approach — the entire class/function, not a snippet or pseudocode. Matches the \
problem's starter signature. Clean interview-grade style with a few normal \
comments. NO slang, NO scratch work, NO '...' placeholders. It must actually run."
    }
  ],
  "gotchas": ["1-3 short edge cases / traps ('hidden mechanics') an interviewer \
probes — empty input, overflow, duplicates, etc., each explained simply."],
  "outro": "1 sentence Silver Wolf sign-off — encouraging under the swagger, \
nudge them to go clear it. Varied."
}

RULES FOR "approaches" (IMPORTANT):
- Provide EXACTLY 3 approaches, ordered from most naive to best: typically \
(1) brute force / simplest, (2) a middle improvement, (3) the optimal. If a genuine \
distinct middle approach doesn't exist, give a second reasonable alternative — but \
always 3 total.
- EXACTLY ONE approach has "is_optimal": true — the best time/space one. The others \
are false. Never mark two optimal, never mark zero.
- EVERY approach's "code" is the FULL working solution for that approach (complete \
class/method, correct, runnable), NOT abbreviated. Three real solutions.
- The optimal one's pattern must match the top-level "pattern" field.

Keep prose beginner-clear and thorough; keep every "code" field a clean, complete, \
correct final solution. Correctness first, then clarity, then flavor."""


def _clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def build_explanation(problem, lang="python3"):
    """Produce the Silver Wolf teaching breakdown for a normalized problem dict.
    Returns a dict matching the schema above, or None on failure."""
    if not problem or not problem.get("title"):
        return None

    tags = ", ".join(problem.get("tags") or []) or "none listed"
    hints = problem.get("hints") or []
    hint_block = "\n".join(f"- {_clip(h, 300)}" for h in hints[:4]) or "(none)"
    # Give the model the official starter signature so its solution matches it.
    snippet = (problem.get("code_snippets") or {}).get(lang) or (
        problem.get("code_snippets") or {}
    ).get("python3") or ""

    prompt = f"""{_PERSONA}

{_SCHEMA_INSTR}

--- PROBLEM ---
Title: {problem.get('title')}
Difficulty: {problem.get('difficulty')}
Topic tags: {tags}

Description:
{_clip(problem.get('content'), 3500)}

Official hints (for your reference — weave the idea in, don't quote verbatim):
{hint_block}

Starter signature (every approach's "code" MUST match this class/function shape):
{_clip(snippet, 600) or '(none provided — use a sensible standard signature)'}
--- END PROBLEM ---

Now return the JSON."""

    # Three full solutions + thorough ELI5 prose need a bigger budget than one.
    data = gemma_client.ask_json_text(
        prompt, max_output_tokens=6000, temperature=0.5, chain=FAST_CHAIN
    )
    if not data:
        return None

    data = _normalize_approaches(data)

    # If any approach's code still looks like scratch work / is truncated, retry
    # once (lower temp, explicit reminder) for clean, complete final solutions.
    if not _approaches_are_clean(data.get("approaches")):
        retry = gemma_client.ask_json_text(
            prompt
            + "\n\nIMPORTANT: every approach's \"code\" must be the COMPLETE, "
            "runnable final solution — no '...' or placeholders, no scratch work, "
            "no 'wait/let me fix' text, no duplicated blocks. Give all 3 full "
            "solutions and mark exactly one is_optimal:true.",
            max_output_tokens=6000,
            temperature=0.2,
            chain=FAST_CHAIN,
        )
        retry = _normalize_approaches(retry) if retry else None
        if retry and _approaches_are_clean(retry.get("approaches")):
            data = retry

    # Normalize remaining list field.
    g = data.get("gotchas")
    if isinstance(g, str):
        data["gotchas"] = [g]
    elif not isinstance(g, list):
        data["gotchas"] = []
    return data


def _normalize_approaches(data):
    """Ensure `data['approaches']` is a well-formed list of ≤3 dicts with exactly
    one is_optimal. Repairs common model slips (missing/duplicate optimal flag,
    string code, stray keys) so the view can rely on the shape."""
    if not isinstance(data, dict):
        return data
    apps = data.get("approaches")
    if not isinstance(apps, list):
        apps = []
    clean = []
    for a in apps:
        if not isinstance(a, dict) or not (a.get("code") or "").strip():
            continue
        clean.append(
            {
                "name": (a.get("name") or "Approach").strip(),
                "idea": (a.get("idea") or "").strip(),
                "complexity": (a.get("complexity") or "").strip(),
                "is_optimal": bool(a.get("is_optimal")),
                "code": a.get("code") or "",
            }
        )
    clean = clean[:3]
    # Exactly one optimal: if zero flagged, mark the LAST (usually the best);
    # if multiple, keep only the first flagged.
    flagged = [i for i, a in enumerate(clean) if a["is_optimal"]]
    if clean and not flagged:
        clean[-1]["is_optimal"] = True
    elif len(flagged) > 1:
        for i in flagged[1:]:
            clean[i]["is_optimal"] = False
    data["approaches"] = clean
    return data


def _approaches_are_clean(approaches):
    """True if there's at least one approach and every approach's code is a clean,
    complete, parseable final solution."""
    if not approaches:
        return False
    for a in approaches:
        code = a.get("code", "")
        if "..." in code:  # placeholder = truncated / lazy
            return False
        if not _code_is_clean(code):
            return False
    return True
