import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from github import Github, Auth
import os
from bs4 import BeautifulSoup
import emoji
import logging
import markdown
from database.database import SupabaseDatabase
from datetime import datetime
from urllib.parse import urlparse

from database.company_search import CompanySearch

POSTED_CUTOFF = datetime(2026, 7, 1)


class JobrightInternships:
    """Scraper for jobright-ai/2026-Software-Engineer-Internship.

    Differs from the vanshb03 repo:
    - single repo, default branch is 'master'
    - columns: Company | Job Title | Location | Work Model | Date Posted
    - the Company cell links the company's own website (no domain search)
    - the application URL lives in the Job Title cell (jobright.ai links)
    - no GitHub-issues submission flow
    """

    REPO = "jobright-ai/2026-Software-Engineer-Internship"
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

        # header text -> internship field
        self.column_map = {
            "Company": "company_name",
            "Job Title": "job_title",
            "Location": "job_location",
            "Date Posted": "job_posted_at",
        }

    # ------------------------------------------------------------------ #
    # GitHub / HTML plumbing
    # ------------------------------------------------------------------ #

    def get_repo(self):
        self.logger.info("Fetching repository: %s", self.REPO)
        return self.github.get_repo(self.REPO)

    def get_readme(self, repo):
        self.logger.info("Fetching README")
        return repo.get_readme().decoded_content.decode()

    def convert_to_html(self, readme_text):
        self.logger.info("Converting markdown to HTML")
        return markdown.markdown(readme_text, extensions=["tables"])

    def parse_html(self, html):
        self.logger.info("Parsing HTML")
        return BeautifulSoup(html, "html.parser")

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

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
            prev_domain = None

            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) != len(headers):
                    continue

                row = {}
                job_url = None
                company_domain = None

                for header, cell in zip(headers, cells):
                    key = self.column_map.get(header)

                    if header == "Company":
                        text = cell.get_text(strip=True)
                        row["company_name"] = text
                        # Company cell links the company's own website
                        anchor = cell.find("a", href=True)
                        if anchor and text != "↳":
                            company_domain = (
                                urlparse(anchor["href"]).netloc.lower()
                                .replace("www.", "")
                            )
                        continue

                    if header == "Job Title":
                        row["job_title"] = cell.get_text(strip=True)
                        anchor = cell.find("a", href=True)
                        job_url = anchor["href"] if anchor else None
                        continue

                    if key:
                        row[key] = cell.get_text(strip=True)

                # '↳' means 'same company as the row above'
                if row.get("company_name") == "↳":
                    row["company_name"] = prev.get("company_name")
                    company_domain = prev_domain

                if not row.get("company_name") or not job_url:
                    continue

                posted = row.get("job_posted_at")
                if not posted or posted == "-":
                    continue
                try:
                    posted_dt = self.normalize_date(posted)
                except ValueError:
                    continue
                if posted_dt < POSTED_CUTOFF:
                    continue

                row["job_url"] = job_url
                row["job_posted_at"] = posted_dt.strftime("%Y-%m-%d")
                row["job_title"] = emoji.replace_emoji(
                    str(row.get("job_title", "")), replace=""
                ).strip()
                row["job_type"] = self.classify_role(row["job_title"])
                row["source_repo"] = f"https://github.com/{self.REPO}"
                row["_company_domain"] = company_domain  # consumed below

                prev = dict(row)
                prev_domain = company_domain
                rows.append(row)

            self.logger.info("Parsed %d internship rows from README table", len(rows))
            return rows

        except Exception:
            self.logger.exception("Failed parsing README table")
            raise

    def normalize_date(self, date_str):
        date_str = date_str.strip()
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            pass
        current_year = datetime.now().year
        return datetime.strptime(f"{date_str} {current_year}", "%b %d %Y")

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

    # ------------------------------------------------------------------ #
    # Company enrichment
    # ------------------------------------------------------------------ #

    def build_company_info(self, internships):
        """Look up info for each new company, concurrently. The Company cell
        already gives the website, so only LinkedIn needs searching."""
        existing_names = set(self.supabase_db.get_existing_company_names())

        # first-seen domain per new company name
        domains = {}
        for i in internships:
            name = i["company_name"]
            if name in existing_names or name in domains:
                continue
            domains[name] = i.get("_company_domain")

        self.logger.info("Company lookup: %d new companies", len(domains))

        if not domains:
            return []

        def lookup(item):
            name, domain = item
            return self.company_search.get_company_info(name, known_domain=domain)

        return [lookup(item) for item in domains.items()]

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #

    def get_internships(self):
        try:
            self.logger.info("Generating internships from repo: %s", self.REPO)

            repo = self.get_repo()
            readme = self.get_readme(repo)
            self.logger.info("README retrieved successfully")

            soup = self.parse_html(self.convert_to_html(readme))
            internships = self.parse_readme_table(soup)

            companies = self.build_company_info(internships)

            # strip the internal helper field before DB insert
            for i in internships:
                i.pop("_company_domain", None)

            return internships, companies

        except Exception:
            self.logger.exception("Failed to generate internships")
            raise

    def insert_internships(self):
        full_repo_name = f"https://github.com/{self.REPO}"
        current_commit_time = self.get_repo().pushed_at.strftime("%Y-%m-%d %H:%M:%S")
        last_commit_time = self.supabase_db.get_repo_update_time(full_repo_name)

        if last_commit_time is not None and current_commit_time <= last_commit_time:
            self.logger.info("No new commits found in repo %s", self.REPO)
            return

        self.logger.info("New commits found in repo %s", self.REPO)
        internships, companies = self.get_internships()

        self.supabase_db.insert_companies(companies)
        self.supabase_db.insert_repo_update_time(full_repo_name, current_commit_time)
        self.supabase_db.insert_internships(internships, self.TABLE)


if __name__ == "__main__":
    scraper = JobrightInternships()
    repo = scraper.get_repo()
    soup = scraper.parse_html(scraper.convert_to_html(scraper.get_readme(repo)))
    rows = scraper.parse_readme_table(soup)
    print(f"parsed {len(rows)} rows")
    for r in rows[:5]:
        print(f"  {r['company_name']:20} | {r['job_title'][:35]:35} | {r['job_posted_at']} | dom={r.get('_company_domain')}")
        print(f"     url={r['job_url'][:70]}")
