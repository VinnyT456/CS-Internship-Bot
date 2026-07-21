from internships.github_internships import GithubInternships


class GithubNewGrad(GithubInternships):
    """vanshb03/New-Grad-2027 — same markdown table format as the Summer2027
    internships repo. Single repo, no GitHub-issues submission flow."""

    REPO = "vanshb03/New-Grad-2027"
    TABLE = "new_grads"

    def __init__(self):
        super().__init__()

        # Only this repo, and it uses the same column layout as vanshb03's
        # internship list.
        self.repos = [self.REPO]
        self.repo_column_name = {
            self.REPO: {
                "Company": "company_name",
                "Role": "job_title",
                "Location": "job_location",
                "Application/Link": "job_url",
                "Date Posted": "job_posted_at",
            }
        }

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
                self.supabase_db.insert_internships(internships, self.TABLE)
            else:
                self.logger.info("No new commits found in repo %s", repo_name)
        # No issues flow — this repo has no submission issues


if __name__ == "__main__":
    scraper = GithubNewGrad()
    scraper.current_repo_name = scraper.REPO
    soup = scraper.parse_html(
        scraper.convert_to_html(scraper.get_readme(scraper.get_repo(scraper.REPO)))
    )
    rows = scraper.parse_readme_table(soup)
    print(f"parsed {len(rows)} rows from {scraper.REPO}")
    for r in rows[:5]:
        print(f"  {r['company_name']:20} | {r['job_title'][:35]:35} | {r['job_posted_at']}")
