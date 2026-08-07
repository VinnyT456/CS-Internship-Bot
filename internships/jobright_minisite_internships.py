"""Scraper for jobright.ai's minisite JSON feed (SWE internships, US).

The GitHub-README scraper (jobright_internships.py) only sees the curated repo
table. The public minisite embed at

    https://jobright.ai/minisites-jobs/intern/us/swe?embed=true

is a Next.js page whose server payload (`__NEXT_DATA__.props.pageProps.initialJobs`)
carries structured job rows directly, and a `_next/data/<buildId>/…json` route
serves the same JSON for paging. This scraper pulls that feed and maps ONLY the
fields the DB needs — company, title, apply/detail URL, location, posted date,
work model, role type. Everything richer (summary, requirements, salary, tags) is
left for details/jobright_detail.py, which already reads the same jobright.ai/jobs/
info/<id> posting page, so nothing extra is written to the DB here.

Reuses JobrightInternships for classify_role / normalize_date / the commit_scrape
plumbing; only the data SOURCE differs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
from datetime import datetime, timezone

import httpx

from internships.jobright_internships import JobrightInternships


class JobrightMinisiteInternships(JobrightInternships):
    """Pull SWE intern rows from jobright.ai's minisite JSON feed."""

    # Stable identity for repo_info / source_repo (not a GitHub repo, but the
    # commit-gate table keys on this string).
    SOURCE = "https://jobright.ai/minisites-jobs/intern/us/swe"
    TABLE = "internships"
    # Page path used to read the embed HTML (whose server payload carries the
    # freshest ~50 jobs). The site's "load more" uses an auth-gated internal API,
    # so we take the initial 50 freshest each run — with a 15-min scrape loop that
    # comfortably keeps up with new postings, and the DB dedups repeats.
    _PAGE_PATH = "minisites-jobs/intern/us/swe"
    _BASE = "https://jobright.ai"
    _TIMEOUT = 25.0
    _UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/537.36"

    # --- feed fetching ----------------------------------------------------
    def _client(self):
        return httpx.Client(
            timeout=self._TIMEOUT, headers={"User-Agent": self._UA},
            follow_redirects=True,
        )

    def _fetch_jobs(self, client):
        """Read the freshest jobs from the embed's server payload
        (__NEXT_DATA__.props.pageProps.initialJobs). Returns a list of raw job
        dicts. Raises on a hard fetch/parse failure (caller isolates it)."""
        r = client.get(f"{self._BASE}/{self._PAGE_PATH}", params={"embed": "true"})
        r.raise_for_status()
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            r.text, re.S,
        )
        if not m:
            raise RuntimeError("minisite: __NEXT_DATA__ not found")
        data = json.loads(m.group(1), strict=False)
        return (data.get("props", {}).get("pageProps", {}) or {}).get("initialJobs") or []

    # --- mapping ----------------------------------------------------------
    def _row_from_job(self, job):
        """Map ONE raw minisite job to a DB internships row — necessary fields
        only. Returns None if it lacks the essentials (company/title/url)."""
        company = (job.get("company") or "").strip()
        title = (job.get("title") or "").strip()
        job_id = (job.get("id") or "").strip()
        if not company or not title or not job_id:
            return None

        # jobright's posting page is BOTH the apply link and the detail page the
        # enricher reads — store it in both, like the README scraper does.
        url = (job.get("applyUrl") or "").strip() or f"{self._BASE}/jobs/info/{job_id}"

        row = {
            "company_name": company,
            "job_title": title,
            "job_url": url,
            "detail_url": url,
            "job_location": (job.get("location") or "").strip() or None,
            "job_type": self.classify_role(title),
            "job_posted_at": self._posted_date(job.get("postedDate")),
            "source_repo": self.SOURCE,
        }
        # work_model is a plain existing column and comes free in the feed — no
        # extra scrape, so it's cheap to fill.
        wm = (job.get("workModel") or "").strip()
        if wm:
            row["work_model"] = wm
        return row

    @staticmethod
    def _posted_date(epoch_ms):
        """Epoch milliseconds → YYYY-MM-DD (the job_posted_at column is a DATE),
        or None if unparseable."""
        try:
            dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc)
            return dt.date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    # --- scraper entrypoints (override the README-based ones) -------------
    def get_internships(self):
        """Return (internships, companies) from the minisite feed."""
        rows = []
        seen = set()  # de-dup within this run on the posting URL
        with self._client() as client:
            jobs = self._fetch_jobs(client)

        for job in jobs:
            row = self._row_from_job(job)
            if not row:
                continue
            key = row["job_url"]
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

        self.logger.info("minisite: mapped %d internship rows", len(rows))
        # company_info only needs the name to exist for the FK; the detail
        # enricher + company search fill richer company data elsewhere.
        existing = set(self.supabase_db.get_existing_company_names())
        companies = [
            {"company_name": n}
            for n in {r["company_name"] for r in rows}
            if n not in existing
        ]
        return rows, companies

    def insert_internships(self):
        """Fetch the feed and commit. Gated like the README scraper: only advance
        the stored timestamp when the commit succeeds, so a failure retries next
        cycle. Uses 'now' as the pushed_at marker (the feed has no commit time)."""
        try:
            internships, companies = self.get_internships()
        except Exception:
            self.logger.exception("minisite: fetch/map failed")
            return
        if not internships:
            self.logger.info("minisite: no rows this run")
            return
        now = datetime.now(timezone.utc)
        # internships FK-reference repo_info(source_repo); commit_scrape only
        # writes that row AFTER the internship insert, so pre-register the source
        # here (idempotent upsert) to satisfy the FK on the very first run.
        self.supabase_db.insert_repo_update_time(self.SOURCE, now.isoformat())
        self.supabase_db.commit_scrape(
            self.SOURCE, now, internships, companies, self.TABLE
        )


if __name__ == "__main__":
    s = JobrightMinisiteInternships()
    rows, companies = s.get_internships()
    print(f"mapped {len(rows)} rows, {len(companies)} new companies")
    for r in rows[:5]:
        print(f"  {r['company_name'][:22]:22} | {r['job_title'][:34]:34} | "
              f"{r['job_posted_at']} | {r.get('work_model','')}")
        print(f"     {r['job_url'][:72]}")
