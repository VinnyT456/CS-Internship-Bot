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


class CompanySearch:
    BAD_DOMAINS = {
        "wikipedia.org",
        "en.wikipedia.org",
        "m.wikipedia.org",
        "crunchbase.com",
        "startupranking.com",
        "freshershunt.in",
        "facebook.com",
        "twitter.com",
        "x.com",
    }

    def __init__(self):
        self.ddgs = DDGS()

        self.logger = logging.getLogger("github_internships")
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            handler = logging.FileHandler(
                "logs/github_internships.log",
                mode="a",
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(levelname)s - %(message)s"
                )
            )
            self.logger.addHandler(handler)

    def _search(self, query, max_results=5, retries=3):
        for attempt in range(retries):
            try:
                return list(self.ddgs.text(query, max_results=max_results))

            except DDGSException as e:
                self.logger.warning(
                    f"Search failed ({attempt + 1}/{retries}) for '{query}': {e}"
                )

                if attempt == retries - 1:
                    self.logger.error(
                        f"Giving up searching for '{query}'."
                    )
                    return []

                time.sleep(1)

        return []

    def _find_best_website(self, company_name):
        query = (
            f'"{company_name}" official website '
            "-site:wikipedia.org "
            "-site:crunchbase.com"
        )

        results = self._search(query)

        if not results:
            return None

        for result in results:
            url = result.get("href")

            if not url:
                continue

            domain = urlparse(url).netloc.lower().removeprefix("www.")

            if any(bad in domain for bad in self.BAD_DOMAINS):
                continue

            return url

        return None

    def _find_linkedin(self, company_name):
        query = f'site:linkedin.com/company "{company_name}"'

        results = self._search(query)

        if not results:
            return None

        for result in results:
            url = result.get("href")

            if url and "linkedin.com/company/" in url:
                return url

        return None

    @staticmethod
    def _get_domain(url):
        if not url:
            return None

        return urlparse(url).netloc.lower().removeprefix("www.")

    @staticmethod
    def _get_logo(domain):
        if not domain:
            return None

        return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

    @lru_cache(maxsize=2048)
    def get_company_info(self, company_name):
        website = self._find_best_website(company_name)
        domain = self._get_domain(website)
        linkedin = self._find_linkedin(company_name)
        logo = self._get_logo(domain)

        return {
            "company_name": company_name,
            "company_website": website,
            "company_domain": domain,
            "company_linkedin": linkedin,
            "company_logo": logo,
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

            company_info = {
                company: self.company_search.get_company_info(company)
                for company in df["company_name"].unique()
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
