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
import pprint


class GithubInternships:
    def __init__(self):
        load_dotenv()

        self.logger = logging.getLogger("logs/github_internships.log")
        self.logger.setLevel(logging.DEBUG)
        handler = logging.FileHandler(
            "logs/github_internships.log",
            mode="a"
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s"
            )
        )
        self.logger.addHandler(handler)

        self.auth = Auth.Token(os.getenv("GITHUB_TOKEN"))
        self.github = Github(auth=self.auth)

        self.markdown = MarkdownIt()
        self.supabase_db = SupabaseDatabase()

        self.repos = [
            "vanshb03/Summer2027-Internships"
        ]

        self.df = pd.DataFrame()
        self.urls = np.array([])

        self.logger.info("Github internships scraper initialized")

    def get_internships(self):
        for repo_name in self.repos:
            try:
                self.logger.info(
                    "Generating internships from repo: %s",
                    repo_name
                )

                repo = self.get_repo(repo_name)
                readme = self.get_readme(repo)

                self.logger.info(
                    "README retrieved successfully"
                )

                html = self.convert_to_html(readme)
                soup = self.parse_html(html)
                self.urls = self.get_urls(soup)

                self.logger.info(
                    "Found %d application URLs",
                    len(self.urls)
                )

                self.generate_db(soup)

                self.logger.info(
                    "Extracted %d internship rows",
                    len(self.df)
                )

                self.df = self.clean_df(
                    self.df,
                    self.urls
                )
                self.logger.info(
                    "Cleaned dataframe contains %d internships",
                    len(self.df)
                )

                if (repo_name == "vanshb03/Summer2027-Internships"):
                    issues = self.get_issues(repo)
                    internships = self.parse_issue(issues)
                    self.logger.info(
                        "Parsed %d internships from issues",
                        len(internships)
                    )
                    self.df = pd.concat(
                        [self.df, pd.DataFrame(internships)],
                        ignore_index=True
                    )

                self.supabase_db.insert_internships(self.df.to_dict(orient="records"))
            except Exception:
                self.logger.exception("Failed to generate internships")
                raise

    def generate_db(self, soup):
        try:
            dfs = pd.read_html(StringIO(str(soup)))
            self.df = pd.concat(
                dfs,
                ignore_index=True
            )

        except Exception:
            self.logger.exception(
                "Failed to convert HTML table to dataframe"
            )
            raise

    def get_markdown(self, readme):
        return self.markdown.render(readme)

    def get_repo(self, repo_name):
        self.logger.info(
            "Fetching repository: %s",
            repo_name
        )

        return self.github.get_repo(repo_name)

    def get_readme(self, repo):
        self.logger.info(
            "Fetching README"
        )

        return repo.get_readme().decoded_content.decode()

    def get_issues(self, repo):
        self.logger.info(
            "Fetching issues"
        )

        return repo.get_issues(state="open")

    def convert_to_html(self, readme_text):
        self.logger.info(
            "Converting markdown to HTML"
        )

        html = markdown.markdown(
            readme_text,
            extensions=["tables"]
        )

        html = html.replace("</br>", " | ")
        html = html.replace("<br>", " | ")
        html = html.replace("<br/>", " | ")
        html = html.replace("<br />", " | ")

        return html

    def parse_html(self, html):
        self.logger.info(
            "Parsing HTML"
        )

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        for summary in soup.find_all("summary"):
            summary.decompose()

        for details in soup.find_all("details"):
            details.unwrap()

        return soup

    def parse_issue(self, issues):
        internships = []
        for issue in issues:
            if (issue.title != "New Internship"):
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
                "source_repo": "https://github.com/vanshb03/Summer2027-Internships",
            }
            internships.append(internship)
        return internships

    def get_urls(self, soup):
        return np.array(
            [
                link.get("href")
                for link in soup.select("td a[href]")
            ]
        )

    def clean_df(self, df, urls):
        self.logger.info(
            "Cleaning dataframe"
        )

        df = df.replace(
            "↳",
            np.nan
        ).ffill()

        before = len(df)

        df = df[
            df["Application/Link"] != "🔒"
        ]

        self.logger.info(
            "Removed %d locked applications",
            before - len(df)
        )

        df["Application/Link"] = urls

        df["Source Repo"] = (
            "https://github.com/vanshb03/Summer2027-Internships"
        )

        if "Date Posted" in df.columns:
            df.drop(
                columns=["Date Posted"],
                inplace=True
            )

        df["Role"] = (
            df["Role"]
            .apply(
                lambda x: emoji.replace_emoji(
                    str(x),
                    replace=""
                )
            )
            .str.strip()
        )

        df["Category"] = (
            df["Role"]
            .apply(self.classify_role)
        )

        self.logger.info(
            "Role categories generated"
        )

        df.rename(columns={
            "Company":"company_name",
            "Role":"job_title",
            "Location":"job_location",
            "Application/Link":"job_url",
            "Source Repo":"source_repo",
            "Category":"job_type"
        }, inplace=True)

        self.logger.info(
            "Renamed columns"
        )

        return df

    def classify_role(self, title: str) -> str:
        title = title.lower()

        if any(k in title for k in [
            "machine learning",
            "ml",
            "artificial intelligence",
            "ai",
            "llm",
            "deep learning"
        ]):
            return "AI / ML"

        if any(k in title for k in [
            "data science",
            "data scientist",
            "data analyst",
            "analytics",
            "data"
        ]):
            return "Data Science"

        if any(k in title for k in [
            "quant",
            "trading",
            "researcher",
            "research",
            "quantitative"
        ]):
            return "Quant"

        if "product" in title:
            return "Product"

        if any(k in title for k in [
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
            "engineering"
        ]):
            return "Software Engineering"

        return "Other"

if __name__ == "__main__":
    github_internships = GithubInternships()
    github_internships.get_internships()