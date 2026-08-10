"""Thin client for the public LeetCode API (noworneverev/leetcode-api).

The bot is Python on Render; the two Node LeetCode APIs the user linked would each
need their own host. noworneverev/leetcode-api is FastAPI and is already deployed
publicly at leetcode-api-pied.vercel.app with open access (no key), so we call it
directly over httpx — no extra service to keep awake.

Endpoints used:
  GET /daily              -> today's challenge: {date, link, question{...}}
  GET /problem/{id|slug}  -> full problem: content, hints, topicTags, codeSnippets…

`companyTags` from the API is null for unauthenticated callers (LeetCode gates
company data behind Premium), so "which companies ask this" comes from a vendored
snapshot (company_tags.json, built from snehasishroy/leetcode-companywise-…) via
companies_for().
"""

import json
import logging
import os
import re
from html import unescape

import httpx

logger = logging.getLogger("leetcode_api")

_BASE = os.getenv("LEETCODE_API_BASE", "https://leetcode-api-pied.vercel.app")
_TIMEOUT = 20.0
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36"

# Vendored slug -> [company, …] map (frequency-sorted, top 12). Loaded once.
_COMPANY_PATH = os.path.join(os.path.dirname(__file__), "company_tags.json")
_company_map = None

# Vendored DS&A learning roadmap (ordered curriculum). Loaded once.
_ROADMAP_PATH = os.path.join(os.path.dirname(__file__), "dsa_roadmap.json")
_roadmap = None

# Per-pattern ground-truth (signal/state/template/misconception) distilled from the
# user's learning notes — fed into the /learn lesson so facts are accurate.
_KNOWLEDGE_PATH = os.path.join(os.path.dirname(__file__), "dsa_knowledge.json")
_knowledge = None


def _client():
    return httpx.Client(
        timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True
    )


def _get(path):
    """GET a JSON endpoint. Returns the parsed body, or None on any failure."""
    try:
        with _client() as c:
            r = c.get(f"{_BASE}{path}")
            r.raise_for_status()
            return r.json()
    except Exception:
        logger.exception("leetcode api GET %s failed", path)
        return None


def get_daily():
    """Today's daily challenge. Returns a normalized problem dict or None."""
    raw = _get("/daily")
    if not raw or not raw.get("question"):
        return None
    prob = _normalize(raw["question"])
    prob["date"] = raw.get("date")
    # /daily's question payload omits hints/codeSnippets; refetch the full record
    # by slug so the explainer + reveal have everything.
    full = get_problem(prob["slug"]) if prob.get("slug") else None
    if full:
        for k in ("content", "hints", "code_snippets", "similar", "url"):
            if full.get(k):
                prob[k] = full[k]
    return prob


def get_problem(id_or_slug):
    """Full problem by numeric id or title slug. Returns a normalized dict or None."""
    raw = _get(f"/problem/{id_or_slug}")
    if not raw or not raw.get("title"):
        return None
    return _normalize(raw)


def _normalize(q):
    """Map a raw API question object to the fields the bot uses."""
    slug = (q.get("titleSlug") or q.get("slug") or "").strip()
    snippets = {
        s.get("langSlug"): s.get("code")
        for s in (q.get("codeSnippets") or [])
        if s.get("langSlug")
    }
    return {
        "id": str(q.get("questionFrontendId") or q.get("questionId") or "").strip(),
        "title": (q.get("title") or "").strip(),
        "slug": slug,
        "difficulty": (q.get("difficulty") or "").strip(),
        "ac_rate": q.get("acRate"),
        "paid_only": bool(q.get("isPaidOnly")),
        "tags": [t.get("name") for t in (q.get("topicTags") or []) if t.get("name")],
        "content": _html_to_text(q.get("content") or ""),
        "content_html": q.get("content") or "",
        "hints": q.get("hints") or [],
        "code_snippets": snippets,
        "url": q.get("url") or (f"https://leetcode.com/problems/{slug}/" if slug else ""),
        "similar": q.get("similarQuestions"),
    }


# --- problem lists: filter / by-tag / random ---------------------------------
_DIFF_MAP = {"easy": "EASY", "medium": "MEDIUM", "hard": "HARD"}


def _list_row(p):
    """Normalize one row from a /problems/* list endpoint (lighter than a full
    problem — list rows don't carry content/hints)."""
    slug = (p.get("title_slug") or p.get("titleSlug") or "").strip()
    return {
        "id": str(p.get("frontend_id") or p.get("id") or "").strip(),
        "title": (p.get("title") or "").strip(),
        "slug": slug,
        "difficulty": (p.get("difficulty") or "").strip(),
        "paid_only": bool(p.get("paid_only")),
        "url": p.get("url") or (f"https://leetcode.com/problems/{slug}/" if slug else ""),
    }


def problems_by_difficulty(difficulty=None, limit=50, skip=0):
    """A page of problems, optionally filtered by difficulty ('easy'/'medium'/
    'hard'). Returns (rows, total) — rows are light list rows."""
    q = f"/problems/filter?limit={limit}&skip={skip}"
    if difficulty:
        d = _DIFF_MAP.get(difficulty.strip().lower())
        if d:
            q += f"&difficulty={d}"
    raw = _get(q)
    if not isinstance(raw, dict):
        return [], 0
    rows = [_list_row(p) for p in raw.get("problems", []) if not p.get("paid_only")]
    return rows, int(raw.get("total") or len(rows))


def problems_by_tag(tag_slug, limit=50, skip=0):
    """Problems carrying a topic tag (e.g. 'two-pointers', 'dynamic-programming').
    Returns (rows, total)."""
    raw = _get(f"/problems/tag/{tag_slug}?limit={limit}&skip={skip}")
    if not isinstance(raw, dict):
        return [], 0
    rows = [_list_row(p) for p in raw.get("problems", []) if not p.get("paid_only")]
    return rows, int(raw.get("total") or len(rows))


def random_problem(difficulty=None, tag_slug=None, rng=None):
    """Pick a random FREE problem, optionally constrained by difficulty and/or
    topic tag. Returns a full normalized problem dict (fetched by slug) or None.
    `rng` lets the caller inject randomness (the module avoids importing random at
    top level so it stays cheap to import)."""
    import random as _random

    rng = rng or _random
    # Pull a page, then random-skip within the total for spread beyond page 1.
    if tag_slug:
        rows, total = problems_by_tag(tag_slug, limit=50)
    else:
        rows, total = problems_by_difficulty(difficulty, limit=50)
    if not rows:
        return None
    # If there are more pages, jump to a random page for variety.
    if total > 50:
        skip = rng.randint(0, max(0, total - 1)) // 50 * 50
        if tag_slug:
            page, _ = problems_by_tag(tag_slug, limit=50, skip=skip)
        else:
            page, _ = problems_by_difficulty(difficulty, limit=50, skip=skip)
        rows = page or rows
    # Tag pages ignore difficulty; when both are given, filter the fetched rows by
    # difficulty. If this page has no match, fall back to an unfiltered pick rather
    # than returning nothing — the caller asked for a topic first, difficulty second.
    if tag_slug and difficulty:
        want = difficulty.strip().lower()
        matches = [r for r in rows if r["difficulty"].lower() == want]
        rows = matches or rows
    return get_problem(rng.choice(rows)["slug"])


# --- HTML → readable text (problem bodies come as LeetCode HTML) --------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def _html_to_text(html):
    if not html:
        return ""
    t = html
    # Preserve superscripts as ^ before stripping tags — constraints like
    # 2 * 10<sup>5</sup> otherwise collapse to "2 * 105", which reads as a wrong
    # (much smaller) bound and misleads which complexity is allowed.
    t = re.sub(r"<sup>(.*?)</sup>", r"^\1", t, flags=re.I | re.S)
    t = re.sub(r"</p>|<br\s*/?>|</div>|</li>", "\n", t, flags=re.I)
    t = re.sub(r"<li>", "• ", t, flags=re.I)
    t = _TAG_RE.sub("", t)
    t = unescape(t)
    t = _WS_RE.sub(" ", t)
    t = _NL_RE.sub("\n\n", t)
    return t.strip()


# --- vendored company tags ----------------------------------------------------
def _load_companies():
    global _company_map
    if _company_map is None:
        try:
            with open(_COMPANY_PATH, encoding="utf-8") as f:
                _company_map = json.load(f)
        except Exception:
            logger.exception("failed loading company_tags.json")
            _company_map = {}
    return _company_map


def companies_for(slug, limit=8):
    """Companies that ask this problem (frequency-sorted), from the vendored
    snapshot. Empty list if unknown. The API's own companyTags is null for
    unauthenticated callers, so this is the source of that signal."""
    if not slug:
        return []
    return _load_companies().get(slug, [])[:limit]


# Reverse index: company -> [problem slugs], built lazily from the same snapshot.
_company_index = None


def _build_company_index():
    global _company_index
    if _company_index is None:
        idx = {}
        for slug, comps in _load_companies().items():
            for c in comps:
                idx.setdefault(c, []).append(slug)
        _company_index = idx
    return _company_index


def problems_for_company(company, limit=25):
    """Problem slugs a company asks (from the vendored snapshot). Case/format
    tolerant on the name. Returns [] if the company isn't in the snapshot."""
    if not company:
        return []
    idx = _build_company_index()
    key = company.strip().lower().replace(" ", "-").replace(".", "")
    if key in idx:
        return idx[key][:limit]
    # loose contains-match fallback (e.g. "goldman" -> "goldman-sachs")
    for name, slugs in idx.items():
        if key in name or name in key:
            return slugs[:limit]
    return []


# --- vendored DS&A learning roadmap ------------------------------------------
def _load_roadmap():
    global _roadmap
    if _roadmap is None:
        try:
            with open(_ROADMAP_PATH, encoding="utf-8") as f:
                _roadmap = json.load(f)
        except Exception:
            logger.exception("failed loading dsa_roadmap.json")
            _roadmap = {"categories": [], "patterns": []}
    return _roadmap


def roadmap_patterns():
    """All roadmap patterns, in tier order then declared order. Each is the full
    dict (key, name, category, tier, prereqs, tag, examples)."""
    pats = _load_roadmap().get("patterns", [])
    return sorted(pats, key=lambda p: (p.get("tier", 99),))


def roadmap_categories():
    """Category metadata (key, name, emoji) in declared order."""
    return _load_roadmap().get("categories", [])


# Common shorthand / synonyms a user might type in /learn -> roadmap key.
_PATTERN_ALIASES = {
    "hashmap": "hashing", "hash map": "hashing", "hashtable": "hashing",
    "hash table": "hashing", "dictionary": "hashing", "dict": "hashing",
    "set": "hashing", "map": "hashing",
    "dp": "dp_1d", "dynamic programming": "dp_1d", "memoization": "dp_1d",
    "2d dp": "dp_2d", "grid dp": "dp_2d",
    "xor": "bit_manipulation", "bits": "bit_manipulation", "bitmask": "bit_manipulation",
    "dsu": "union_find", "disjoint set": "union_find",
    "trie": "tries", "prefix tree": "tries",
    "queue": "stack_queue", "stack": "stack_queue", "deque": "stack_queue",
    "pq": "heap", "priority queue": "heap",
    "backtracking": "dfs", "recursion": "dfs",
    "cycle": "fast_slow", "floyd": "fast_slow",
    "binary search tree": "bst", "search tree": "bst",
    "grid": "matrix", "2d array": "matrix",
    "string": "arrays", "strings": "arrays", "array": "arrays",
    "two pointer": "two_pointers", "2 pointers": "two_pointers",
    "window": "sliding_window",
    "graph": "graphs", "topological sort": "graphs", "topo sort": "graphs",
    "mono stack": "monotonic_stack",
}


def roadmap_pattern(key):
    """One pattern by key (or a loose name / alias match), or None. Tolerant:
    matches the key, a known shorthand alias, the name, or a slugified form so
    /learn <topic> is forgiving (e.g. 'dp', 'hashmap', 'xor', 'queue')."""
    if not key:
        return None
    want = key.strip().lower()
    want_slug = want.replace(" ", "-").replace("&", "and")
    patterns = _load_roadmap().get("patterns", [])
    by_key = {p["key"]: p for p in patterns}

    # 1) exact key, or a known alias -> its canonical key.
    if want in by_key:
        return by_key[want]
    if want_slug in by_key:
        return by_key[want_slug]
    aliased = _PATTERN_ALIASES.get(want)
    if aliased and aliased in by_key:
        return by_key[aliased]

    # 2) loose name / tag match.
    for p in patterns:
        name = p["name"].lower()
        if want in name or want in p.get("tag", "") or want_slug == p.get("tag"):
            return p
    return None


def pattern_knowledge(pattern_key):
    """Doc-grounded facts for a roadmap pattern key (signal, key_question, state,
    template, complexity, misconception, mental_model), or None if the pattern
    isn't in the knowledge base. Fed into the /learn lesson as ground truth."""
    global _knowledge
    if _knowledge is None:
        try:
            with open(_KNOWLEDGE_PATH, encoding="utf-8") as f:
                _knowledge = json.load(f).get("patterns", {})
        except Exception:
            logger.exception("failed loading dsa_knowledge.json")
            _knowledge = {}
    return _knowledge.get(pattern_key)


def roadmap_patterns_for_tags(tag_names):
    """Map a problem's LeetCode topic tags (display names like 'Two Pointers',
    'Dynamic Programming') to roadmap patterns that teach them. Returns a list of
    (key, name) — deduped, ordered by roadmap tier. Powers the daily 'this uses X,
    learn it' tie-in."""
    if not tag_names:
        return []
    wanted = {t.strip().lower() for t in tag_names}
    wanted_slugs = {w.replace(" ", "-") for w in wanted}
    # Precise: a roadmap pattern matches a tag only if its own tag slug is one the
    # problem carries, OR the pattern name equals a tag exactly. Fuzzy substring
    # matching over-pulled unrelated patterns (Two Pointers dragging in Difference
    # Array, Intervals, …), so we require a slug/name hit, not a contains.
    out, seen = [], set()
    for p in roadmap_patterns():
        pn = p["name"].lower()
        ptag = p.get("tag", "")
        hit = ptag in wanted_slugs or pn in wanted or any(
            # allow the pattern's canonical tag to equal a wanted slug
            ptag == s for s in wanted_slugs
        )
        if hit and p["key"] not in seen:
            seen.add(p["key"])
            out.append((p["key"], p["name"]))
    return out


def roadmap_next(learned_keys):
    """The next pattern a learner should tackle: the lowest-tier pattern not yet
    learned whose prereqs are ALL learned. Falls back to the first unlearned if
    none are prereq-ready. Returns a pattern dict or None (all learned)."""
    learned = set(learned_keys or [])
    unlearned = [p for p in roadmap_patterns() if p["key"] not in learned]
    if not unlearned:
        return None
    ready = [p for p in unlearned if all(q in learned for q in p.get("prereqs", []))]
    return (ready or unlearned)[0]
