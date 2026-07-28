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
    last_checked_at TIMESTAMPTZ,

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
    last_checked_at TIMESTAMPTZ,

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

-- Uploaded resumes. Users upload a PDF (pdf_path); the bot rasterizes it to a
-- single stacked PNG (image_path) in the Resumes bucket — the image is what the
-- vision model (Gemma) reads. One current resume per user (UNIQUE user_id);
-- re-upload overwrites.
CREATE TABLE IF NOT EXISTS public.resumes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE
        REFERENCES users(id),
    pdf_path TEXT NOT NULL,
    image_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
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

-- Alert subscriptions. A user can have several rows (e.g. one per category,
-- one keyword). A new scraped job is DM'd to a subscriber when it matches their
-- category (if set) AND their keyword (if set) — both null means "everything".
CREATE TABLE IF NOT EXISTS public.subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL
        REFERENCES users(id),
    discord_id BIGINT NOT NULL,     -- denormalized so the DM hook needn't join
    category TEXT,                  -- job_type filter, or NULL for any
    keyword TEXT,                   -- matched against company/title, or NULL
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, category, keyword)
);

ALTER TABLE IF EXISTS public.company_info DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.internships DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.new_grads DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.repo_info DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.users DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.resumes DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.saved_jobs DISABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.subscriptions DISABLE ROW LEVEL SECURITY;