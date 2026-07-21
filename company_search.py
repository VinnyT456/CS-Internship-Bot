from ddgs import DDGS
from ddgs.exceptions import DDGSException
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse


class CompanySearch:
    # Never a company's own domain — skip for logo resolution
    BAD_DOMAINS = (
        "wikipedia.org",
        "reddit.com",
        "facebook.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "glassdoor.com",
        "indeed.com",
        "crunchbase.com",
        "bloomberg.com",
        "instagram.com",
        "github.com",
        "medium.com",
        "greenhouse.io",
        "lever.co",
        "myworkdayjobs.com",
        "workday.com",
        "ashbyhq.com",
        "simplify.jobs",
        "ziprecruiter.com",
        "levels.fyi",
        "google.com",
    )

    def __init__(self):
        self.logger = logging.getLogger("github_internships")

    def _search(self, query, max_results=5):
        # A fresh DDGS per call keeps this thread-safe for concurrent lookups
        try:
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        except DDGSException as e:
            self.logger.warning("Search failed for %r: %s", query, e)
            return []

    @staticmethod
    def _careers_url(company):
        return f"https://www.google.com/search?q={quote(company + ' careers')}"

    @staticmethod
    def _linkedin_search_url(company):
        # Fallback when we can't resolve a real company page — a LinkedIn
        # company search the user can click through, same idea as the
        # Google-careers website link.
        return (
            "https://www.linkedin.com/search/results/companies/?keywords="
            f"{quote(company)}"
        )

    @staticmethod
    def _expected_slug(company):
        # LinkedIn slugs are usually the name lowercased, ()-content dropped,
        # non-alphanumerics collapsed to hyphens. 'The Trade Desk' ->
        # 'the-trade-desk'. Used as a prior to rank real search results.
        name = re.sub(r"\(.*?\)", "", company)
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    def _find_linkedin(self, company):
        expected = self._expected_slug(company)
        if not expected:
            return None

        candidates = []
        for result in self._search(f'site:linkedin.com/company "{company}"'):
            url = result.get("href") or ""
            if "linkedin.com/company/" not in url:
                continue
            slug = url.split("linkedin.com/company/", 1)[1].split("/")[0]
            slug = slug.split("?")[0].lower()
            if slug:
                candidates.append(slug)

        if not candidates:
            # No real page found — hand back a LinkedIn company search link
            # rather than a guessed slug that would 404
            return self._linkedin_search_url(company)

        # Pick the candidate slug closest to what the name predicts. Exact
        # match wins; otherwise the most similar (handles 'jane-street' vs
        # 'jane-street-global', country suffixes, etc.)
        if expected in candidates:
            best = expected
        else:
            best = max(
                candidates,
                key=lambda s: SequenceMatcher(None, expected, s).ratio(),
            )

        return f"https://www.linkedin.com/company/{best}/"

    @staticmethod
    def _root_domain(domain):
        """'docs.databricks.com' -> 'databricks.com', keeping the favicon
        keyed off the company's real domain."""
        parts = domain.split(".")
        if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac", "gov"):
            return ".".join(parts[-3:])
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return domain

    def _find_domain(self, company):
        for result in self._search(f'{company} official website'):
            url = result.get("href") or ""
            domain = urlparse(url).netloc.lower().replace("www.", "")
            if domain and not any(bad in domain for bad in self.BAD_DOMAINS):
                return self._root_domain(domain)
        return None

    @staticmethod
    def _logo(domain):
        if not domain:
            return None
        return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

    def get_company_info(self, company):
        # Run the two independent searches concurrently — halves latency
        with ThreadPoolExecutor(max_workers=2) as pool:
            domain_future = pool.submit(self._find_domain, company)
            linkedin_future = pool.submit(self._find_linkedin, company)
            domain = domain_future.result()
            linkedin = linkedin_future.result()

        return {
            "company_name": company,
            "company_website": self._careers_url(company),
            "company_domain": domain,
            "company_linkedin": linkedin,
            "company_logo": self._logo(domain),
        }


if __name__ == "__main__":
    import sys

    company = " ".join(sys.argv[1:]) or "Stripe"
    cs = CompanySearch()
    info = cs.get_company_info(company)

    for key, value in info.items():
        print(f"{key:18}: {value}")
