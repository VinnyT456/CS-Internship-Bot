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
    # The /problem/{id_or_slug} endpoint omits titleSlug/slug but carries a `url`
    # (https://leetcode.com/problems/<slug>/) — recover the slug from it so the
    # Reveal button (custom_id leet:reveal:<slug>) still works on manual fetches.
    if not slug:
        url = q.get("url") or ""
        m = re.search(r"/problems/([^/]+)", url)
        if m:
            slug = m.group(1).strip()
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


# --- slug → difficulty index (for annotating company problem lists) ----------
# Built lazily from the /problems/filter list endpoint (paginated), cached for the
# process. Lets /leetcode company show a difficulty dot per problem WITHOUT 15
# per-problem HTTP fetches — one warm-up, then O(1) lookups. Best-effort: any
# failure leaves the index empty and difficulty is simply omitted (⚪ dot).
_difficulty_map = None


def _difficulty_index():
    """{slug: 'Easy'|'Medium'|'Hard'} for all problems, built once and cached.
    Returns {} on failure (caller degrades gracefully)."""
    global _difficulty_map
    if _difficulty_map is not None:
        return _difficulty_map
    index = {}
    try:
        page, skip = 500, 0
        # The list endpoint reports `total`; page through it a bounded number of
        # times so a huge/looping response can't hang the warm-up.
        for _ in range(12):
            raw = _get(f"/problems/filter?limit={page}&skip={skip}")
            if not isinstance(raw, dict):
                break
            problems = raw.get("problems") or []
            if not problems:
                break
            for p in problems:
                slug = (p.get("title_slug") or p.get("titleSlug") or "").strip()
                diff = (p.get("difficulty") or "").strip()
                if slug and diff:
                    index[slug] = diff
            skip += page
            if skip >= int(raw.get("total") or 0):
                break
    except Exception:
        logger.exception("failed building difficulty index")
    _difficulty_map = index  # cache even a partial/empty result (avoid re-hammering)
    return _difficulty_map


def difficulty_for(slug):
    """Difficulty string for a slug ('Easy'/'Medium'/'Hard'), or '' if unknown."""
    if not slug:
        return ""
    return _difficulty_index().get(slug, "")


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


def _raw_patterns():
    """Patterns straight from the JSON (no computed tier). Internal — used to
    compute the learning sequence without recursing through roadmap_patterns."""
    return _load_roadmap().get("patterns", [])


# Cache the computed sequence/tiers per process (the roadmap JSON is static).
_SEQUENCE_CACHE = None
_TIER_CACHE = None


def roadmap_patterns():
    """All roadmap patterns, ordered by the LEARNING SEQUENCE (commonality-then-
    difficulty, prereq-respecting). Each pattern's `tier` is OVERWRITTEN with a
    display band computed from that sequence, so tiers reflect the gradual
    difficulty/commonality ramp rather than the hand-set JSON value. Full dict:
    key, name, category, tier, prereqs, recommended, tag, examples."""
    global _SEQUENCE_CACHE, _TIER_CACHE
    raw = _raw_patterns()
    if _SEQUENCE_CACHE is None:
        _SEQUENCE_CACHE = _compute_sequence(raw)
        n = max(1, len(_SEQUENCE_CACHE))
        _TIER_CACHE = {k: min(5, 1 + (i * 5) // n)
                       for i, k in enumerate(_SEQUENCE_CACHE)}
    order = {k: i for i, k in enumerate(_SEQUENCE_CACHE)}
    out = []
    for p in raw:
        q = dict(p)
        q["tier"] = _TIER_CACHE.get(p["key"], p.get("tier", 99))
        out.append(q)
    out.sort(key=lambda p: (p["tier"], order.get(p["key"], 999)))
    return out


def _compute_sequence(raw):
    """Kahn's algorithm over the raw patterns: repeatedly place the READY pattern
    (all core prereqs already placed) that ranks best by (commonality desc,
    difficulty asc, declared order). Prereq-valid AND difficulty/commonality-
    graded. Returns a list of keys."""
    by_key = {p["key"]: p for p in raw}
    decl = {p["key"]: i for i, p in enumerate(raw)}
    remaining = set(by_key)
    placed = set()
    out = []
    while remaining:
        ready = [k for k in remaining
                 if all(q in placed for q in (by_key[k].get("prereqs") or []))]
        if not ready:
            ready = list(remaining)
        ready.sort(key=lambda k: (_pattern_weight(by_key[k]), decl[k]))
        pick = ready[0]
        out.append(pick)
        placed.add(pick)
        remaining.discard(pick)
    return out


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


# --- Learn-only advanced topic pool (segment tree, dijkstra, KMP, …) ----------
# Taught via /learn + /pattern but NOT part of the roadmap graph. Loaded once.
_ADVANCED_PATH = os.path.join(os.path.dirname(__file__), "advanced_topics.json")
_advanced = None


def _load_advanced():
    global _advanced
    if _advanced is None:
        try:
            with open(_ADVANCED_PATH, encoding="utf-8") as f:
                _advanced = json.load(f)
        except Exception:
            logger.exception("failed loading advanced_topics.json")
            _advanced = {"categories": [], "topics": []}
    return _advanced


def advanced_topics():
    """All learn-only advanced topics (list of dicts: key, name, category, tag,
    examples). Ordered as declared in the JSON (grouped by category)."""
    return _load_advanced().get("topics", [])


def advanced_categories():
    """Category metadata for the advanced pool (key, name)."""
    return _load_advanced().get("categories", [])


def advanced_topic(key):
    """One advanced topic by key or loose name/tag match, or None. Tolerant like
    roadmap_pattern so /learn <topic> is forgiving."""
    if not key:
        return None
    want = key.strip().lower()
    want_slug = want.replace(" ", "-").replace("&", "and")
    topics = advanced_topics()
    by_key = {t["key"]: t for t in topics}
    if want in by_key:
        return by_key[want]
    if want_slug in by_key:
        return by_key[want_slug]
    for t in topics:
        name = t["name"].lower()
        if want in name or want == t.get("tag") or want_slug == t.get("tag"):
            return t
    return None


def learnable_topic(key):
    """Resolve a /learn or /pattern topic to EITHER a roadmap pattern or an
    advanced-pool topic. Returns (kind, dict) where kind is 'roadmap' | 'advanced',
    or (None, None) if unknown. Roadmap takes precedence."""
    p = roadmap_pattern(key)
    if p:
        return "roadmap", p
    a = advanced_topic(key)
    if a:
        return "advanced", a
    return None, None


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


def _pattern_weight(p):
    """Sort key for the learning order: DIFFICULTY first (easy→hard), then
    COMMONALITY (interview frequency, high→low), then declared order as a stable
    final tiebreak. Difficulty leads so the roadmap reads as clean difficulty
    BANDS (easy techniques up top, harder ones at the bottom); within a band the
    most common topic comes first. `frequency`/`difficulty` are 1-5 fields
    (default 3 if missing). Lower tuple = learn earlier."""
    freq = p.get("frequency", 3)
    diff = p.get("difficulty", 3)
    return (diff, -freq)


def roadmap_sequence():
    """The canonical study ORDER of all patterns: prereq-respecting, tie-broken so
    the MOST COMMON + EASIEST ready pattern comes next (see _compute_sequence).
    Returns a list of pattern KEYS. Seeds each user's personal roadmap queue."""
    global _SEQUENCE_CACHE
    if _SEQUENCE_CACHE is None:
        roadmap_patterns()  # populates the cache
    return list(_SEQUENCE_CACHE)


def roadmap_progress(learned_keys):
    """Roadmap state for rendering the progression path. Returns a dict:
        {
          "total": int, "learned_count": int, "percent": int,
          "current_tier": int,           # highest tier with any learned pattern
          "next": <pattern dict or None>,
          "tiers": [
             {"tier": 1, "patterns": [
                 {**pattern, "state": "learned"|"unlocked"|"locked",
                  "missing": [<unmet prereq NAME>, ...]}   # missing only when locked
             ]},
             ...
          ]
        }
    A pattern is 'unlocked' when every prereq is learned (ready to study now),
    'locked' when some prereq isn't, 'learned' when done. Pure data — the command
    just renders it."""
    learned = set(learned_keys or [])
    pats = roadmap_patterns()
    name_by_key = {p["key"]: p["name"] for p in pats}

    nxt = roadmap_next(learned)
    next_key = nxt["key"] if nxt else None

    tiers = {}
    for p in pats:
        prereqs = p.get("prereqs", []) or []
        if p["key"] in learned:
            state = "learned"
            missing = []
        elif all(q in learned for q in prereqs):
            state = "unlocked"
            missing = []
        else:
            state = "locked"
            missing = [name_by_key.get(q, q) for q in prereqs if q not in learned]
        entry = {**p, "state": state, "missing": missing, "is_next": p["key"] == next_key}
        tiers.setdefault(p.get("tier", 99), []).append(entry)

    tier_list = [{"tier": t, "patterns": tiers[t]} for t in sorted(tiers)]
    learned_tiers = [p.get("tier", 0) for p in pats if p["key"] in learned]
    total = len(pats)
    lc = len(learned & {p["key"] for p in pats})
    return {
        "total": total,
        "learned_count": lc,
        "percent": round(100 * lc / total) if total else 0,
        "current_tier": max(learned_tiers) if learned_tiers else 0,
        "next": nxt,
        "tiers": tier_list,
    }


# Dependency-ordered flow of the CATEGORY clusters (foundation → downstream).
# Drives the roadmap's panel layout so it reads as a learning path, not a grid.
_CLUSTER_FLOW = ("data_structures", "techniques", "searching", "dp", "advanced")


def roadmap_clusters(learned_keys):
    """Roadmap grouped into CATEGORY clusters for the panel layout. Returns:
        {
          <all the roadmap_progress top-level stats>,
          "clusters": [
             {"key","name","emoji","learned","total",
              "patterns": [ {**pattern, "state", "missing", "is_next"} ... ]},
             ...   # in dependency-flow order
          ],
          "cluster_edges": [ (src_cat, dst_cat), ... ]   # cluster-level prereqs
        }
    Node state is identical to roadmap_progress; patterns within a cluster are
    tier-ordered. cluster_edges are the DEDUPED cross-category prerequisite links
    (an edge cat A→B means some pattern in B depends on a pattern in A)."""
    base = roadmap_progress(learned_keys)
    pats = roadmap_patterns()
    cat_of = {p["key"]: p["category"] for p in pats}
    cat_meta = {c["key"]: c for c in roadmap_categories()}

    # Flatten the per-node state out of the tier view.
    node_by_key = {}
    for tb in base["tiers"]:
        for p in tb["patterns"]:
            node_by_key[p["key"]] = p

    # Build clusters in flow order (unknown categories appended after).
    order = list(_CLUSTER_FLOW) + [
        c for c in cat_meta if c not in _CLUSTER_FLOW
    ]
    clusters = []
    for ckey in order:
        members = [node_by_key[p["key"]] for p in pats
                   if p["category"] == ckey and p["key"] in node_by_key]
        if not members:
            continue
        members.sort(key=lambda p: (p.get("tier", 99),))
        done = sum(1 for m in members if m["state"] == "learned")
        meta = cat_meta.get(ckey, {})
        clusters.append({
            "key": ckey,
            "name": meta.get("name", ckey),
            "emoji": meta.get("emoji", ""),
            "learned": done,
            "total": len(members),
            "patterns": members,
        })

    # Cluster-level prereq edges: dedup cross-category prerequisite links.
    edges = set()
    for p in pats:
        dst = p["category"]
        for q in p.get("prereqs", []) or []:
            src = cat_of.get(q)
            if src and src != dst:
                edges.add((src, dst))
    # Order edges by the flow so drawing is deterministic.
    flow_idx = {c: i for i, c in enumerate(order)}
    cluster_edges = sorted(edges, key=lambda e: (flow_idx.get(e[0], 99), flow_idx.get(e[1], 99)))

    return {**base, "clusters": clusters, "cluster_edges": cluster_edges}


# Which TRACK each category belongs to when the roadmap is split into two graphs
# (data structures vs algorithms/techniques), so each image is small + readable.
_TRACK_OF_CATEGORY = {
    "data_structures": "structures",
    "advanced": "structures",   # Graphs / Tries / Union-Find are structures too...
    "techniques": "algorithms",
    "searching": "algorithms",
    "dp": "algorithms",
}
# ...except Bit Manipulation, which lives in 'advanced' but is an algo technique.
_TRACK_OVERRIDE = {"bit_manipulation": "algorithms"}

_TRACK_META = {
    "structures": {"name": "Data Structures", "title": "DATA STRUCTURES"},
    "algorithms": {"name": "Algorithms & Techniques", "title": "ALGORITHMS & TECHNIQUES"},
}


def _track_of(pattern):
    k = pattern["key"]
    if k in _TRACK_OVERRIDE:
        return _TRACK_OVERRIDE[k]
    return _TRACK_OF_CATEGORY.get(pattern["category"], "algorithms")


def roadmap_tracks(learned_keys, sequence=None):
    """Split the roadmap into TWO self-contained graphs — 'structures' and
    'algorithms' — so each renders small + readable while keeping the roadmap
    feel. Returns:
        {
          ...roadmap_guidance top-level stats (now, next_up, percent, ...),
          "tracks": {
            "structures": {"name","title","tiers":[{tier,patterns:[...]}],
                           "has_now": bool},
            "algorithms": {...},
          }
        }
    Each pattern node carries state/is_start/is_next_up (from guidance) PLUS
    `cross_prereqs`: the NAMES of its prerequisites that live in the OTHER track,
    so the renderer can show a dim '↖ needs X' ghost label instead of a dangling
    cross-graph arrow. `has_now` flags which track holds the START-HERE pick."""
    g = roadmap_guidance(learned_keys, sequence=sequence)
    pats = roadmap_patterns()
    track_of = {p["key"]: _track_of(p) for p in pats}
    name_by_key = {p["key"]: p["name"] for p in pats}

    # Pull the tagged nodes out of the guidance tier view.
    node_by_key = {}
    for tb in g["tiers"]:
        for n in tb["patterns"]:
            node_by_key[n["key"]] = n

    now_key = g["now"]["key"] if g.get("now") else None

    tracks = {}
    for tkey, meta in _TRACK_META.items():
        # Nodes in this track, grouped into tiers (preserve tier ordering).
        tiers_map = {}
        has_now = False
        for p in pats:
            if track_of[p["key"]] != tkey:
                continue
            node = dict(node_by_key.get(p["key"], {}))  # copy so we can annotate
            # Cross-track prereqs → names, for the ghost label.
            cross = [name_by_key.get(q, q)
                     for q in (p.get("prereqs") or [])
                     if track_of.get(q) and track_of[q] != tkey]
            # A soft/recommended prereq in the other track is also a cross ref,
            # but we don't clutter the ghost label with it — only hard cross deps.
            node["cross_prereqs"] = cross
            # In-track edges, split by strength: solid (core prereq) vs dashed
            # (recommended). Only same-track links are drawn as arrows.
            node["in_prereqs"] = [q for q in (p.get("prereqs") or [])
                                  if track_of.get(q) == tkey]
            node["in_recommended"] = [q for q in (p.get("recommended") or [])
                                      if track_of.get(q) == tkey]
            if node.get("key") == now_key:
                has_now = True
            tiers_map.setdefault(p.get("tier", 99), []).append(node)
        tiers = [{"tier": t, "patterns": tiers_map[t]} for t in sorted(tiers_map)]
        tracks[tkey] = {
            "name": meta["name"], "title": meta["title"],
            "tiers": tiers, "has_now": has_now,
        }

    # STUDY-ORDER SPINE: connect the islands. A node with NO incoming in-track
    # edge (no core prereq, no recommended) would otherwise float. Give it a
    # `spine_parent` = the nearest EARLIER node in the same track along the
    # learning sequence, so you can follow arrows through every topic. This is a
    # path-continuation link, drawn distinctly from real prereqs — it means "next
    # in order," not "required".
    seq = sequence or roadmap_sequence()
    seq_idx = {k: i for i, k in enumerate(seq)}
    for tkey, tr in tracks.items():
        nodes = [n for tb in tr["tiers"] for n in tb["patterns"]]
        track_keys = {n["key"] for n in nodes}
        for n in nodes:
            has_edge = (n.get("in_prereqs") or n.get("in_recommended")
                        or n.get("cross_prereqs"))
            n["spine_parent"] = None
            if has_edge:
                continue
            # nearest earlier same-track node in the sequence
            my = seq_idx.get(n["key"], 0)
            best = None
            for other in nodes:
                oi = seq_idx.get(other["key"], -1)
                if other["key"] != n["key"] and oi < my:
                    if best is None or oi > seq_idx.get(best, -1):
                        best = other["key"]
            n["spine_parent"] = best

    return {**{k: v for k, v in g.items() if k not in ("tiers", "clusters")},
            "tracks": tracks}


def roadmap_guidance(learned_keys, sequence=None):
    """Clustered roadmap PLUS explicit 'what to do now / next' guidance. Adds:
        {
          ...roadmap_clusters output...,
          "now": <pattern dict or None>,      # the single START-HERE pick
          "next_up": [<pattern dict>, ...],   # what to practice after `now`
          "topics_left": [<key>, ...],        # ordered, still to do
          "topics_done": [<key>, ...],        # ordered, completed
          # every node in clusters/tiers also gets: is_start, is_next_up
        }

    PERSONAL QUEUE mode (`sequence` given): `now` = the first pattern in the
    user's stored study order that they haven't learned yet (queue popleft over
    the done-set), and `next_up` = the following unlearned patterns in that same
    order. `sequence` is the user's ordered pattern keys (see
    db.get_or_seed_roadmap_sequence). 'Done' stays in leetcode_learned, so left/
    done are derived here and can't drift.

    Fallback (`sequence` is None): `now` = roadmap_next (lowest-tier prereq-ready
    unlearned) and `next_up` = the one-hop-ahead patterns from the DAG."""
    data = roadmap_clusters(learned_keys)
    learned = set(learned_keys or [])
    pats = roadmap_patterns()
    valid_keys = {p["key"] for p in pats}
    by_key = {p["key"]: p for p in pats}

    topics_left, topics_done = [], []
    if sequence:
        # Personal queue: keep only known keys, dedup, and append any roadmap
        # pattern the stored sequence is missing (e.g. a newly-added pattern) in
        # topo order so nothing is unreachable.
        seen = set()
        seq = []
        for k in sequence:
            if k in valid_keys and k not in seen:
                seq.append(k)
                seen.add(k)
        for k in roadmap_sequence():
            if k not in seen:
                seq.append(k)
                seen.add(k)

        for k in seq:
            (topics_done if k in learned else topics_left).append(k)

        now = by_key.get(topics_left[0]) if topics_left else None
        next_up = [by_key[k] for k in topics_left[1:4]]
    else:
        now = data.get("next")  # roadmap_next result
        # "Ready now" = unlearned with all prereqs learned; next_up = one hop out.
        ready = {p["key"] for p in pats
                 if p["key"] not in learned
                 and all(q in learned for q in p.get("prereqs", []) or [])}
        hypothetically = learned | ready
        nu = []
        for p in pats:
            k = p["key"]
            if k in learned or k in ready:
                continue
            prereqs = set(p.get("prereqs", []) or [])
            if prereqs and prereqs <= hypothetically:
                nu.append(k)
        next_up = sorted((by_key[k] for k in set(nu)),
                         key=lambda p: (p.get("tier", 99),))[:3]
        # Derive left/done in topo order for consistency with the queue mode.
        for k in roadmap_sequence():
            (topics_done if k in learned else topics_left).append(k)

    start_key = now["key"] if now else None
    next_up_set = {p["key"] for p in next_up}

    def _tag(node):
        node["is_start"] = node["key"] == start_key
        node["is_next_up"] = node["key"] in next_up_set
    for tb in data["tiers"]:
        for n in tb["patterns"]:
            _tag(n)
    for cl in data["clusters"]:
        for n in cl["patterns"]:
            _tag(n)

    return {
        **data,
        "now": now,
        "next_up": next_up[:3],
        "topics_left": topics_left,
        "topics_done": topics_done,
    }
