CREATE TABLE public.internships (
    id BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    job_url TEXT NOT NULL,
    job_location TEXT,
    job_type TEXT,
    job_posted_at TIMESTAMPTZ,
    source_repo TEXT,

    company_domain TEXT,
    company_logo TEXT,
    company_website TEXT,
    company_linkedin TEXT,

    sent_to_discord BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (company_name, job_title, job_url)
);