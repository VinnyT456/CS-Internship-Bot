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
