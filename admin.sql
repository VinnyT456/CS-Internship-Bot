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

-- Smart (résumé-match) alerts: a subscription flagged `smart` gets the new job
-- AI-scored against the user's résumé; a high score DMs them the role. `smart`
-- rows may have NULL category/keyword — the match is résumé-driven, not filters.
ALTER TABLE IF EXISTS public.subscriptions
    ADD COLUMN IF NOT EXISTS smart BOOLEAN NOT NULL DEFAULT false;

-- Dedup log for smart alerts so a user isn't DM'd the same job twice across the
-- rolling scrape cycles. One row per (user, job) once a smart alert is sent.
CREATE TABLE IF NOT EXISTS public.smart_alerts_sent (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    job_table TEXT NOT NULL,        -- 'internships' | 'new_grads'
    job_id BIGINT NOT NULL,
    score INT,                      -- the match score at send time (for audit)
    created_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, job_table, job_id)
);

ALTER TABLE IF EXISTS public.smart_alerts_sent DISABLE ROW LEVEL SECURITY;

-- LeetCode grind channel: one row per (user, problem) when a user marks a
-- problem solved (✅ react on the daily post, or /leetcode). Powers /streak and
-- the solve count. UNIQUE keeps re-reacting idempotent; un-reacting deletes it.
CREATE TABLE IF NOT EXISTS public.leetcode_solves (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    problem_slug TEXT NOT NULL,
    difficulty TEXT,                -- 'Easy' | 'Medium' | 'Hard' (nullable)
    solved_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, problem_slug)
);

ALTER TABLE IF EXISTS public.leetcode_solves DISABLE ROW LEVEL SECURITY;

-- Struggle log: how the attempt went, so /weakspots can find the patterns a user
-- keeps missing and spaced-repetition can resurface them. 'solved' = clean clear,
-- 'struggled' = got it but needed the solution/hints, 'failed' = couldn't. Topic
-- tags (comma-joined) let /weakspots aggregate by pattern. review_at drives the
-- spaced-repetition due list.
ALTER TABLE IF EXISTS public.leetcode_solves
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'solved',
    ADD COLUMN IF NOT EXISTS topics TEXT,
    ADD COLUMN IF NOT EXISTS review_at TIMESTAMPTZ;

-- Sent-log for the daily LeetCode post: one row per problem_date once the daily
-- embed is posted, so the scheduled loop + startup catch-up never double-post and
-- a day is never skipped (same idea as the internship scrape gate). UNIQUE on
-- problem_date is the dedup key — one daily per calendar day.
CREATE TABLE IF NOT EXISTS public.leetcode_daily_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    problem_date DATE NOT NULL,
    problem_slug TEXT NOT NULL,
    message_id BIGINT,              -- the posted Discord message (for audit/edit)
    sent_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (problem_date)
);

ALTER TABLE IF EXISTS public.leetcode_daily_posts DISABLE ROW LEVEL SECURITY;
-- Sent-log for feature announcements. The bot auto-posts the pending changelog
-- once on startup; this table keys on a version tag (a hash of the changelog) so
-- a restart never re-posts the same announcement, and editing the changelog (new
-- hash) posts a fresh one exactly once.
CREATE TABLE IF NOT EXISTS public.sent_announcements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version TEXT NOT NULL,           -- hash of the changelog content
    message_id BIGINT,               -- the posted Discord message (audit)
    sent_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (version)
);

ALTER TABLE IF EXISTS public.sent_announcements DISABLE ROW LEVEL SECURITY;

-- DS&A learning progress: one row per (user, pattern) when a user learns a
-- roadmap pattern via /learn. Powers /roadmap completion and the "learn next"
-- suggestion. pattern_key matches the keys in leetcode/dsa_roadmap.json.
CREATE TABLE IF NOT EXISTS public.leetcode_learned (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    pattern_key TEXT NOT NULL,
    learned_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (user_id, pattern_key)
);

ALTER TABLE IF EXISTS public.leetcode_learned DISABLE ROW LEVEL SECURITY;
