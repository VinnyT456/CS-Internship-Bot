"""Job-detail enrichment.

Both aggregators publish a structured payload on their own posting page, so
after a scrape we fetch `detail_url` and fill in summary/comp/requirements/
work-model that the README tables don't carry.

Rows are routed by the host of `detail_url`. Anything else (a raw ATS link,
or a row with no detail_url) passes through untouched.
"""

import logging
from urllib.parse import urlparse

from details import jobright_detail, simplify_detail

logger = logging.getLogger("details")

# Fields an enricher may add. main.py uses this to strip enrichment before
# insert if the columns aren't migrated yet.
DETAIL_FIELDS = (
    "job_summary",
    "job_responsibilities",
    "job_requirements",
    "job_benefits",
    "comp_min",
    "comp_max",
    "salary_desc",
    "employment_type",
    "seniority",
    "job_tags",
    "work_model",
    "is_closed",
)

_MODULES = (jobright_detail, simplify_detail)


def _host(row: dict) -> str:
    url = row.get("detail_url") or row.get("job_url") or ""
    return urlparse(url).netloc.lower()


def _module_for(row: dict):
    host = _host(row)
    if host == "jobright.ai" or host.endswith(".jobright.ai"):
        return jobright_detail
    if host == "simplify.jobs" or host.endswith(".simplify.jobs"):
        return simplify_detail
    return None


def enrich_jobs(rows: list[dict], *, max_workers: int | None = None) -> list[dict]:
    """Enrich in place-by-position. Rows with no supported detail host are
    returned unchanged, so a mixed batch is safe to pass in whole."""
    if not rows:
        return []

    buckets: dict[object, list[int]] = {}
    for index, row in enumerate(rows):
        module = _module_for(row)
        if module is not None:
            buckets.setdefault(module, []).append(index)

    enriched = list(rows)
    for module, indexes in buckets.items():
        subset = [rows[i] for i in indexes]
        try:
            results = module.enrich_jobs(subset, max_workers=max_workers)
        except Exception:
            logger.exception("Enrichment failed for %s", module.__name__)
            continue
        for index, result in zip(indexes, results):
            enriched[index] = result

    skipped = len(rows) - sum(len(v) for v in buckets.values())
    if skipped:
        logger.info("Skipped %d row(s) with no supported detail host", skipped)

    return enriched


def check_job_closed(url: str) -> bool | None:
    """True/False if a module recognizes the host, None otherwise."""
    module = _module_for({"detail_url": url})
    if module is None:
        return None
    return module.check_job_closed(url)


def close_http_clients() -> None:
    """Drop pooled sockets after an enrich pass (Render memory hygiene)."""
    for module in _MODULES:
        try:
            module.close_http_client()
        except Exception:
            logger.exception("Failed closing client for %s", module.__name__)
