from dotenv import load_dotenv
from github import Github, Auth
import os
from bs4 import BeautifulSoup
import emoji
import logging
import markdown
import re
from database.database import SupabaseDatabase
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from database.company_search import CompanySearch

COMPANY_LOOKUP_CONCURRENCY = 5
POSTED_CUTOFF = datetime(2026, 6, 1)


class SimplifyInternships:
    TABLE = "internships"

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

        self.supabase_db = SupabaseDatabase()
        self.company_search = CompanySearch()

        self.repo_name = "SimplifyJobs/Summer2026-Internships"

        self.column_map = {
            "Company": "company_name",
            "Role": "job_title",
            "Location": "job_location",
        }

    def get_repo(self):
        self.logger.info("Fetching repository: %s", self.repo_name)
        return self.github.get_repo(self.repo_name)

    def get_readme(self, repo):
        self.logger.info("Fetching README")
        return repo.get_readme().decoded_content.decode()

    def parse_html(self, readme_text):
        # README table is raw HTML; run through markdown so any markdown
        # fragments render consistently, then parse.
        self.logger.info("Parsing README HTML")
        html = markdown.markdown(readme_text, extensions=["tables"])
        return BeautifulSoup(html, "html.parser")


    def _relative_age_to_date(self, age_text):
        """'0d' / '5d' / '3mo' -> an absolute date."""
        age_text = age_text.strip().lower()
        match = re.match(r"(\d+)\s*(mo|d|w|y)", age_text)
        if not match:
            raise ValueError(f"Unrecognized age: {age_text!r}")

        n = int(match.group(1))
        unit = match.group(2)
        days = {"d": 1, "w": 7, "mo": 30, "y": 365}[unit]
        return datetime.now() - timedelta(days=n * days)

    def parse_readme_table(self, soup):
        try:
            table = soup.find("table")
            if table is None:
                raise ValueError("No table found in README")

            headers = [
                th.get_text(strip=True) for th in table.find("thead").find_all("th")
            ]

            rows = []
            prev = {}

            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) != len(headers):
                    continue

                row = {}
                job_url = None

                for header, cell in zip(headers, cells):
                    if header == "Application":
                        # first anchor = real ATS apply link (second is a
                        # simplify.jobs redirect)
                        anchor = cell.find("a", href=True)
                        if anchor:
                            job_url = anchor["href"]
                        continue

                    if header == "Age":
                        row["_age"] = cell.get_text(strip=True)
                        continue

                    key = self.column_map.get(header)
                    if key:
                        row[key] = cell.get_text(strip=True)

                # '↳' means 'same company as the row above'
                if row.get("company_name") in ("↳", ""):
                    row["company_name"] = prev.get("company_name")

                company = row.get("company_name")
                if not company or not job_url:
                    continue

                # Locked postings only have the simplify.jobs redirect anchor
                if "simplify.jobs/p/" in job_url:
                    continue

                age = row.get("_age")
                if not age:
                    continue
                try:
                    posted_dt = self._relative_age_to_date(age)
                except ValueError:
                    continue
                if posted_dt < POSTED_CUTOFF:
                    continue

                row["job_url"] = job_url
                row["job_posted_at"] = posted_dt.strftime("%Y-%m-%d")
                row["company_name"] = emoji.replace_emoji(company, replace="").strip()
                row["job_title"] = emoji.replace_emoji(
                    str(row.get("job_title", "")), replace=""
                ).strip()
                row["job_type"] = self.classify_role(row["job_title"])
                row["source_repo"] = f"https://github.com/{self.repo_name}"
                row.pop("_age", None)

                prev = {"company_name": row["company_name"]}
                rows.append(row)

            self.logger.info("Parsed %d internship rows from README table", len(rows))
            return rows

        except Exception:
            self.logger.exception("Failed parsing README table")
            raise

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
            for k in ["data science", "data scientist", "data analyst", "analytics", "data"]
        ):
            return "Data Science"

        if any(
            k in title
            for k in ["quant", "trading", "researcher", "research", "quantitative"]
        ):
            return "Quant"

        if "product" in title:
            return "Product"

        return "Software Engineering"

    def build_company_info(self, internships):
        """Look up website/linkedin/logo for each new company, concurrently.
        The Company cell links simplify.jobs, so we search the domain."""
        existing_names = set(self.supabase_db.get_existing_company_names())

        new_names = [
            name
            for name in dict.fromkeys(i["company_name"] for i in internships)
            if name not in existing_names
        ]

        self.logger.info("Company lookup: %d new companies", len(new_names))

        if not new_names:
            return []

        with ThreadPoolExecutor(max_workers=COMPANY_LOOKUP_CONCURRENCY) as pool:
            return list(pool.map(self.company_search.get_company_info, new_names))

    def get_internships(self):
        try:
            self.logger.info("Generating internships from repo: %s", self.repo_name)

            repo = self.get_repo()
            readme = self.get_readme(repo)
            self.logger.info("README retrieved successfully")

            soup = self.parse_html(readme)
            internships = self.parse_readme_table(soup)

            companies = self.build_company_info(internships)

            return internships, companies

        except Exception:
            self.logger.exception("Failed to generate internships")
            raise

    def insert_internships(self):
        full_repo_name = f"https://github.com/{self.repo_name}"
        current_commit_time = self.get_repo().pushed_at.strftime("%Y-%m-%d %H:%M:%S")
        last_commit_time = self.supabase_db.get_repo_update_time(full_repo_name)

        if last_commit_time is not None and current_commit_time <= last_commit_time:
            self.logger.info("No new commits found in repo %s", self.repo_name)
            return

        self.logger.info("New commits found in repo %s", self.repo_name)
        internships, companies = self.get_internships()

        self.supabase_db.insert_companies(companies)
        self.supabase_db.insert_repo_update_time(full_repo_name, current_commit_time)
        self.supabase_db.insert_internships(internships, self.TABLE)


if __name__ == "__main__":
    scraper = SimplifyInternships()
    soup = scraper.parse_html(scraper.get_readme(scraper.get_repo()))
    rows = scraper.parse_readme_table(soup)
    print(f"parsed {len(rows)} rows")
    for r in rows[:6]:
        print(f"  {r['company_name']:18} | {r['job_title'][:32]:32} | {r['job_posted_at']}")
        print(f"     url={r['job_url'][:75]}")
