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
        "github.io",
        "medium.com",
        "greenhouse.io",
        "lever.co",
        "myworkdayjobs.com",
        "workday.com",
        "ashbyhq.com",
        "simplify.jobs",
        "ziprecruiter.com",
        "levels.fyi",
        # NOTE: google.com deliberately NOT blocked — it's a real company
        # domain. Google *search-result* links are filtered by path elsewhere.
    )

    def __init__(self):
        self.logger = logging.getLogger("github_internships")

    # Tried in order; one engine throttling/emptying doesn't kill the search
    SEARCH_BACKENDS = ("duckduckgo", "bing", "brave")

    def _search(self, query, max_results=5):
        # A fresh DDGS per call keeps this thread-safe for concurrent lookups.
        # Fall through backends so a throttled/empty engine doesn't return NULL.
        for backend in self.SEARCH_BACKENDS:
            try:
                with DDGS() as ddgs:
                    results = list(
                        ddgs.text(query, max_results=max_results, backend=backend)
                    )
                if results:
                    return results
            except DDGSException as e:
                self.logger.warning("%s search failed for %r: %s", backend, query, e)

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

    def _is_bad_domain(self, domain):
        # Match on the root domain, not a substring — 'netflix.com' must not
        # be rejected because it ends in 'x.com'
        root = self._root_domain(domain)
        return root in self.BAD_DOMAINS

    def _find_domain(self, company):
        core = re.sub(r"[^a-z0-9]", "", company.lower())

        candidates = []
        for result in self._search(f'{company} official website'):
            url = result.get("href") or ""
            parsed = urlparse(url)
            domain = parsed.netloc.lower().replace("www.", "")

            if not domain or self._is_bad_domain(domain):
                continue

            # A google.com/search... link is a search result, not the
            # company's site — only accept google.com as a bare homepage
            if "google.com" in domain and parsed.path.rstrip("/") not in ("", "/"):
                continue

            root = self._root_domain(domain)
            if root not in candidates:
                candidates.append(root)

        if not candidates or not core:
            return None

        # Rank by how well the domain's name matches the company's. Substring
        # overlap ('jpmorganchase' ⊇ 'chase') scores highest; otherwise fall
        # back to fuzzy ratio. This rejects junk like 'linktr.ee' or
        # 'computerhope.com' that rank high but share no name with the company.
        def score(cand):
            root_name = cand.split(".")[0]
            if root_name in core or core in root_name:
                return 1.0
            return SequenceMatcher(None, core, root_name).ratio()

        best = max(candidates, key=score)

        # Below this, the "match" is coincidental — better no logo than a
        # wrong one (the website field still links to a Google careers search)
        if score(best) < 0.5:
            self.logger.info(
                "No confident domain for %s (best: %s)", company, best
            )
            return None

        return best

    @staticmethod
    def _logo(domain):
        if not domain:
            return None
        return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

    def get_company_info(self, company, known_domain=None):
        # If the source already gives the company's site (e.g. jobright's
        # README links it), skip the domain search entirely.
        if known_domain:
            domain = self._root_domain(known_domain.lower().replace("www.", ""))
            linkedin = self._find_linkedin(company)
        else:
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
