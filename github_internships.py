from dotenv import load_dotenv
from github import Github, Auth
import os
from markdown_it import MarkdownIt
from io import StringIO
import pandas as pd
from bs4 import BeautifulSoup
import numpy as np
import emoji
import logging
import markdown
from database import SupabaseDatabase
from datetime import datetime
import time
from functools import lru_cache
from urllib.parse import urlparse
from ddgs import DDGS
from ddgs.exceptions import DDGSException
import re
import requests
from difflib import SequenceMatcher


class CompanySearch:
    BAD_DOMAINS = {
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
        "pitchbook.com",
        "bloomberg.com",
        "instagram.com",
        "tiktok.com",
        "github.com",
        "medium.com",
        "news.ycombinator.com",
        "freshershunt.in",
        "startupranking.com",
        # Job boards / ATS hosts — never the company's own site
        "greenhouse.io",
        "lever.co",
        "myworkdayjobs.com",
        "workday.com",
        "smartrecruiters.com",
        "ashbyhq.com",
        "jobvite.com",
        "bamboohr.com",
        "icims.com",
        "wellfound.com",
        "angel.co",
        "builtin.com",
        "builtinnyc.com",
        "levels.fyi",
        "simplify.jobs",
        "jobright.ai",
        "ripplematch.com",
        "untapped.io",
        "ziprecruiter.com",
        "monster.com",
        "dice.com",
        "handshake.com",
        "joinhandshake.com",
        # Company-data aggregators
        "zoominfo.com",
        "apollo.io",
        "rocketreach.co",
        "signalhire.com",
        "theorg.com",
        "owler.com",
        "craft.co",
        "cbinsights.com",
        "zippia.com",
        "pitchgrade.com",
    }

    # Legal/branding suffixes that appear in listings but rarely in domains
    COMPANY_SUFFIXES = (
        "incorporated",
        "technologies",
        "technology",
        "corporation",
        "holdings",
        "limited",
        "company",
        "group",
        "corp",
        "labs",
        "tech",
        "inc",
        "llc",
        "ltd",
        "co",
        "ai",
        "io",
    )

    # Best domain must clear this or we return None instead of junk
    MIN_WEBSITE_SCORE = 30

    # ATS hosts embed the company slug in the job URL — free, deterministic
    # signal for cross-checking search results
    ATS_HOST_SLUG_PATTERNS = (
        r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)",
        r"jobs\.lever\.co/([A-Za-z0-9_-]+)",
        r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)",
        r"([A-Za-z0-9-]+)\.wd\d+\.myworkdayjobs\.com",
        r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
        r"apply\.workable\.com/([A-Za-z0-9_-]+)",
        r"([A-Za-z0-9-]+)\.recruitee\.com",
        r"([A-Za-z0-9-]+)\.breezy\.hr",
    )

    # Wikidata entity descriptions must look like a business before we
    # trust their official-website claim (P856)
    BUSINESS_WORDS = (
        "company",
        "corporation",
        "business",
        "firm",
        "enterprise",
        "manufacturer",
        "conglomerate",
        "bank",
        "fund",
        "trading",
        "technology",
        "software",
        "developer",
        "retailer",
        "startup",
        "defense",
        "defence",
        "contractor",
        "exchange",
        "provider",
        "services",
    )

    WIKIDATA_API = "https://www.wikidata.org/w/api.php"
    HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (CSInternshipBot/1.0)"}

    def __init__(self):
        self.ddgs = DDGS()
        self.supabase_db = SupabaseDatabase()

        self.logger = logging.getLogger("github_internships")

    # Tried in order; one engine throttling us doesn't kill the search
    SEARCH_BACKENDS = ("duckduckgo", "bing", "brave")

    def _search(self, query, max_results=8, retries=2):
        for backend in self.SEARCH_BACKENDS:
            for attempt in range(retries):
                try:
                    results = list(
                        self.ddgs.text(
                            query,
                            max_results=max_results,
                            backend=backend,
                        )
                    )

                    if results:
                        return results

                    break  # empty result set: try next backend

                except DDGSException as e:
                    self.logger.warning("%s: %s", backend, e)

                    if attempt == retries - 1:
                        break  # exhausted retries: try next backend

                    time.sleep(1)

        return []

    def _clean(self, text):
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def _normalize_company(self, company):
        """Company name reduced to its distinctive core: lowercased,
        punctuation stripped, leading 'the' and trailing legal suffixes
        removed ('J.P. Morgan & Co.' -> 'jpmorgan')."""
        cleaned = self._clean(company)

        if cleaned.startswith("the") and len(cleaned) > 6:
            cleaned = cleaned[3:]

        for suffix in self.COMPANY_SUFFIXES:
            if cleaned.endswith(suffix) and len(cleaned) - len(suffix) >= 3:
                cleaned = cleaned[: -len(suffix)]
                break

        return cleaned

    @staticmethod
    def _domain_root(domain):
        """'careers.stripe.com' -> 'stripe', 'stripe.co.uk' -> 'stripe'."""
        parts = domain.split(".")
        if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac", "gov"):
            return parts[-3]
        if len(parts) >= 2:
            return parts[-2]
        return parts[0]

    def _score_result(self, company, url, title):
        score = 0

        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")

        # Ignore junk websites
        if any(bad in domain for bad in self.BAD_DOMAINS):
            return -100

        company_core = self._normalize_company(company)
        domain_clean = self._clean(domain)
        domain_root = self._clean(self._domain_root(domain))
        title_clean = self._clean(title or "")

        # Acronym: 'Susquehanna Investment Group' -> 'sig' == sig.com
        words = re.findall(r"[a-z]+", company.lower())
        initials = "".join(
            w[0] for w in words if w not in ("the", "of", "and")
        )

        # Exact domain match is the strongest possible signal
        if domain_root == company_core:
            score += 100
        elif len(initials) >= 3 and domain_root == initials:
            score += 70
        elif domain_root.startswith(company_core) or company_core.startswith(domain_root):
            score += 55
        elif company_core in domain_clean:
            score += 45
        else:
            # Fuzzy match catches abbreviations and mergers
            # ('jpmorgan' vs 'jpmorganchase')
            similarity = SequenceMatcher(None, company_core, domain_root).ratio()
            if similarity >= 0.8:
                score += 50
            elif similarity >= 0.65:
                score += 25

        # Title contains company name
        if company_core and company_core in title_clean:
            score += 20

        # Prefer shallow URLs: homepage beats deep blog/press links
        path_depth = len([seg for seg in parsed.path.split("/") if seg])
        score -= path_depth * 8

        # Official wording
        if "official" in (title or "").lower():
            score += 10

        # Careers/about pages are still on the company's own site
        if any(seg in parsed.path.lower() for seg in ("/careers", "/jobs", "/about")):
            score += 12

        # Penalize obvious junk
        bad_words = [
            "reddit",
            "glassdoor",
            "salary",
            "review",
            "wiki",
            "news",
            "linkedin",
        ]

        if any(word in (title or "").lower() for word in bad_words):
            score -= 50

        return score

    def _job_url_signals(self, job_urls):
        """Extract (direct company domains, ATS company slugs) from the
        internship posting URLs themselves."""
        domains = set()
        slugs = set()

        for url in job_urls or ():
            if not url:
                continue

            parsed = urlparse(url)
            netloc = parsed.netloc.lower().replace("www.", "")

            if not netloc:
                continue

            haystack = netloc + parsed.path.lower()

            matched_slug = None
            for pattern in self.ATS_HOST_SLUG_PATTERNS:
                match = re.search(pattern, haystack)
                if match:
                    matched_slug = match.group(1).lower()
                    break

            if matched_slug:
                slugs.add(matched_slug)
                continue

            # A non-ATS, non-junk posting URL points at the company's own site
            if any(bad in netloc for bad in self.BAD_DOMAINS):
                continue

            domains.add(netloc)

        return domains, slugs

    def _wikidata_website(self, company, retries=3):
        """Official website (P856) from Wikidata; high precision for
        established companies."""
        for attempt in range(retries):
            try:
                return self._wikidata_website_once(company)
            except Exception:
                if attempt == retries - 1:
                    self.logger.warning("Wikidata lookup failed for %s", company)
                    return None
                time.sleep(2)

    def _wikidata_website_once(self, company):
        response = requests.get(
            self.WIKIDATA_API,
            params={
                "action": "wbsearchentities",
                "search": company,
                "language": "en",
                "format": "json",
                "type": "item",
                "limit": 5,
            },
            headers=self.HTTP_HEADERS,
            timeout=8,
        )

        for hit in response.json().get("search", []):
            description = (hit.get("description") or "").lower()

            if not any(word in description for word in self.BUSINESS_WORDS):
                continue

            claims_response = requests.get(
                self.WIKIDATA_API,
                params={
                    "action": "wbgetclaims",
                    "entity": hit["id"],
                    "property": "P856",
                    "format": "json",
                },
                headers=self.HTTP_HEADERS,
                timeout=8,
            )

            claims = claims_response.json().get("claims", {}).get("P856", [])

            if claims:
                url = claims[0]["mainsnak"]["datavalue"]["value"]
                self.logger.info("Wikidata website for %s: %s", company, url)
                return url

        return None

    def _verify_website(self, url, company):
        """Fetch the candidate and confirm it mentions the company.
        Returns True (verified), False (positively wrong), or None
        (inconclusive — network error or anti-bot response)."""
        company_core = self._normalize_company(company)
        try:
            response = requests.get(
                url,
                headers=self.HTTP_HEADERS,
                timeout=6,
                allow_redirects=True,
            )
        except requests.RequestException:
            return None

        # Anti-bot walls say nothing about correctness
        if response.status_code in (401, 403, 429, 503):
            return None

        if response.status_code >= 400:
            return False

        final_domain = urlparse(response.url).netloc.lower().replace("www.", "")

        if any(bad in final_domain for bad in self.BAD_DOMAINS):
            return False

        if not company_core:
            return None

        head = response.text[:20000].lower()
        head_clean = self._clean(head)

        # Accept if any distinctive form of the name appears: full core,
        # acronym ('sig' for Susquehanna Investment Group), or the longest
        # word ('susquehanna') — DB names often differ slightly from the
        # company's own branding
        name_words = re.findall(r"[a-z0-9]+", company.lower()) or [company_core]
        longest_word = max(name_words, key=len)

        needles = {company_core, longest_word}

        if len(name_words) >= 3:
            needles.add("".join(w[0] for w in name_words))

        for needle in needles:
            if needle and len(needle) >= 3 and needle in head_clean:
                return True

        title_match = re.search(r"<title[^>]*>(.*?)</title>", head, re.S)

        if title_match:
            title_clean = self._clean(title_match.group(1))
            similarity = SequenceMatcher(
                None, company_core, title_clean[: len(company_core) + 5]
            ).ratio()
            if similarity >= 0.7:
                return True
            return False

        return None

    def _canonicalize(self, url, path_depth, company):
        """Deep link on a strongly-matching domain -> homepage."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        domain_root = self._clean(self._domain_root(domain))
        company_core = self._normalize_company(company)

        strong_match = (
            domain_root == company_core
            or SequenceMatcher(None, company_core, domain_root).ratio() >= 0.8
        )

        if path_depth > 1 and strong_match:
            scheme = parsed.scheme or "https"
            return f"{scheme}://{parsed.netloc}/"

        return url

    def _find_best_website(self, company, job_urls=()):

        queries = [
            f'"{company}" official website',
            f'"{company}"',
            f'"{company}" careers',
        ]

        # Aggregate per domain: a domain surfacing across multiple queries
        # is much more likely the real site than a one-off high scorer.
        domain_scores = {}
        domain_best = {}  # domain -> (score, path_depth, url)
        seen_urls = set()

        for query in queries:

            results = self._search(query)

            for result in results:

                url = result.get("href")

                if not url or url in seen_urls:
                    continue

                seen_urls.add(url)

                score = self._score_result(
                    company,
                    url,
                    result.get("title", ""),
                )

                if score <= -100:
                    continue

                parsed = urlparse(url)
                domain = parsed.netloc.lower().replace("www.", "")
                path_depth = len([seg for seg in parsed.path.split("/") if seg])

                domain_scores[domain] = domain_scores.get(domain, 0) + score + 5

                best = domain_best.get(domain)
                if best is None or (score, -path_depth) > (best[0], -best[1]):
                    domain_best[domain] = (score, path_depth, url)

        # --- Merge in high-precision signals -------------------------------

        # 1. Domains taken straight from the company's own job posting URLs
        direct_domains, ats_slugs = self._job_url_signals(job_urls)

        for domain in direct_domains:
            domain_scores[domain] = domain_scores.get(domain, 0) + 90
            domain_best.setdefault(domain, (90, 0, f"https://{domain}/"))

        # 2. Wikidata official website
        wikidata_url = self._wikidata_website(company)

        if wikidata_url:
            wd_domain = urlparse(wikidata_url).netloc.lower().replace("www.", "")
            domain_scores[wd_domain] = domain_scores.get(wd_domain, 0) + 90
            domain_best.setdefault(wd_domain, (90, 0, wikidata_url))

        # 3. ATS slugs corroborate matching search candidates
        for slug in ats_slugs:
            slug_clean = self._clean(slug)
            for domain in list(domain_scores):
                root = self._clean(self._domain_root(domain))
                if (
                    root == slug_clean
                    or root.startswith(slug_clean)
                    or slug_clean.startswith(root)
                ):
                    domain_scores[domain] += 40

        if not domain_scores:
            return None

        # --- Pick the best candidate that survives live verification ------

        company_core = self._normalize_company(company)

        ranked = sorted(
            domain_scores.items(), key=lambda item: item[1], reverse=True
        )

        for domain, score in ranked[:3]:

            if score < self.MIN_WEBSITE_SCORE:
                break

            _, path_depth, url = domain_best[domain]
            candidate = self._canonicalize(url, path_depth, company)

            verdict = self._verify_website(candidate, company)

            if verdict is False:
                self.logger.info(
                    "Rejected %s for %s: page does not match company",
                    candidate,
                    company,
                )
                continue

            return candidate

        self.logger.info("No confident website for %s", company)
        return None

    # Slug must resemble the company name this much or we return None —
    # a search-URL fallback beats linking the wrong company
    MIN_LINKEDIN_SCORE = 60

    def _find_linkedin(self, company, domain_root=None):

        results = self._search(
            f'site:linkedin.com/company "{company}"'
        )

        company_core = self._normalize_company(company)

        best_url = None
        best_slug = None
        best_score = 0

        for result in results:

            url = result.get("href")

            if not url or "linkedin.com/company/" not in url:
                continue

            slug = (
                url.split("linkedin.com/company/", 1)[1]
                .split("/")[0]
                .split("?")[0]
            )
            slug_clean = self._clean(slug)
            title_clean = self._clean(result.get("title") or "")

            if slug_clean == company_core:
                score = 100
            elif company_core and (
                slug_clean.startswith(company_core)
                or company_core.startswith(slug_clean)
            ):
                score = 75
            elif company_core and (
                company_core in slug_clean or slug_clean in company_core
            ):
                # Mid-string containment is weak: 'aquatic' appears inside
                # 'aquarena-aquatic-leisure-centre' without being that company
                score = 40
            else:
                score = int(
                    SequenceMatcher(None, company_core, slug_clean).ratio() * 100
                )

            if company_core and company_core in title_clean:
                score += 15

            # Slug agreeing with the verified website domain is strong
            # cross-source confirmation
            if domain_root and (
                slug_clean == domain_root
                or slug_clean.startswith(domain_root)
            ):
                score += 25

            # Tie-break on slug length: 'optiver' beats 'optiver-medellin',
            # 'd.-e.-shaw-&-co.' beats the India-subsidiary slug
            if score > best_score or (
                score == best_score
                and best_slug is not None
                and len(slug) < len(best_slug)
            ):
                best_score = score
                best_slug = slug
                best_url = f"https://www.linkedin.com/company/{slug}/"

        if best_score >= self.MIN_LINKEDIN_SCORE:
            return best_url

        self.logger.info(
            "No confident LinkedIn for %s (best score %d)", company, best_score
        )
        return None

    @staticmethod
    def _get_domain(url):

        if not url:
            return None

        return urlparse(url).netloc.replace("www.", "")

    @staticmethod
    def _get_logo(domain):

        if not domain:
            return None

        return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

    @lru_cache(maxsize=2048)
    def get_company_info(self, company, job_urls=()):
        if company in self.supabase_db.get_existing_company_names():
            return self.supabase_db.get_company_info(company)

        website = self._find_best_website(company, job_urls)

        domain = self._get_domain(website)
        domain_root = self._clean(self._domain_root(domain)) if domain else None

        return {
            "company_name": company,
            "company_website": website,
            "company_domain": domain,
            "company_linkedin": self._find_linkedin(company, domain_root),
            "company_logo": self._get_logo(domain),
        }


class GithubInternships:
    def __init__(self):
        load_dotenv()

        self.logger = logging.getLogger("logs/github_internships.log")
        self.logger.setLevel(logging.DEBUG)
        handler = logging.FileHandler("logs/github_internships.log", mode="a")
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        self.logger.addHandler(handler)

        self.auth = Auth.Token(os.getenv("GITHUB_TOKEN"))
        self.github = Github(auth=self.auth)

        self.markdown = MarkdownIt()
        self.supabase_db = SupabaseDatabase()
        self.company_search = CompanySearch()

        self.repos = [
            "vanshb03/Summer2027-Internships",
            "sndsh404/summer-2027-internships"
        ]

        self.repo_column_name = {
            "vanshb03/Summer2027-Internships": {
                "Company": "company_name",
                "Role": "job_title",
                "Location": "job_location",
                "Application/Link": "job_url",
                "Date Posted": "job_posted_at",
            },
            "sndsh404/summer-2027-internships": {
                "Company": "company_name",
                "Role": "job_title",
                "Location": "job_location",
                "Apply": "job_url",
                "Added": "job_posted_at",
            },
        }


        self.urls = np.array([])
        self.current_repo_name = None

        self.logger.info("Github internships scraper initialized")

    def get_internships(self, repo_name):
        try:
            self.logger.info("Generating internships from repo: %s", repo_name)

            repo = self.get_repo(repo_name)
            readme = self.get_readme(repo)

            self.logger.info("README retrieved successfully")

            html = self.convert_to_html(readme)
            soup = self.parse_html(html)
            df = self.generate_db(soup)
            urls = self.get_urls(soup, count=len(df))
            df = self.clean_df(df, urls)

            self.logger.info("Found %d application URLs", len(urls))
            self.logger.info("Extracted %d internship rows", len(df))

            companies = self.build_company_info(df)
            internships = df.to_dict(orient="records")

            return internships, companies

        except Exception:
            self.logger.exception("Failed to generate internships")
            raise

    # Small batches enrich inline so first Discord posts have full company
    # info; big batches defer to the hourly enrichment worker
    SYNC_ENRICH_THRESHOLD = 5

    def build_company_info(self, df):
        existing_names = set(self.supabase_db.get_existing_company_names())

        new_names = [
            company
            for company in df["company_name"].unique()
            if company not in existing_names
        ]

        self.logger.info(
            "Company lookup: %d unique, %d already known, %d to search",
            df["company_name"].nunique(),
            df["company_name"].nunique() - len(new_names),
            len(new_names),
        )

        if len(new_names) > self.SYNC_ENRICH_THRESHOLD:
            self.logger.info(
                "%d new companies exceeds sync threshold %d — "
                "inserting bare rows, deferring to enrichment worker",
                len(new_names),
                self.SYNC_ENRICH_THRESHOLD,
            )
            return [{"company_name": name} for name in new_names]

        company_info = {
            company: self.company_search.get_company_info(
                company,
                tuple(
                    df.loc[df["company_name"] == company, "job_url"]
                    .dropna()
                    .head(5)
                ),
            )
            for company in new_names
        }

        return list(company_info.values())

    ENRICH_BATCH_SIZE = 20
    ENRICH_SLEEP_SECONDS = 3

    def enrich_companies(self, limit=None):
        """Background enrichment: fill in metadata for companies whose
        website is still NULL, paced to stay under search-engine rate
        limits. Failed companies retry up to the DB-side attempt cap."""
        limit = limit or self.ENRICH_BATCH_SIZE

        rows = self.supabase_db.get_companies_needing_enrichment(limit=limit)

        if not rows:
            self.logger.info("No companies need enrichment")
            return 0

        # Retries must not be served a cached failure from a prior batch
        self.company_search.get_company_info.cache_clear()

        enriched = 0

        for row in rows:
            name = row["company_name"]
            attempts = (row.get("enrich_attempts") or 0) + 1

            job_urls = tuple(self.supabase_db.get_company_job_urls(name))
            info = self.company_search.get_company_info(name, job_urls)

            self.supabase_db.update_company_info(name, info, attempts)

            if info.get("company_website"):
                enriched += 1

            time.sleep(self.ENRICH_SLEEP_SECONDS)

        self.logger.info(
            "Enrichment batch done: %d/%d websites found", enriched, len(rows)
        )

        return enriched

    def generate_db(self, soup):
        try:
            df = pd.read_html(StringIO(str(soup)))[0]
            df = df.rename(self.repo_column_name[self.current_repo_name], axis=1)
            df["job_posted_at"] = df["job_posted_at"].replace("-", pd.NA)
            df.dropna(inplace=True)

            df["job_posted_at"] = df["job_posted_at"].apply(self.normalize_date)
            cutoff = datetime(2026, 6, 1)
            self.logger.info("Filtering internships posted after %s", cutoff)
            df = df[df["job_posted_at"] >= cutoff]
            df["job_posted_at"] = df["job_posted_at"].dt.strftime("%Y-%m-%d")

            return df

        except Exception:
            self.logger.exception("Failed to convert HTML table to dataframe")
            raise

    def get_markdown(self, readme):
        return self.markdown.render(readme)

    def get_repo(self, repo_name):
        self.logger.info("Fetching repository: %s", repo_name)

        return self.github.get_repo(repo_name)

    def get_readme(self, repo):
        self.logger.info("Fetching README")

        return repo.get_readme().decoded_content.decode()

    def get_issues(self, repo):
        self.logger.info("Fetching issues")

        return repo.get_issues(state="open")

    def convert_to_html(self, readme_text):
        self.logger.info("Converting markdown to HTML")

        html = markdown.markdown(readme_text, extensions=["tables"])

        html = html.replace("</br>", " | ")
        html = html.replace("<br>", " | ")
        html = html.replace("<br/>", " | ")
        html = html.replace("<br />", " | ")

        return html

    def parse_html(self, html):
        self.logger.info("Parsing HTML")

        soup = BeautifulSoup(html, "html.parser")

        for summary in soup.find_all("summary"):
            summary.decompose()

        for details in soup.find_all("details"):
            details.unwrap()

        return soup

    def parse_issue(self, issues):
        internships = []
        for issue in issues:
            if issue.title != "New Internship" or not issue.body:
                continue

            posted_date = issue.created_at.strftime("%Y-%m-%d")

            rendered = self.markdown.render(issue.body)
            soup = BeautifulSoup(rendered, "html.parser")

            fields = {}
            for heading in soup.find_all("h3"):
                value = heading.find_next_sibling("p")
                if value:
                    fields[heading.get_text(strip=True)] = value.get_text(strip=True)

            company_name = fields.get("Company Name")
            job_title = fields.get("Internship Title")
            job_url = fields.get("Link to Internship Posting")

            if not all((company_name, job_title, job_url)):
                self.logger.warning(
                    "Skipping malformed issue #%s: missing required fields",
                    issue.number,
                )
                continue

            internship = {
                "company_name": company_name,
                "job_title": job_title,
                "job_url": job_url,
                "job_location": fields.get("Location"),
                "job_type": self.classify_role(job_title),
                "job_posted_at": posted_date,
                "source_repo": "https://github.com/vanshb03/Summer2027-Internships",
            }
            internships.append(internship)
        return internships

    def get_urls(self, soup, count):
        return np.array([link.get("href") for link in soup.select("td a[href]")])[
            :count
        ]

    def clean_df(self, df, urls):
        self.logger.info("Cleaning dataframe")

        df = df.replace("↳", np.nan).ffill()

        before = len(df)

        df = df[df["job_url"] != "🔒"]

        self.logger.info("Removed %d locked applications", before - len(df))

        df["job_url"] = urls

        df["source_repo"] = "https://github.com/vanshb03/Summer2027-Internships"

        df["job_title"] = (
            df["job_title"]
            .apply(lambda x: emoji.replace_emoji(str(x), replace=""))
            .str.strip()
        )

        df["job_type"] = df["job_title"].apply(self.classify_role)
        return df

    def classify_role(self, title):
        title = title.lower()

        if any(
            k in title
            for k in [
                "machine learning",
                "ml",
                "artificial intelligence",
                "ai",
                "llm",
                "deep learning",
            ]
        ):
            return "AI / ML"

        if any(
            k in title
            for k in [
                "data science",
                "data scientist",
                "data analyst",
                "analytics",
                "data",
            ]
        ):
            return "Data Science"

        if any(
            k in title
            for k in ["quant", "trading", "researcher", "research", "quantitative"]
        ):
            return "Quant"

        if "product" in title:
            return "Product"

        if any(
            k in title
            for k in [
                "frontend",
                "front-end",
                "backend",
                "back-end",
                "full stack",
                "full-stack",
                "software engineer",
                "software development",
                "swe",
                "developer",
                "engineer",
                "engineering",
            ]
        ):
            return "Software Engineering"

        return "Other"

    def normalize_date(self, date_str):
        date_str = date_str.strip()
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            pass

        current_year = datetime.now().year
        return datetime.strptime(f"{date_str} {current_year}", "%b %d %Y")

    def insert_issues_internships(self):
        try:
            repo = self.get_repo("vanshb03/Summer2027-Internships")
            issues = self.get_issues(repo)
            internships = self.parse_issue(issues)

            self.logger.info(
                "Parsed %d internships from issues", len(internships)
            )

            if not internships:
                return []

            df = pd.DataFrame(internships)

            cutoff = datetime(2026, 6, 1)
            df = df[pd.to_datetime(df["job_posted_at"]) >= cutoff]

            if df.empty:
                self.logger.info("No issue internships after cutoff")
                return []

            internships = df.to_dict(orient="records")

            companies = self.build_company_info(df)
            self.supabase_db.insert_companies(companies)
            return self.supabase_db.insert_internships(internships)

        except Exception:
            self.logger.exception("Failed inserting issue internships")
            return None

    def insert_internships(self):
        for repo_name in self.repos:
            self.current_repo_name = repo_name
            full_repo_name = f"https://github.com/{repo_name}"
            current_commit_time = self.get_repo(repo_name).pushed_at.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            last_commit_time = self.supabase_db.get_repo_update_time(full_repo_name)

            if last_commit_time is None or current_commit_time > last_commit_time:
                self.logger.info("New commits found in repo %s", repo_name)
                internships, companies = self.get_internships(repo_name)

                self.supabase_db.insert_companies(companies)
                self.supabase_db.insert_repo_update_time(
                    full_repo_name, current_commit_time
                )
                self.supabase_db.insert_internships(internships)
            else:
                self.logger.info("No new commits found in repo %s", repo_name)

        # Issues flow is deliberately outside the commit-time gate
        self.insert_issues_internships()


if __name__ == "__main__":
    github_internships = GithubInternships()
    github_internships.current_repo_name = "sndsh404/summer-2027-internships"
    repo = github_internships.get_repo("sndsh404/summer-2027-internships")
    #repo = github_internships.github.get_repo("vanshb03/Summer2027-Internships")
    readme = github_internships.get_readme(repo)
    html = github_internships.convert_to_html(readme)
    soup = github_internships.parse_html(html)
    df = github_internships.generate_db(soup)
    print(df)