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

    def __init__(self):
        self.ddgs = DDGS()

        self.logger = logging.getLogger("github_internships")

    def _search(self, query, max_results=8, retries=3):
        for attempt in range(retries):
            try:
                return list(
                    self.ddgs.text(
                        query,
                        max_results=max_results,
                    )
                )

            except DDGSException as e:
                self.logger.warning(e)

                if attempt == retries - 1:
                    return []

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

        # Exact domain match is the strongest possible signal
        if domain_root == company_core:
            score += 100
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

    def _find_best_website(self, company):

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

        if not domain_scores:
            return None

        best_domain = max(domain_scores, key=domain_scores.get)

        if domain_scores[best_domain] < self.MIN_WEBSITE_SCORE:
            self.logger.info(
                "No confident website for %s (best: %s, score %d)",
                company,
                best_domain,
                domain_scores[best_domain],
            )
            return None

        best_score, best_depth, best_url = domain_best[best_domain]

        # If the winner is a deep link on a domain that clearly matches the
        # company, canonicalize to the homepage.
        domain_root = self._clean(self._domain_root(best_domain))
        company_core = self._normalize_company(company)
        strong_match = (
            domain_root == company_core
            or SequenceMatcher(None, company_core, domain_root).ratio() >= 0.8
        )

        if best_depth > 1 and strong_match:
            scheme = urlparse(best_url).scheme or "https"
            return f"{scheme}://{urlparse(best_url).netloc}/"

        return best_url

    def _find_linkedin(self, company):

        results = self._search(
            f'site:linkedin.com/company "{company}"'
        )

        for result in results:

            url = result.get("href")

            if url and "linkedin.com/company/" in url:
                return url

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
    def get_company_info(self, company):

        website = self._find_best_website(company)

        domain = self._get_domain(website)

        return {
            "company_name": company,
            "company_website": website,
            "company_domain": domain,
            "company_linkedin": self._find_linkedin(company),
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
        ]

        self.df = pd.DataFrame()
        self.urls = np.array([])

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

            if repo_name == "vanshb03/Summer2027-Internships":
                issues = self.get_issues(repo)
                internships = self.parse_issue(issues)
                self.logger.info("Parsed %d internships from issues", len(internships))
                df = pd.concat([df, pd.DataFrame(internships)], ignore_index=True)

            # Only run web searches for companies not already stored —
            # the DB row is the source of truth for known companies.
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

            company_info = {
                company: self.company_search.get_company_info(company)
                for company in new_names
            }

            companies = list(company_info.values())
            internships = df.to_dict(orient="records")

            return internships, companies

        except Exception:
            self.logger.exception("Failed to generate internships")
            raise

    def generate_db(self, soup):
        try:
            dfs = pd.read_html(StringIO(str(soup)))
            df = pd.concat(dfs, ignore_index=True)

            df["Date Posted"] = df["Date Posted"].apply(self.normalize_date)
            cutoff = datetime(2026, 6, 1)
            self.logger.info("Filtering internships posted after %s", cutoff)
            df = df[df["Date Posted"] >= cutoff]
            df["Date Posted"] = df["Date Posted"].dt.strftime("%Y-%m-%d")
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
            posted_date = issue.created_at.strftime("%Y-%m-%d")

            if issue.title != "New Internship":
                continue
            issue = self.markdown.render(issue.body)
            soup = BeautifulSoup(issue, "html.parser")

            fields = {}
            for heading in soup.find_all("h3"):
                value = heading.find_next_sibling("p")
                if value:
                    fields[heading.get_text(strip=True)] = value.get_text(strip=True)

            internship = {
                "company_name": fields.get("Company Name"),
                "job_title": fields.get("Internship Title"),
                "job_url": fields.get("Link to Internship Posting"),
                "job_location": fields.get("Location"),
                "job_type": self.classify_role(fields.get("Internship Title")),
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

        df = df[df["Application/Link"] != "🔒"]

        self.logger.info("Removed %d locked applications", before - len(df))

        df["Application/Link"] = urls

        df["Source Repo"] = "https://github.com/vanshb03/Summer2027-Internships"

        df["Role"] = (
            df["Role"]
            .apply(lambda x: emoji.replace_emoji(str(x), replace=""))
            .str.strip()
        )

        df["Category"] = df["Role"].apply(self.classify_role)

        self.logger.info("Role categories generated")

        df.rename(
            columns={
                "Company": "company_name",
                "Role": "job_title",
                "Location": "job_location",
                "Application/Link": "job_url",
                "Source Repo": "source_repo",
                "Category": "job_type",
                "Date Posted": "job_posted_at",
            },
            inplace=True,
        )

        self.logger.info("Renamed columns")

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

    def insert_internships(self):
        for repo_name in self.repos:
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


if __name__ == "__main__":
    github_internships = GithubInternships()
    github_internships.insert_internships()
