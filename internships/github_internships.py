import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from github import Github, Auth
import os
from markdown_it import MarkdownIt
from bs4 import BeautifulSoup
import emoji
import logging
import markdown
from database.database import SupabaseDatabase
from datetime import datetime

from database.company_search import CompanySearch

# Concurrent company lookups. Modest to avoid tripping DDG rate limits —
# each company fires 2 searches internally.
POSTED_CUTOFF = datetime(2026, 7, 1)

class GithubInternships:
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
            internships = self.parse_readme_table(soup)

            companies = self.build_company_info(internships)

            return internships, companies

        except Exception:
            self.logger.exception("Failed to generate internships")
            raise

    def build_company_info(self, internships):
        """Look up website/linkedin/logo for each new company, concurrently."""
        existing_names = set(self.supabase_db.get_existing_company_names())

        new_names = [
            name
            for name in dict.fromkeys(i["company_name"] for i in internships)
            if name not in existing_names
        ]

        self.logger.info("Company lookup: %d new companies", len(new_names))

        if not new_names:
            return []

        # Offline & instant now — plain loop, no thread pool needed
        return [self.company_search.get_company_info(name) for name in new_names]

    def parse_readme_table(self, soup):
        try:
            table = soup.find("table")
            if table is None:
                raise ValueError("No table found in README")

            headers = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
            column_map = self.repo_column_name[self.current_repo_name]

            rows = []
            prev = {}

            for tr in table.find_all("tr"):
                cells = tr.find_all("td")

                if len(cells) != len(headers):
                    continue

                row = {}
                link = None

                for header, cell in zip(headers, cells):
                    key = column_map.get(header)
                    if key is None:
                        continue

                    row[key] = cell.get_text(strip=True)

                    if key == "job_url":
                        anchor = cell.find("a", href=True)
                        link = anchor["href"] if anchor else None

                # '↳' means 'same as the row above'
                for key, value in row.items():
                    if value == "↳" and key in prev:
                        row[key] = prev[key]
                prev = dict(row)

                # Locked postings have no application link
                if not link:
                    continue
                row["job_url"] = link

                posted = row.get("job_posted_at")
                if not posted or posted == "-":
                    continue

                try:
                    posted_dt = self.normalize_date(posted)
                except ValueError:
                    continue

                if posted_dt < POSTED_CUTOFF:
                    continue

                row["job_posted_at"] = posted_dt.strftime("%Y-%m-%d")

                row["job_title"] = emoji.replace_emoji(
                    str(row.get("job_title", "")), replace=""
                ).strip()
                row["job_type"] = self.classify_role(row["job_title"])
                row["source_repo"] = f"https://github.com/{self.current_repo_name}"

                rows.append(row)

            self.logger.info(
                "Parsed %d internship rows from README table", len(rows)
            )
            return rows

        except Exception:
            self.logger.exception("Failed parsing README table")
            raise

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
            # Titles vary ('New Internship', 'New Internship - Radix ...',
            # or just the job name), so identify submissions by the issue
            # template's required fields instead of the title
            if not issue.body:
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
                self.logger.info(
                    "Skipping issue #%s (%r): not an internship submission",
                    issue.number,
                    issue.title[:60],
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

        # 'Mon DD' has no year. A month later in the calendar than the current
        # month can't be from this year yet, so it's from last year (e.g. in
        # July, 'Dec 20' → last December, not this coming December).
        now = datetime.now()
        parsed = datetime.strptime(f"{date_str} {now.year}", "%b %d %Y")
        if parsed.month > now.month:
            parsed = parsed.replace(year=now.year - 1)
        return parsed

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

            internships = [
                i
                for i in internships
                if datetime.strptime(i["job_posted_at"], "%Y-%m-%d")
                >= POSTED_CUTOFF
            ]

            if not internships:
                self.logger.info("No issue internships after cutoff")
                return []

            companies = self.build_company_info(internships)
            # Companies first (FK), then rows — bail if the upsert fails
            if companies and self.supabase_db.insert_companies(companies) is None:
                self.logger.error("Company upsert failed for issues — skipping")
                return None
            return self.supabase_db.insert_internships(internships, self.TABLE)

        except Exception:
            self.logger.exception("Failed inserting issue internships")
            return None

    def insert_internships(self):
        for repo_name in self.repos:
            self.current_repo_name = repo_name
            full_repo_name = f"https://github.com/{repo_name}"
            pushed_at = self.get_repo(repo_name).pushed_at

            if self.supabase_db.repo_has_new_commits(full_repo_name, pushed_at):
                self.logger.info("New commits found in repo %s", repo_name)
                internships, companies = self.get_internships(repo_name)

                self.supabase_db.commit_scrape(
                    full_repo_name, pushed_at, internships, companies, self.TABLE
                )
            else:
                self.logger.info("No new commits found in repo %s", repo_name)

        # Issues flow is deliberately outside the commit-time gate
        self.insert_issues_internships()


if __name__ == "__main__":
    github_internships = GithubInternships()
    github_internships.insert_internships()
    