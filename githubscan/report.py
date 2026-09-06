"""Compile the GitHub scan into a markdown report + drive the tailor suggestion.

Two entry points:
  build_report(username, jd_text, token, limit)  -> str (markdown, 6 sections)
  suggest_for_posting(username, jd_text, resume_projects, token) -> dict | None

The report BODY is recruiter-facing, so it stays clean and professional — the
Silver Wolf voice belongs only in the chat text the command wraps around it, never
inside the report itself (same rule as résumé bullets).

Anti-fabrication: the new-project idea is drawn from the three reference repos +
the JD; we name which reference inspired it and never claim the candidate built
anything they didn't.
"""

from __future__ import annotations

import asyncio
import logging

from githubscan import repo_analyzer as ra

logger = logging.getLogger("githubscan")

# Reference idea banks (from the task inputs). We cite these by name in the
# new-project suggestion so the user can go browse them.
REFERENCE_REPOS = [
    "The-Cool-Coders/Project-Ideas-And-Resources",
    "practical-tutorials/project-based-learning",
    "codecrafters-io/build-your-own-x",
]

_MAX_REPOS_ANALYZED = 30  # cap API + AI work on users with huge profiles


async def _analyze_all(entries: list[dict]) -> list[dict]:
    """Analyze repos concurrently (gemma_client has its own concurrency cap +
    key rotation, so a burst here queues rather than 429s)."""
    tasks = [asyncio.to_thread(ra.analyze_repo, e) for e in entries]
    return await asyncio.gather(*tasks)


async def scan_and_score(username: str, jd_text: str, token: str | None,
                         limit: int = _MAX_REPOS_ANALYZED):
    """Steps 1-3: list → analyze → score. Returns (all_scored, entries_count).
    Raises on a bad username so the caller surfaces a clean error."""
    entries = await asyncio.to_thread(ra.list_public_repos, username, token)
    total = len(entries)
    entries = entries[:limit]  # newest-first already; cap the tail
    analyses = await _analyze_all(entries)
    scored = await asyncio.to_thread(ra.score_repos, analyses, jd_text)
    return scored, total


async def extract_projects(scored: list[dict]) -> list[dict]:
    """ATS-extract structured rows for the github_projects table from already-
    analyzed repos (reuses their README/listing — no extra GitHub calls). Skips
    forks (not the candidate's original work). Concurrent."""
    targets = [s for s in scored if not s.get("is_fork")]
    rows = await asyncio.gather(
        *(asyncio.to_thread(ra.extract_project, s) for s in targets)
    )
    return [r for r in rows if r and r.get("repo_name")]


_IMPROVE_INSTR = """\
You are advising on how to strengthen ONE GitHub project for a specific job. Given \
the project summary/stack and the target job description, give 3-5 CONCRETE, \
SPECIFIC improvement suggestions — never generic. Bad: "add tests". Good: "add \
integration tests for the payment webhook handler, since the role lists Stripe and \
reliability as core responsibilities". Cover, where relevant: missing skills/tech \
from the JD, documentation gaps (README/setup/architecture), testing/CI/CD/code- \
quality gaps appropriate to the role's seniority, and feature additions that map to \
the role's responsibilities. Return STRICT JSON: {"suggestions": ["...", "..."]}. \
Each suggestion one sentence, actionable, and tied to the JD."""


async def improve_project(project: dict, jd_text: str) -> list[str]:
    """Step 4: specific, JD-tailored improvements for the strongest project."""
    import json

    from commands import gemma_client
    from commands.gemma_client import SMART_CHAIN

    proj = {
        "name": project.get("name"),
        "summary": project.get("summary"),
        "stack": project.get("stack"),
        "language": project.get("language"),
    }
    prompt = (
        f"{_IMPROVE_INSTR}\n\n--- JOB DESCRIPTION ---\n{(jd_text or '')[:4000]}\n"
        f"--- PROJECT ---\n{json.dumps(proj, ensure_ascii=False, default=str)[:2000]}\n--- END ---"
    )
    data = await asyncio.to_thread(
        gemma_client.ask_json_text, prompt, 900, 0.3, SMART_CHAIN
    )
    out = []
    if data and isinstance(data.get("suggestions"), list):
        out = [str(s).strip() for s in data["suggestions"] if str(s).strip()]
    return out[:5]


_NEWPROJ_INSTR = """\
Propose EXACTLY ONE new project idea that fills a gap between the candidate's \
existing repos and the target job description. It must be distinct from what they \
already have (a list of their repo names is given — do not duplicate one). Draw the \
concept from the reference idea banks named below and adapt it to the JD. Scope it \
to be BUILDABLE (a few focused features), not open-ended. Return STRICT JSON:
{
  "name": "<project name/concept>",
  "why": "<why it's relevant to THIS role, echoing the job description's language>",
  "features": ["<core feature>", "<core feature>", "<core feature>"],
  "inspired_by": "<which reference repo(s) inspired it, by name>",
  "stack": "<recommended tech stack, tied to the JD>"
}
Facts only; the reference repos are IDEA sources, not something the candidate built."""


async def propose_project(existing_names: list[str], jd_text: str) -> dict | None:
    """Step 5: one new project idea from the reference banks + JD."""
    import json

    from commands import gemma_client
    from commands.gemma_client import SMART_CHAIN

    ctx = {
        "existing_repos": existing_names[:40],
        "reference_idea_banks": REFERENCE_REPOS,
    }
    prompt = (
        f"{_NEWPROJ_INSTR}\n\n--- JOB DESCRIPTION ---\n{(jd_text or '')[:4000]}\n"
        f"--- CONTEXT ---\n{json.dumps(ctx, ensure_ascii=False)}\n--- END ---"
    )
    data = await asyncio.to_thread(
        gemma_client.ask_json_text, prompt, 900, 0.5, SMART_CHAIN
    )
    if data and data.get("name"):
        return data
    return None


def _pushed_str(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "—"


def _shortlist(scored: list[dict], lo: int = 3, hi: int = 5) -> list[dict]:
    """Top 3-5 non-fork, non-tutorial projects (fall back to best available if
    the profile is thin)."""
    strong = [s for s in scored if not s["is_fork"] and not s["looks_tutorial"]]
    picks = strong[:hi]
    if len(picks) < lo:  # thin profile — backfill from the rest
        picks = scored[:hi]
    return picks[:hi] if len(picks) >= lo else picks


async def build_report(username: str, jd_text: str, token: str | None,
                       limit: int = _MAX_REPOS_ANALYZED) -> tuple[str, dict]:
    """Full 6-section markdown report. Returns (markdown, meta) where meta has
    the shortlist + scanned counts for the chat summary. Clean/professional
    body (recruiter-facing)."""
    scored, total = await scan_and_score(username, jd_text, token, limit)
    if not scored:
        return "", {"total": total, "shortlist": [], "analyzed": 0}

    shortlist = _shortlist(scored)
    strongest = shortlist[0] if shortlist else scored[0]

    improvements, new_proj, project_rows = await asyncio.gather(
        improve_project(strongest, jd_text),
        propose_project([s["name"] for s in scored], jd_text),
        extract_projects(scored),
    )

    lines = [f"# GitHub Project Analysis — @{ra._normalize_username(username)}", ""]

    # 1. Repo Scan Summary
    lines += ["## 1. Repo Scan Summary", ""]
    lines += ["| Repo | Lang | ★ | Updated | Summary |",
              "| --- | --- | --- | --- | --- |"]
    for s in scored:
        flags = []
        if s["is_fork"]:
            flags.append("fork")
        if s["looks_tutorial"]:
            flags.append("tutorial")
        if s["archived"]:
            flags.append("archived")
        tag = f" _({', '.join(flags)})_" if flags else ""
        summ = (s["summary"] or "").replace("|", "\\|").replace("\n", " ")
        summ = (summ[:140] + "…") if len(summ) > 140 else summ
        lines.append(
            f"| [{s['name']}]({s['url']}){tag} | {s['language'] or '—'} | "
            f"{s['stars']} | {_pushed_str(s['pushed_at'])} | {summ} |"
        )
    if total > len(scored):
        lines.append(f"\n_Analyzed the {len(scored)} most-recent of {total} public repos._")
    lines.append("")

    # 2. Recommended Projects Section (resume-ready)
    lines += ["## 2. Recommended Projects Section", "",
              "_Paste-ready for a résumé. Ordered by fit to this role._", ""]
    for s in shortlist:
        stack = ", ".join(s["stack"][:6]) if s["stack"] else (s["language"] or "—")
        lines += [
            f"### {s['name']}",
            f"{s['summary']}",
            f"**Tech:** {stack}",
            f"**Why it fits:** {s['reason'] or 'Relevant to the role.'}",
            "",
        ]

    # 3. Improvement Plan
    lines += ["## 3. Improvement Plan", "",
              f"**Strongest candidate for this role: [{strongest['name']}]({strongest['url']})**", ""]
    if improvements:
        for imp in improvements:
            lines.append(f"- {imp}")
    else:
        lines.append("- (Could not generate specific suggestions — try re-running.)")
    lines.append("")

    # 4. New Project Suggestion
    lines += ["## 4. New Project Suggestion", ""]
    if new_proj:
        lines += [
            f"### {new_proj.get('name')}",
            f"**Why this role:** {new_proj.get('why', '')}",
        ]
        feats = new_proj.get("features") or []
        if isinstance(feats, list) and feats:
            lines.append("**Core features:**")
            for f in feats:
                lines.append(f"- {f}")
        lines += [
            f"**Recommended stack:** {new_proj.get('stack', '')}",
            f"**Inspired by:** {new_proj.get('inspired_by', '')}",
            "",
        ]
    else:
        lines.append("_(Could not generate a suggestion — try re-running.)_")

    meta = {
        "total": total,
        "analyzed": len(scored),
        "shortlist": shortlist,
        "strongest": strongest,
        "project_rows": project_rows,  # ready for db.upsert_github_projects
    }
    return "\n".join(lines), meta


# --- Tailor-button path -------------------------------------------------------

async def suggest_for_posting(username: str, jd_text: str,
                              resume_project_names: list[str],
                              token: str | None,
                              limit: int = 15) -> dict | None:
    """For the Tailor button: scan the user's GitHub, score vs this posting's JD,
    and return the single best repo that (a) scores well for the role and (b) is
    NOT already on their résumé — i.e. a project they may have overlooked.
    Returns a dict {name, url, summary, stack, relevance, reason} or None.

    Suggestion only — the caller decides whether to surface/splice it. Never
    modifies anything here."""
    try:
        scored, _ = await scan_and_score(username, jd_text, token, limit)
    except Exception:
        logger.exception("github suggest: scan failed for %s", username)
        return None
    if not scored:
        return None

    have = {n.strip().lower() for n in (resume_project_names or []) if n}
    for s in scored:
        if s["is_fork"] or s["looks_tutorial"]:
            continue
        if s["name"].strip().lower() in have:
            continue  # already on the résumé
        if s["relevance"] < 55:
            break  # sorted best-first; nothing left is relevant enough
        return {
            "name": s["name"],
            "url": s["url"],
            "summary": s["summary"],
            "stack": s["stack"],
            "language": s["language"],
            "relevance": s["relevance"],
            "reason": s["reason"],
        }
    return None
