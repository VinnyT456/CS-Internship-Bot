import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from internships.simplify_internships import SimplifyInternships


class SimplifyNewGrad(SimplifyInternships):
    """SimplifyJobs/New-Grad-Positions — same HTML table format as the
    Summer2026 internship repo, different repo. Everything else inherited."""

    TABLE = "new_grads"

    def __init__(self):
        super().__init__()
        self.repo_name = "SimplifyJobs/New-Grad-Positions"


if __name__ == "__main__":
    scraper = SimplifyNewGrad()
    soup = scraper.parse_html(scraper.get_readme(scraper.get_repo()))
    rows = scraper.parse_readme_table(soup)
    print(f"parsed {len(rows)} rows from {scraper.repo_name}")
    for r in rows[:6]:
        print(f"  {r['company_name']:18} | {r['job_title'][:32]:32} | {r['job_posted_at']}")
        print(f"     url={r['job_url'][:75]}")
