from internships.jobright_internships import JobrightInternships


class JobrightNewGrad(JobrightInternships):
    """jobright-ai/2026-Software-Engineer-New-Grad — same table format as the
    internship repo, different repo. Everything else is inherited."""

    REPO = "jobright-ai/2026-Software-Engineer-New-Grad"
    TABLE = "new_grads"


if __name__ == "__main__":
    scraper = JobrightNewGrad()
    repo = scraper.get_repo()
    soup = scraper.parse_html(scraper.convert_to_html(scraper.get_readme(repo)))
    rows = scraper.parse_readme_table(soup)
    print(f"parsed {len(rows)} rows from {scraper.REPO}")
    for r in rows[:5]:
        print(f"  {r['company_name']:20} | {r['job_title'][:35]:35} | {r['job_posted_at']} | dom={r.get('_company_domain')}")
