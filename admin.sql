CREATE TABLE IF NOT EXISTS public.repo_info (
    source_repo TEXT NOT NULL PRIMARY KEY,
    last_updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE (source_repo)
);

CREATE TABLE IF NOT EXISTS public.company_info (
    company_name TEXT NOT NULL PRIMARY KEY,
    company_website TEXT,
    company_linkedin TEXT,
    company_logo TEXT,
    company_domain TEXT,

    last_updated_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (company_name)
);

CREATE TABLE IF NOT EXISTS public.internships (
    id BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL
        REFERENCES company_info(company_name),
    job_title TEXT NOT NULL,
    job_url TEXT NOT NULL,
    -- Aggregator posting page the detail scraper reads. For jobright this is
    -- job_url; for simplify it's the simplify.jobs/p/ link while job_url
    -- stays the direct ATS apply link.
    detail_url TEXT,
    job_location TEXT,
    job_type TEXT,
    job_posted_at DATE,
    source_repo TEXT NOT NULL
        REFERENCES repo_info(source_repo),

    -- Filled in by details/ from the aggregator's posting page.
    job_summary TEXT,
    job_responsibilities TEXT[],
    job_requirements TEXT[],
    job_benefits TEXT[],
    job_tags TEXT[],
    comp_min INTEGER,             -- annualized thousands (125 = $125k/yr)
    comp_max INTEGER,
    salary_desc TEXT,             -- as displayed, e.g. '$50 - $70/hr'
    employment_type TEXT,
    seniority TEXT,
    work_model TEXT,              -- Remote | Hybrid | On-site
    is_closed BOOLEAN NOT NULL DEFAULT FALSE,

    sent_to_discord BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    discord_message_id BIGINT,

    -- Coarse backstop; the app dedups on (company, title, normalized
    -- location) since the same job appears under different aggregator URLs.
    UNIQUE (company_name, job_title, job_location)
);

CREATE TABLE IF NOT EXISTS public.new_grads (
    id BIGSERIAL PRIMARY KEY,
    company_name TEXT NOT NULL
        REFERENCES company_info(company_name),
    job_title TEXT NOT NULL,
    job_url TEXT NOT NULL,
    -- Aggregator posting page the detail scraper reads. For jobright this is
    -- job_url; for simplify it's the simplify.jobs/p/ link while job_url
    -- stays the direct ATS apply link.
    detail_url TEXT,
    job_location TEXT,
    job_type TEXT,
    job_posted_at DATE,
    source_repo TEXT NOT NULL
        REFERENCES repo_info(source_repo),

    -- Filled in by details/ from the aggregator's posting page.
    job_summary TEXT,
    job_responsibilities TEXT[],
    job_requirements TEXT[],
    job_benefits TEXT[],
    job_tags TEXT[],
    comp_min INTEGER,             -- annualized thousands (125 = $125k/yr)
    comp_max INTEGER,
    salary_desc TEXT,             -- as displayed, e.g. '$50 - $70/hr'
    employment_type TEXT,
    seniority TEXT,
    work_model TEXT,              -- Remote | Hybrid | On-site
    is_closed BOOLEAN NOT NULL DEFAULT FALSE,

    sent_to_discord BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    discord_message_id BIGINT,

    -- Coarse backstop; the app dedups on (company, title, normalized
    -- location) since the same job appears under different aggregator URLs.
    UNIQUE (company_name, job_title, job_location)
);

-- Discord members who interact with the bot (e.g. resume uploads).
CREATE TABLE IF NOT EXISTS public.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    discord_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    display_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Uploaded resumes; the file lives in storage, parsed_text holds the extract.
CREATE TABLE IF NOT EXISTS public.resumes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL
        REFERENCES users(id),
    storage_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    parsed_text TEXT,
    uploaded_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Jobs a member saved via the Save button. job_table + job_id is a
-- polymorphic reference (a job lives in internships OR new_grads), so there's
-- no single FK — the app guarantees the pair points at a real row.
CREATE TABLE IF NOT EXISTS public.saved_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL
        REFERENCES users(id),
    job_table TEXT NOT NULL,          -- 'internships' | 'new_grads'
    job_id BIGINT NOT NULL,
    saved_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, job_table, job_id)
);

ALTER TABLE IF EXISTS public.company_info DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.internships DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.new_grads DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.repo_info DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.users DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.resumes DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.saved_jobs DISABLE ROW LEVEL SECURITY;