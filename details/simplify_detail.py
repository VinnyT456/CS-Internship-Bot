"""Detail scraper for simplify.jobs postings.

simplify.jobs is a Next.js app that server-renders the whole posting into a
`__NEXT_DATA__` blob, so one GET yields fully structured data — no HTML
scraping and no ld+json section-guessing like jobright needs.

The payload lives at props.pageProps.jobPosting and carries the fields we
want already split into lists (requirements/responsibilities/desirable),
plus salary as numbers with a period code and a work-model code.

Field encodings below were confirmed by sampling ~90 live postings across
Summer2026-Internships and New-Grad-Positions.
"""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("simplify_detail")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_MAX_WORKERS = int(os.getenv("SIMPLIFY_ENRICH_WORKERS", "4"))

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)

# salary_period: 1 = hourly, 4 = yearly. Internship rows are almost all 1,
# new-grad rows almost all 4; anything else we leave uncosted rather than
# guess a multiplier.
HOURS_PER_YEAR = 2080
PERIOD_HOURLY = 1
PERIOD_YEARLY = 4

# travel_requirements is misnamed upstream — it is the work-model chip the
# page renders next to the location.
WORK_MODEL_BY_CODE = {1: "Remote", 2: "Hybrid", 3: "On-site"}

# The boolean seniority flags, most senior first so the label wins the tie.
SENIORITY_FLAGS = (
    ("expert", "Expert"),
    ("senior", "Senior"),
    ("mid_level", "Mid Level"),
    ("junior", "Junior"),
    ("entry_level", "Entry Level"),
)

_client_lock = threading.Lock()
_http_client: httpx.Client | None = None


def normalize_job_url(job_url: str | None) -> str | None:
    """Strip query/fragment so the ?utm_source=GHList variants collapse."""
    if not job_url:
        return None

    parsed = urlparse(job_url.strip())
    if not parsed.scheme or not parsed.netloc:
        return job_url.strip()

    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _get_http_client() -> httpx.Client:
    global _http_client
    with _client_lock:
        if _http_client is None:
            _http_client = httpx.Client(
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return _http_client


def close_http_client() -> None:
    """Release pooled connections after an enrich pass so idle sockets don't
    accumulate on Render's 512 MB instance."""
    global _http_client
    with _client_lock:
        if _http_client is not None:
            _http_client.close()
            _http_client = None


def _unique_strings(values) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _to_thousands(amount) -> int | None:
    """Annual dollars -> thousands, matching the jobright module's unit."""
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(1, round(value / 1000))


def _format_money(amount: float, currency: str, suffix: str) -> str:
    prefix = "$" if currency == "USD" else f"{currency} "
    if amount >= 1000:
        text = f"{amount / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{prefix}{text}k{suffix}"
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    return f"{prefix}{text}{suffix}"


def _salary(posting: dict) -> tuple[int | None, int | None, str | None]:
    """-> (comp_min_k, comp_max_k, human string). Comp is annualized
    thousands so hourly and yearly postings stay comparable."""
    period = posting.get("salary_period")
    raw_min = posting.get("min_salary")
    raw_max = posting.get("max_salary")
    currency = (posting.get("currency_type") or "USD").upper()

    if raw_min is None and raw_max is None:
        return None, None, None
    if raw_min is None:
        raw_min = raw_max
    if raw_max is None:
        raw_max = raw_min

    try:
        low = float(raw_min)
        high = float(raw_max)
    except (TypeError, ValueError):
        return None, None, None

    if period == PERIOD_HOURLY:
        suffix = "/hr"
        comp_min = _to_thousands(low * HOURS_PER_YEAR)
        comp_max = _to_thousands(high * HOURS_PER_YEAR)
    elif period == PERIOD_YEARLY:
        suffix = "/yr"
        comp_min = _to_thousands(low)
        comp_max = _to_thousands(high)
    else:
        # Unknown period code — surface the numbers but don't annualize them
        # into a comp range we can't justify.
        logger.info("Unknown salary_period %r; skipping comp range", period)
        return None, None, None

    if low == high:
        desc = _format_money(low, currency, suffix)
    else:
        desc = f"{_format_money(low, currency, '')} - {_format_money(high, currency, suffix)}"

    return comp_min, comp_max, desc


def _summary(posting: dict) -> str | None:
    """First paragraph of the description, falling back to a flat prefix."""
    description = posting.get("description") or ""
    if not description:
        return None

    soup = BeautifulSoup(description, "html.parser")
    first = soup.find("p")
    if first:
        text = first.get_text(" ", strip=True)
        if text:
            return text

    plain = soup.get_text(" ", strip=True)
    return plain[:500] or None


def _seniority(posting: dict) -> str | None:
    for key, label in SENIORITY_FLAGS:
        if posting.get(key):
            return label
    return None


def _location(posting: dict) -> str | None:
    locations = posting.get("locations") or []
    values = _unique_strings(loc.get("value") for loc in locations if isinstance(loc, dict))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return f"{values[0]} (+{len(values) - 1} more)"


def _tags(posting: dict) -> list[str]:
    # Skills = the qualification tags only. 'functions' (job categories like
    # "Full-Stack Engineering") is dropped — it's taxonomy, not a skill.
    tags = [s.get("name") for s in (posting.get("skills") or []) if isinstance(s, dict)]
    return _unique_strings(tags)


def _is_closed(posting: dict) -> bool:
    """A posting is gone when it's archived, hidden, or explicitly inactive.
    Only `active is False` counts — a missing key means unknown, not closed."""
    if posting.get("archive") is True:
        return True
    if posting.get("visible") is False:
        return True
    return posting.get("active") is False


def _from_posting(posting: dict) -> dict:
    comp_min, comp_max, salary_desc = _salary(posting)

    # `desirable` is simplify's nice-to-have bucket; it has no column of its
    # own, and it reads naturally alongside the hard requirements.
    requirements = _unique_strings(posting.get("requirements"))
    requirements.extend(_unique_strings(posting.get("desirable")))

    benefits = []
    if posting.get("sponsors_h1b") is True:
        benefits.append("H1B sponsorship available")
    extra = posting.get("additional_location_info")
    if extra:
        benefits.append(str(extra).strip())

    return {
        "job_summary": _summary(posting),
        "job_responsibilities": _unique_strings(posting.get("responsibilities")),
        "job_requirements": _unique_strings(requirements),
        "job_benefits": _unique_strings(benefits),
        "comp_min": comp_min,
        "comp_max": comp_max,
        "salary_desc": salary_desc,
        "employment_type": None,
        "seniority": _seniority(posting),
        "job_tags": _tags(posting),
        "work_model": WORK_MODEL_BY_CODE.get(posting.get("travel_requirements")),
        "job_location": _location(posting),
        "is_closed": _is_closed(posting),
    }


def _is_simplify_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "simplify.jobs" or host.endswith(".simplify.jobs")


def _fetch_html(job_url: str, *, attempts: int = 3) -> str | None:
    client = _get_http_client()
    last_exc: Exception | None = None

    for attempt in range(attempts):
        try:
            response = client.get(job_url)

            if response.status_code in {403, 404, 410}:
                return None

            response.raise_for_status()

            # simplify.jobs adds a title slug on redirect; anything that lands
            # off-host means the posting was pulled.
            if not _is_simplify_host(str(response.url)):
                logger.info("Skipping non-simplify response for %s", job_url)
                return None

            return response.text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 404, 410}:
                return None
            last_exc = exc
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc

        if attempt < attempts - 1:
            time.sleep(1.0 * (attempt + 1))

    logger.warning(
        "Failed fetching job detail %s after %d attempts: %s",
        job_url,
        attempts,
        last_exc,
    )
    return None


def _posting_from_html(html: str) -> dict | None:
    match = NEXT_DATA_RE.search(html)
    if not match:
        logger.warning("No __NEXT_DATA__ block in simplify detail page")
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("Invalid __NEXT_DATA__ in simplify detail page")
        return None

    posting = payload.get("props", {}).get("pageProps", {}).get("jobPosting")
    return posting if isinstance(posting, dict) and posting else None


def check_job_closed(job_url: str) -> bool | None:
    """True if closed, False if open, None if undetermined.

    None means the page couldn't be fetched or parsed — callers should leave
    the row's status alone rather than assume closed.
    """
    normalized = normalize_job_url(job_url)
    if not normalized or "simplify.jobs" not in normalized:
        return None
    html = _fetch_html(normalized)
    if html is None:
        return None
    posting = _posting_from_html(html)
    if posting is None:
        return None
    return _is_closed(posting)


def fetch_job_detail(job_url: str) -> dict | None:
    normalized = normalize_job_url(job_url)
    if not normalized or "simplify.jobs" not in normalized:
        return None

    html = _fetch_html(normalized)
    if not html:
        return None

    posting = _posting_from_html(html)
    if posting is None:
        return None

    detail = _from_posting(posting)
    if (
        detail.get("job_summary")
        or detail.get("job_responsibilities")
        or detail.get("comp_min")
    ):
        return detail
    return None


def _is_enriched(row: dict) -> bool:
    return bool(
        (row.get("job_summary") or "").strip() or row.get("job_responsibilities")
    )


def enrich_job(row: dict) -> dict:
    detail = fetch_job_detail(row.get("detail_url") or row.get("job_url"))
    if not detail:
        return row

    enriched = dict(row)
    for key, value in detail.items():
        # The README already gave us a location; the detail page's version is
        # only a fallback.
        if key == "job_location" and enriched.get(key):
            continue
        # Otherwise the detail is authoritative for its own fields — take the
        # value even when empty, so a re-enrich clears a field that no longer
        # applies rather than leaving stale data behind. A whole failed fetch
        # returns above, so an empty here means the page really has no value.
        enriched[key] = value

    if enriched.get("comp_min") and not enriched.get("comp_max"):
        enriched["comp_max"] = enriched["comp_min"]

    return enriched


def enrich_jobs(rows: list[dict], *, max_workers: int | None = None) -> list[dict]:
    if not rows:
        return []

    workers = max_workers if max_workers is not None else DEFAULT_MAX_WORKERS
    workers = max(1, min(workers, 8))

    if workers == 1 or len(rows) == 1:
        return [enrich_job(row) for row in rows]

    # Index by position, not URL: two rows can share a detail_url and a
    # url-keyed dict would drop one of them.
    enriched = list(rows)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(enrich_job, row): index for index, row in enumerate(rows)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                enriched[index] = future.result()
            except Exception:
                logger.exception(
                    "Failed enriching %s", rows[index].get("detail_url")
                )

    return enriched
