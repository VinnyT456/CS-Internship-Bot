DROP TABLE IF EXISTS public.internships;
DROP TABLE IF EXISTS public.company_info;
DROP TABLE IF EXISTS public.repo_info;

CREATE TABLE public.repo_info (
    source_repo TEXT NOT NULL PRIMARY KEY,
    last_updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE (source_repo)
);

CREATE TABLE public.company_info (
    company_name TEXT NOT NULL PRIMARY KEY,
    company_website TEXT,
    company_linkedin TEXT,
    company_logo TEXT,
    company_domain TEXT,

    last_updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (company_name)
);

CREATE TABLE public.internships (
    id BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL
        REFERENCES company_info(company_name),
    job_title TEXT NOT NULL,
    job_url TEXT NOT NULL,
    job_location TEXT,
    job_type TEXT,
    job_posted_at DATE,
    source_repo TEXT NOT NULL
        REFERENCES repo_info(source_repo),

    sent_to_discord BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    discord_message_id BIGINT,

    UNIQUE (company_name, job_title, job_url)
);

ALTER TABLE public.company_info DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.internships DISABLE ROW LEVEL SECURITY;
alter publication supabase_realtime add table public.internships;
alter publication supabase_realtime add table public.company_info;
alter publication supabase_realtime add table public.repo_info;