"""GitHub public-repo analyzer for the résumé Projects section.

Scans a GitHub user's PUBLIC, non-fork repos at MINIMAL depth (one README fetch +
one top-level listing per repo, no file-by-file traversal), summarizes each, and
scores them against a target job description so the strongest projects can be
surfaced for a résumé/portfolio.

Read/analysis only — this module never writes to, forks, or modifies any repo.

Auth: an optional fine-grained PAT (scope: public repos, read-only) read from the
GITHUB_TOKEN env var raises the rate limit (5000/hr vs 60/hr). The token is used
only to construct the client; it is NEVER printed, logged, echoed, or persisted.

PyGithub (`github`) is imported lazily inside functions so the always-on bot
process stays lean and an import error can't break module load. The package is
deliberately named `githubscan` (not `github`) so it doesn't shadow PyGithub.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("githubscan")

# Filenames that betray a tech stack without opening any source. Cheap signal:
# derived purely from the top-level listing we already fetch.
_STACK_MARKERS = {
    "package.json": "Node.js/JavaScript",
    "requirements.txt": "Python",
    "pyproject.toml": "Python",
    "Pipfile": "Python",
    "pom.xml": "Java/Maven",
    "build.gradle": "Java/Gradle",
    "go.mod": "Go",
    "Cargo.toml": "Rust",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose",
    "next.config.js": "Next.js",
    "vite.config.js": "Vite",
    "tsconfig.json": "TypeScript",
    "tailwind.config.js": "Tailwind CSS",
    ".github": "GitHub Actions/CI",
    "terraform": "Terraform",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
}

# Heuristics that a repo is a course-along / tutorial clone rather than original
# work — deprioritized in scoring but still shown in the scan summary.
_TUTORIAL_HINTS = re.compile(
    r"\b(tutorial|course|bootcamp|following along|freecodecamp|clone of|"
    r"learning|practice repo|my solutions|leetcode solutions|hackerrank)\b",
    re.IGNORECASE,
)


def _client(token: str | None):
    """Build a PyGithub client. Uses the token only to authenticate; the value
    never leaves this function. Returns an unauthenticated client if no token."""
    from github import Auth, Github

    if token:
        return Github(auth=Auth.Token(token))
    return Github()  # unauthenticated: 60 req/hr, fine for a one-off scan


def _normalize_username(username_or_url: str) -> str:
    """Accept a bare username or a profile/repo URL and return the username."""
    s = (username_or_url or "").strip()
    m = re.search(r"github\.com/([^/\s?#]+)", s, re.IGNORECASE)
    if m:
        return m.group(1)
    return s.lstrip("@")


def list_public_repos(username_or_url: str, token: str | None = None) -> list[dict]:
    """List PUBLIC, owner-owned repos for a user. Forks are kept (flagged
    is_fork) so the scan summary is complete, but callers deprioritize them.
    Returns a list of dicts sorted by most-recently-pushed. Raises on a bad
    username / auth so the caller can surface a clean error."""
    username = _normalize_username(username_or_url)
    gh = _client(token)
    user = gh.get_user(username)  # raises github.GithubException if not found
    repos = []
    for r in user.get_repos(type="owner"):
        if r.private:
            continue  # defensive: an owner scan shouldn't see private repos anyway
        repos.append(
            {
                "name": r.name,
                "full_name": r.full_name,
                "description": r.description or "",
                "language": r.language or "",
                "stars": r.stargazers_count or 0,
                "pushed_at": r.pushed_at,  # datetime or None
                "url": r.html_url,
                "is_fork": bool(r.fork),
                "archived": bool(r.archived),
                "_repo": r,  # kept for analyze_repo; stripped before returning to callers
            }
        )
    repos.sort(key=lambda d: d["pushed_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return repos


def _fetch_readme(repo) -> str:
    """Return the README's first ~5000 chars, or '' if none. One API call."""
    try:
        raw = repo.get_readme().decoded_content.decode("utf-8", errors="replace")
        return raw[:5000]
    except Exception:
        return ""


def _fetch_toplevel(repo) -> list[str]:
    """Return top-level file/dir names. One API call (no recursion)."""
    try:
        contents = repo.get_contents("")
        return [c.name for c in contents]
    except Exception:
        return []


def _stack_from_names(names: list[str]) -> list[str]:
    """Tech-stack markers inferred from top-level filenames only."""
    found = []
    lower = {n.lower(): n for n in names}
    for marker, label in _STACK_MARKERS.items():
        if marker.lower() in lower and label not in found:
            found.append(label)
    return found


_SUMMARY_INSTR = """\
You are summarizing a GitHub repository for a résumé Projects section. Given the \
repo metadata, README excerpt, and top-level files, write a NEUTRAL, professional \
2-3 sentence summary: what the project does and what problem it solves. No hype, \
no first person, no marketing. If signal is thin, say what can be inferred and \
note it's unclear. Return STRICT JSON: {"summary": "..."}. Facts only — never \
invent a feature, tech, or metric not supported by the input."""


def analyze_repo(entry: dict) -> dict:
    """Minimal-depth analysis of one repo entry (from list_public_repos):
    README (5k) + top-level listing + stack markers → a 2-3 sentence summary.
    At most one README fetch + one contents listing. Mutates and returns a
    plain dict (drops the internal PyGithub handle)."""
    import json

    from commands import gemma_client
    from commands.gemma_client import FAST_CHAIN

    repo = entry.get("_repo")
    readme = _fetch_readme(repo) if repo is not None else ""
    names = _fetch_toplevel(repo) if repo is not None else []
    stack = _stack_from_names(names)
    if entry.get("language") and entry["language"] not in stack:
        stack = [entry["language"], *stack]

    # Tutorial/course detection from name + description + README.
    haystack = " ".join(
        [entry.get("name", ""), entry.get("description", ""), readme[:1500]]
    )
    looks_tutorial = bool(_TUTORIAL_HINTS.search(haystack))

    summary = entry.get("description", "").strip()
    ctx = {
        "name": entry.get("name"),
        "description": entry.get("description"),
        "language": entry.get("language"),
        "stack_markers": stack,
        "top_level": names[:40],
        "readme_excerpt": readme,
    }
    prompt = (
        f"{_SUMMARY_INSTR}\n\n--- REPO ---\n"
        f"{json.dumps(ctx, ensure_ascii=False, default=str)[:6000]}\n--- END ---"
    )
    data = gemma_client.ask_json_text(
        prompt, max_output_tokens=400, temperature=0.2, chain=FAST_CHAIN
    )
    if data and data.get("summary"):
        summary = str(data["summary"]).strip()
    if not summary:
        summary = f"{entry.get('name')} — {entry.get('language') or 'code'} project. (Limited README signal.)"

    return {
        "name": entry.get("name"),
        "url": entry.get("url"),
        "description": entry.get("description", ""),
        "language": entry.get("language", ""),
        "stars": entry.get("stars", 0),
        "pushed_at": entry.get("pushed_at"),
        "is_fork": entry.get("is_fork", False),
        "archived": entry.get("archived", False),
        "has_readme": bool(readme),
        "stack": stack,
        "looks_tutorial": looks_tutorial,
        "summary": summary,
        # Kept so extract_project can reuse it without a second README fetch
        # (honors the "one README fetch per repo" constraint).
        "_readme_excerpt": readme,
        "_top_level": names[:40],
    }


_SCORE_INSTR = """\
Rate how RELEVANT this GitHub project is to the target job description, on a 0-100 \
scale, based purely on skills/tech/domain overlap. 100 = directly demonstrates the \
core stack and responsibilities the role asks for; 50 = adjacent/transferable; \
0 = unrelated. Return STRICT JSON: {"relevance": <int 0-100>, "reason": "<one \
short line tying the project to the job description>"}. Be honest and specific."""


def _relevance(analysis: dict, jd_text: str) -> dict:
    """AI relevance score (0-100) of one repo vs the job description."""
    import json

    from commands import gemma_client
    from commands.gemma_client import FAST_CHAIN

    proj = {
        "name": analysis.get("name"),
        "summary": analysis.get("summary"),
        "stack": analysis.get("stack"),
        "language": analysis.get("language"),
    }
    prompt = (
        f"{_SCORE_INSTR}\n\n--- JOB DESCRIPTION ---\n{(jd_text or '')[:4000]}\n"
        f"--- PROJECT ---\n{json.dumps(proj, ensure_ascii=False, default=str)[:2000]}\n--- END ---"
    )
    data = gemma_client.ask_json_text(
        prompt, max_output_tokens=250, temperature=0.0, chain=FAST_CHAIN
    )
    rel = 0
    reason = ""
    if data:
        try:
            rel = max(0, min(100, int(data.get("relevance", 0))))
        except (TypeError, ValueError):
            rel = 0
        reason = str(data.get("reason", "")).strip()
    return {"relevance": rel, "reason": reason}


def _recency_points(pushed_at) -> int:
    """0-100 recency score: full marks within ~2 years, decaying after."""
    if not pushed_at:
        return 30
    now = datetime.now(timezone.utc)
    if pushed_at.tzinfo is None:
        pushed_at = pushed_at.replace(tzinfo=timezone.utc)
    age = now - pushed_at
    if age <= timedelta(days=365 * 2):
        return 100
    if age <= timedelta(days=365 * 3):
        return 65
    if age <= timedelta(days=365 * 4):
        return 40
    return 20


def _completeness_points(analysis: dict) -> int:
    """0-100: has README + real description + a stack = looks functional."""
    pts = 0
    if analysis.get("has_readme"):
        pts += 55
    if (analysis.get("description") or "").strip():
        pts += 20
    if analysis.get("stack"):
        pts += 25
    return min(100, pts)


def _stars_points(stars: int) -> int:
    """0-100, log-ish: engagement is the lowest-weight signal."""
    if stars <= 0:
        return 0
    if stars >= 100:
        return 100
    if stars >= 25:
        return 80
    if stars >= 10:
        return 60
    if stars >= 3:
        return 40
    return 20


# Weighted blend. Relevance dominates; stars barely move the needle.
_WEIGHTS = {"relevance": 0.55, "completeness": 0.20, "recency": 0.15, "stars": 0.10}


def score_repos(analyses: list[dict], jd_text: str) -> list[dict]:
    """Score + rank analyzed repos against the job description. Forks and
    tutorial-along repos are penalized (kept, not dropped). Returns the list
    sorted best-first, each augmented with score/relevance/reason/subscores."""
    scored = []
    for a in analyses:
        rel = _relevance(a, jd_text)
        subs = {
            "relevance": rel["relevance"],
            "completeness": _completeness_points(a),
            "recency": _recency_points(a.get("pushed_at")),
            "stars": _stars_points(a.get("stars", 0)),
        }
        raw = sum(subs[k] * w for k, w in _WEIGHTS.items())
        # Penalize non-original work: it still shows up, just ranks lower.
        penalty = 1.0
        if a.get("is_fork"):
            penalty *= 0.45
        if a.get("looks_tutorial"):
            penalty *= 0.6
        if a.get("archived"):
            penalty *= 0.85
        score = round(raw * penalty, 1)
        scored.append(
            {
                **a,
                "score": score,
                "relevance": rel["relevance"],
                "reason": rel["reason"],
                "subscores": subs,
            }
        )
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored


def get_token() -> str | None:
    """Read the GitHub token from env. Returns None if unset. The value is
    never logged — callers pass it straight into the client."""
    tok = os.getenv("GITHUB_TOKEN")
    return tok or None


# --- Structured ATS extraction (persisted to github_projects) -----------------

# The model must choose EXACTLY ONE. Kept in sync with the CHECK-free enum
# documented in admin.sql. Anything off-list is coerced to "Other".
PROJECT_CATEGORIES = (
    "AI/ML",
    "Web Dev",
    "Mobile App",
    "Data Science",
    "Systems/Backend",
    "DevOps/Infra",
    "Game",
    "Other",
)

_EXTRACT_INSTR = """\
You are a senior software engineer acting as an ATS (applicant-tracking) scanner. \
Read the GitHub project's metadata, README excerpt, and file listing, and extract a \
STRUCTURED record — the way an ATS parses a résumé into fields. Be precise and \
factual: use ONLY what the inputs support, never invent a technology, feature, or \
date.

Return STRICT JSON, exactly these keys:
{
  "summary": "2-3 sentence factual summary: what the project does and the problem \
it solves. Neutral, professional, no marketing, no first person.",
  "tech": ["<language/framework/tool actually used>", "..."],  // concrete, deduped; \
[] if genuinely unknown
  "category": "<EXACTLY ONE of: AI/ML | Web Dev | Mobile App | Data Science | \
Systems/Backend | DevOps/Infra | Game | Other>"
}
Pick the SINGLE best-fit category (a web app with an ML feature is 'Web Dev' unless \
the model IS the product → 'AI/ML'). If signal is thin, infer conservatively from \
the language/stack and say what's certain in the summary."""


def _coerce_category(value: str) -> str:
    """Snap a model-returned category onto the fixed enum (case-insensitive);
    fall back to 'Other'."""
    v = (value or "").strip().lower()
    for c in PROJECT_CATEGORIES:
        if v == c.lower():
            return c
    # light alias tolerance
    aliases = {
        "ml": "AI/ML", "ai": "AI/ML", "machine learning": "AI/ML",
        "web": "Web Dev", "website": "Web Dev", "frontend": "Web Dev",
        "backend": "Systems/Backend", "systems": "Systems/Backend",
        "mobile": "Mobile App", "app": "Mobile App", "ios": "Mobile App",
        "android": "Mobile App", "data": "Data Science",
        "devops": "DevOps/Infra", "infra": "DevOps/Infra", "infrastructure": "DevOps/Infra",
        "gaming": "Game", "games": "Game",
    }
    return aliases.get(v, "Other")


def _finished_date(pushed_at) -> str | None:
    """Last-commit day (pushed_at) as an ISO date string, or None."""
    if not pushed_at:
        return None
    try:
        return pushed_at.date().isoformat()
    except Exception:
        return None


def extract_project(analysis: dict) -> dict:
    """ATS-style structured extraction for ONE repo, reusing the README/listing
    already fetched by analyze_repo (no extra GitHub calls). Returns a row ready
    for the github_projects table:
        {repo_name, summary, tech[], category, finished_at, url}
    finished_at = the repo's last-commit day. Uses the extraction model with
    thinking DISABLED (pure extraction, not reasoning)."""
    import json

    from commands import gemma_client
    from commands.gemma_client import EXTRACT_CHAIN

    ctx = {
        "name": analysis.get("name"),
        "description": analysis.get("description"),
        "language": analysis.get("language"),
        "stack_markers": analysis.get("stack"),
        "top_level": analysis.get("_top_level"),
        "readme_excerpt": analysis.get("_readme_excerpt"),
    }
    prompt = (
        f"{_EXTRACT_INSTR}\n\n--- PROJECT ---\n"
        f"{json.dumps(ctx, ensure_ascii=False, default=str)[:6000]}\n--- END ---"
    )
    data = gemma_client.ask_json_text(
        prompt,
        max_output_tokens=500,
        temperature=0.0,
        chain=EXTRACT_CHAIN,
        thinking=False,
    )
    summary = analysis.get("summary") or ""
    tech = list(analysis.get("stack") or [])
    category = "Other"
    if data:
        if data.get("summary"):
            summary = str(data["summary"]).strip()
        if isinstance(data.get("tech"), list) and data["tech"]:
            tech = [str(t).strip() for t in data["tech"] if str(t).strip()]
        category = _coerce_category(data.get("category"))

    return {
        "repo_name": analysis.get("name"),
        "summary": summary,
        "tech": tech[:20],
        "category": category,
        "finished_at": _finished_date(analysis.get("pushed_at")),
        "url": analysis.get("url"),
    }
