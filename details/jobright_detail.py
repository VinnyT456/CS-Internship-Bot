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

logger = logging.getLogger("jobright_detail")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
DEFAULT_MAX_WORKERS = int(os.getenv("JOBRIGHT_ENRICH_WORKERS", "4"))
HELPER_SCRIPT_RE = re.compile(
    r'<script id="jobright-helper-job-detail-info" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)
LD_JSON_SCRIPT_RE = re.compile(
    r'<script id="job-posting" type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL,
)
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)

_client_lock = threading.Lock()
_http_client: httpx.Client | None = None


def normalize_job_url(job_url: str | None) -> str | None:
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
    """Release the pooled connections. main.py calls this after each enrich
    pass so idle sockets don't accumulate on Render's 512 MB instance."""
    global _http_client
    with _client_lock:
        if _http_client is not None:
            _http_client.close()
            _http_client = None


def _annual_to_thousands(value: int | float | None) -> int | None:
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return max(1, round(amount / 1000))


def _normalize_work_model(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in ("on site", "onsite", "on-site", "in person", "in-person"):
        return "On-site"
    if normalized == "hybrid":
        return "Hybrid"
    if normalized == "remote":
        return "Remote"
    return value.strip()


def _unique_strings(values: list[str] | None) -> list[str]:
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


def _pick_string(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _from_helper_result(result: dict) -> dict:
    min_k = _annual_to_thousands(result.get("minSalary"))
    max_k = _annual_to_thousands(result.get("maxSalary"))

    # skillSummaries is the actual required-skills/qualifications list
    # ("C#", "Unity", "narrative development") — that's what belongs in Skills.
    # Requirements keeps the harder gate items (education + qualifications).
    # jobTags/recommendationTags are marketing noise ("Be an early applicant")
    # and are dropped.
    requirements = _unique_strings(result.get("qualificationSummaries"))
    requirements.extend(_unique_strings(result.get("educationSummaries")))

    skills = _unique_strings(result.get("skillSummaries"))

    return {
        "job_summary": _pick_string(result.get("jobSummary"), result.get("description")),
        "job_responsibilities": _unique_strings(result.get("coreResponsibilities")),
        "job_requirements": _unique_strings(requirements),
        "job_benefits": _unique_strings(result.get("benefitsSummaries")),
        "comp_min": min_k,
        "comp_max": max_k or min_k,
        "salary_desc": _pick_string(result.get("salaryDesc")),
        "employment_type": _pick_string(result.get("employmentType")),
        "seniority": _pick_string(result.get("jobSeniority")),
        "job_tags": skills,
        "work_model": _normalize_work_model(result.get("workModel")),
        "job_location": _pick_string(result.get("jobLocation")),
    }


def _from_ld_json(payload: dict) -> dict:
    description = payload.get("description") or ""

    summary = None
    summary_match = re.search(r"^<p>(.*?)</p>", description, re.DOTALL)
    if summary_match:
        summary = BeautifulSoup(summary_match.group(1), "html.parser").get_text(
            " ", strip=True
        )
    if not summary:
        plain = BeautifulSoup(description, "html.parser").get_text(" ", strip=True)
        summary = plain[:500] if plain else None

    salary = payload.get("baseSalary") or {}
    value = salary.get("value") or {}
    unit = (value.get("unitText") or "YEAR").upper()
    min_val = value.get("minValue")
    max_val = value.get("maxValue") or min_val

    comp_min = comp_max = None
    salary_desc = None
    if min_val is not None:
        if unit == "HOUR":
            salary_desc = f"${min_val}/hr" + (
                f" - ${max_val}/hr" if max_val != min_val else ""
            )
            comp_min = _annual_to_thousands(float(min_val) * 2080)
            comp_max = _annual_to_thousands(float(max_val) * 2080)
        else:
            comp_min = _annual_to_thousands(min_val)
            comp_max = _annual_to_thousands(max_val)

    employment = payload.get("employmentType")
    if isinstance(employment, list):
        employment = employment[0] if employment else None

    location = _pick_string(payload.get("jobLocation"))
    job_location = payload.get("jobLocation") or {}
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else {}
    if isinstance(job_location, dict):
        address = job_location.get("address") or {}
        locality = address.get("addressLocality")
        region = address.get("addressRegion")
        if locality and region:
            location = f"{locality}, {region}"
        elif locality:
            location = locality

    # The helper block is authoritative for the structured lists
    # (responsibilities/requirements/skills). ld+json's HTML section-scrape is
    # lossy — it swept skill text and the "Company Overview" blurb into
    # requirements — so we DON'T surface its lists here. ld+json only fills the
    # scalars the helper can miss: summary, comp, location, employment type.
    return {
        "job_summary": summary,
        "comp_min": comp_min,
        "comp_max": comp_max,
        "salary_desc": salary_desc,
        "employment_type": str(employment).replace("_", " ").title()
        if employment
        else None,
        "job_location": location,
    }


def _from_next_data(payload: dict) -> dict:
    page_props = payload.get("props", {}).get("pageProps", {})
    result = page_props.get("jobResult") or page_props.get("job") or {}
    if isinstance(result, dict) and result:
        return _from_helper_result(result)
    return {}


def _merge_details(*details: dict | None) -> dict:
    # First non-empty source wins per field — for both scalars AND lists.
    # Sources are passed helper-first; the helper's lists are clean structured
    # data, while ld+json's section-scraper is lossy (it swept the "Company
    # Overview" blurb into requirements). Concatenating them polluted the clean
    # lists, so ld+json/next now only FILL fields the helper left empty rather
    # than appending to them.
    merged: dict = {}

    for detail in details:
        if not detail:
            continue
        for key, value in detail.items():
            if value in (None, "", []):
                continue
            if key not in merged or merged[key] in (None, "", []):
                merged[key] = value

    if merged.get("comp_min") and not merged.get("comp_max"):
        merged["comp_max"] = merged["comp_min"]

    return merged


def _is_jobright_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "jobright.ai" or host.endswith(".jobright.ai")


def _fetch_html(job_url: str, *, attempts: int = 3) -> str | None:
    client = _get_http_client()
    last_exc: Exception | None = None

    for attempt in range(attempts):
        try:
            response = client.get(job_url, follow_redirects=False)

            hops = 0
            while response.status_code in {301, 302, 303, 307, 308} and hops < 3:
                location = response.headers.get("location")
                if not location:
                    break
                if not _is_jobright_host(location):
                    logger.info(
                        "Skipping off-site redirect for %s (listing host blocks bots)",
                        job_url,
                    )
                    return None
                response = client.get(location, follow_redirects=False)
                hops += 1

            if response.status_code in {403, 404, 410}:
                return None

            response.raise_for_status()

            if not _is_jobright_host(str(response.url)):
                logger.info("Skipping non-jobright response for %s", job_url)
                return None

            return response.text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 404, 410}:
                return None
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(1.0 * (attempt + 1))
                continue

    logger.warning(
        "Failed fetching job detail %s after %d attempts: %s",
        job_url,
        attempts,
        last_exc,
    )
    return None


# jobright renders a literal "This job has closed." div (server-side) when a
# posting is no longer accepting applicants. Open pages never contain it and DO
# carry the job-posting ld+json block; closed pages drop that block. The text is
# the primary signal; the missing ld+json is corroborating.
_CLOSED_TEXT_RE = re.compile(r"this job has closed", re.IGNORECASE)


def is_html_closed(html: str) -> bool:
    return bool(html) and bool(_CLOSED_TEXT_RE.search(html))


def check_job_closed(job_url: str) -> bool | None:
    """Return True if closed, False if open, None if it couldn't be determined.

    None means the page couldn't be fetched (network error, off-site redirect,
    403/404) — callers should leave the job's status unchanged rather than
    assume closed.
    """
    normalized = normalize_job_url(job_url)
    if not normalized or "jobright.ai" not in normalized:
        return None
    html = _fetch_html(normalized)
    if html is None:
        return None
    return is_html_closed(html)


def _parse_html(html: str) -> dict:
    helper_detail: dict = {}
    ld_detail: dict = {}
    next_detail: dict = {}

    helper_match = HELPER_SCRIPT_RE.search(html)
    if helper_match:
        try:
            payload = json.loads(helper_match.group(1))
            result = payload.get("jobResult") or payload
            helper_detail = _from_helper_result(result)
        except json.JSONDecodeError:
            logger.warning("Invalid helper JSON in job detail page")

    ld_match = LD_JSON_SCRIPT_RE.search(html)
    if ld_match:
        try:
            ld_detail = _from_ld_json(json.loads(ld_match.group(1)))
        except json.JSONDecodeError:
            logger.warning("Invalid ld+json in job detail page")

    next_match = NEXT_DATA_RE.search(html)
    if next_match:
        try:
            next_detail = _from_next_data(json.loads(next_match.group(1)))
        except json.JSONDecodeError:
            logger.warning("Invalid __NEXT_DATA__ in job detail page")

    return _merge_details(helper_detail, ld_detail, next_detail)


def fetch_job_detail(job_url: str) -> dict | None:
    normalized = normalize_job_url(job_url)
    if not normalized or "jobright.ai" not in normalized:
        return None

    html = _fetch_html(normalized)
    if not html:
        return None

    detail = _parse_html(html)
    if (
        detail.get("job_summary")
        or detail.get("job_responsibilities")
        or detail.get("comp_min")
    ):
        # Free: we already have the page, so no second request to decide this.
        detail["is_closed"] = is_html_closed(html)
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
        if value in (None, [], ""):
            continue
        if key in {"job_location", "work_model"} and enriched.get(key):
            continue
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
        enriched = [enrich_job(row) for row in rows]
    else:
        # Index by position, not URL: two rows can share a detail_url and a
        # url-keyed dict would silently drop one of them.
        enriched = list(rows)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(enrich_job, row): index
                for index, row in enumerate(rows)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    enriched[index] = future.result()
                except Exception:
                    logger.exception(
                        "Failed enriching %s", rows[index].get("detail_url")
                    )

    retry_rows = [
        index for index, row in enumerate(enriched) if not _is_enriched(row)
    ]
    if retry_rows and workers > 1:
        logger.info("Retrying %d unenriched jobs sequentially", len(retry_rows))
        for index in retry_rows:
            enriched[index] = enrich_job(enriched[index])

    return enriched
