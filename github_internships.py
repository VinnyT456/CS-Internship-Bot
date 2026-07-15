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

        self.repos = ["vanshb03/Summer2027-Internships"]

        self.df = pd.DataFrame()
        self.urls = np.array([])

        self.logger.info("Github internships scraper initialized")

    def get_internships(self):
        internships = []
        for repo_name in self.repos:
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
                    self.logger.info(
                        "Parsed %d internships from issues", len(internships)
                    )
                    df = pd.concat([df, pd.DataFrame(internships)], ignore_index=True)

                self.supabase_db.insert_internships(df.to_dict(orient="records"))
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
        return np.array([link.get("href") for link in soup.select("td a[href]")])[:count]

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


if __name__ == "__main__":
    github_internships = GithubInternships()
    github_internships.get_internships()
