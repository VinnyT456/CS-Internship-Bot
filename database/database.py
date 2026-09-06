import os
import re
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client


# --- US-location classifier ---------------------------------------------------
# Two-letter USPS state/territory codes.
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho", "illinois",
    "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "puerto rico",
}
_US_MARKERS = ("united states", "u.s.", "u.s.a", "usa", "us-", "-us", "(us)", "us,")
# Canadian province codes — the main abbreviation collision (e.g. "Toronto, ON"
# has no country word but ON = Ontario, not a US state). Reject when one appears.
_CA_PROVINCES = {"ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU"}
# Non-US country names that appear in this feed's locations — an explicit reject.
_NON_US_COUNTRIES = {
    "canada", "united kingdom", "uk", "england", "scotland", "ireland", "india",
    "germany", "france", "spain", "italy", "netherlands", "poland", "romania",
    "mexico", "brazil", "argentina", "china", "japan", "singapore", "australia",
    "new zealand", "israel", "switzerland", "sweden", "norway", "denmark",
    "portugal", "belgium", "austria", "czech", "hungary", "ukraine", "turkey",
    "egypt", "nigeria", "kenya", "south africa", "uae", "dubai", "qatar",
    "philippines", "vietnam", "thailand", "malaysia", "indonesia", "taiwan",
    "hong kong", "korea", "colombia", "chile", "peru", "costa rica",
}


def is_us_location(location):
    """Best-effort: is this job location in the US? Handles 'United States',
    'City, ST, United States', a bare state name/abbr, and 'Remote (US)'. Rejects
    known foreign countries. A blank or bare 'Remote' location is treated as US
    (ambiguous — keep it rather than silently drop a possibly-US remote role)."""
    if not location:
        return True  # unknown → don't drop
    low = location.lower().strip()

    # 1) explicit non-US country anywhere → reject (unless it also names the US).
    has_us_marker = any(m in low for m in _US_MARKERS)
    for c in _NON_US_COUNTRIES:
        if re.search(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])", low):
            if not has_us_marker:
                return False
    # 2) explicit US marker → accept.
    if has_us_marker:
        return True
    # 3) a US state name present → accept.
    for s in _US_STATE_NAMES:
        if re.search(r"(?<![a-z])" + re.escape(s) + r"(?![a-z])", low):
            return True
    # 4) a two-letter code as its own token: US state → accept; Canadian province
    #    (no US marker) → reject (e.g. "Toronto, ON").
    tokens = {t.upper() for t in re.split(r"[,\s/|]+", location.strip())}
    if tokens & _US_STATE_ABBR:
        return True
    if tokens & _CA_PROVINCES:
        return False
    # 5) bare 'Remote' / 'Hybrid' with no country → ambiguous, keep it.
    if low in ("remote", "hybrid", "on-site", "onsite") or "remote" in low:
        return True
    # 6) nothing identifiable → keep (don't drop on uncertainty).
    return True


class SupabaseDatabase:
    def __init__(self):

        load_dotenv()

        self.logger = logging.getLogger("logs/database.log")
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            file_handler = logging.FileHandler("logs/database.log", mode="a")
            file_handler.setLevel(logging.DEBUG)

            formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

        self.supabase = create_client(
            supabase_url=os.getenv("SUPABASE_URL"),
            supabase_key=os.getenv("SUPABASE_KEY"),
        )

        self.internships_table = "internships"
        self.new_grads_table = "new_grads"
        self.companies_table = "company_info"
        self.repo_table = "repo_info"
        self.users_table = "users"
        self.saved_jobs_table = "saved_jobs"
        self.score_cache_table = "score_cache"
        self.tailor_cache_table = "tailor_cache"

    # Bump whenever the Score prompt / output schema changes so stale rows
    # (old voice, missing bilingual fields) are ignored and regenerated.
    SCORE_CACHE_VERSION = 52

    # --- Score cache ---------------------------------------------------
    def get_cached_score(self, user_uuid, job_table, job_id):
        """The cached Score JSON for (user, job), or None. Rows written under an
        older cache version are treated as a miss so they get regenerated with
        the current prompt (Silver Wolf voice + bilingual fields)."""
        try:
            data = (
                self.supabase.table(self.score_cache_table)
                .select("result")
                .eq("user_id", user_uuid)
                .eq("job_table", job_table)
                .eq("job_id", int(job_id))
                .limit(1)
                .execute()
                .data
            )
            if not data:
                return None
            result = data[0]["result"]
            if not isinstance(result, dict) or result.get("_v") != self.SCORE_CACHE_VERSION:
                return None
            return result
        except Exception:
            self.logger.exception("Failed reading score cache")
            return None

    def set_cached_score(self, user_uuid, job_table, job_id, result):
        """Upsert a Score result for (user, job). Best-effort. Stamps the current
        cache version so a later prompt change can invalidate it."""
        try:
            if isinstance(result, dict):
                result = {**result, "_v": self.SCORE_CACHE_VERSION}
            self.supabase.table(self.score_cache_table).upsert(
                {
                    "user_id": user_uuid,
                    "job_table": job_table,
                    "job_id": int(job_id),
                    "result": result,
                },
                on_conflict="user_id,job_table,job_id",
            ).execute()
        except Exception:
            self.logger.exception("Failed writing score cache")

    def clear_score_cache(self, user_uuid):
        """Drop all cached scores for a user — call when their resume changes."""
        try:
            self.supabase.table(self.score_cache_table).delete().eq(
                "user_id", user_uuid
            ).execute()
        except Exception:
            self.logger.exception("Failed clearing score cache for %s", user_uuid)

    # --- Tailor cache --------------------------------------------------
    def get_cached_tailor(self, user_uuid, job_table, job_id):
        """The cached tailor blob (a JSON string) for (user, job), or None.
        Version invalidation lives in the blob itself (see _load_blob)."""
        try:
            data = (
                self.supabase.table(self.tailor_cache_table)
                .select("yaml")
                .eq("user_id", user_uuid)
                .eq("job_table", job_table)
                .eq("job_id", int(job_id))
                .limit(1)
                .execute()
                .data
            )
            return data[0]["yaml"] if data else None
        except Exception:
            self.logger.exception("Failed reading tailor cache")
            return None

    def set_cached_tailor(self, user_uuid, job_table, job_id, yaml_text):
        """Upsert the tailor blob (a JSON string) for (user, job). Best-effort."""
        try:
            self.supabase.table(self.tailor_cache_table).upsert(
                {
                    "user_id": user_uuid,
                    "job_table": job_table,
                    "job_id": int(job_id),
                    "yaml": yaml_text,
                },
                on_conflict="user_id,job_table,job_id",
            ).execute()
        except Exception:
            self.logger.exception("Failed writing tailor cache")

    def clear_tailor_cache(self, user_uuid):
        """Drop all cached tailors for a user — call when their resume changes."""
        try:
            self.supabase.table(self.tailor_cache_table).delete().eq(
                "user_id", user_uuid
            ).execute()
        except Exception:
            self.logger.exception("Failed clearing tailor cache for %s", user_uuid)

    def get_or_create_user(self, discord_id, username=None, display_name=None):
        """Return the users.id UUID for a Discord member, inserting the row on
        first contact. Used by the Save button so a save always has a user to
        attach to."""
        try:
            existing = (
                self.supabase.table(self.users_table)
                .select("id")
                .eq("discord_id", discord_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                return existing.data[0]["id"]

            created = (
                self.supabase.table(self.users_table)
                .insert(
                    {
                        "discord_id": discord_id,
                        "username": username,
                        "display_name": display_name,
                    }
                )
                .execute()
            )
            return created.data[0]["id"] if created.data else None
        except Exception:
            self.logger.exception("Failed get_or_create_user for %s", discord_id)
            return None

    def get_github_username(self, user_uuid):
        """The GitHub username the user stored for repo analysis, or None.
        Requires the users.github_username column (see admin.sql migration)."""
        try:
            data = (
                self.supabase.table(self.users_table)
                .select("github_username")
                .eq("id", user_uuid)
                .limit(1)
                .execute()
                .data
            )
            if not data:
                return None
            return (data[0].get("github_username") or "").strip() or None
        except Exception:
            self.logger.exception("Failed reading github_username for %s", user_uuid)
            return None

    def set_github_username(self, user_uuid, github_username):
        """Persist the user's GitHub username so repo analysis works even when
        their résumé omits a GitHub link. Pass None/empty to clear it."""
        try:
            value = (github_username or "").strip() or None
            (
                self.supabase.table(self.users_table)
                .update({"github_username": value})
                .eq("id", user_uuid)
                .execute()
            )
            return True
        except Exception:
            self.logger.exception("Failed setting github_username for %s", user_uuid)
            return False

    def upsert_github_projects(self, user_uuid, rows):
        """Upsert the user's extracted GitHub projects into github_projects,
        keyed by (user_id, repo_name). `rows` = list of dicts with keys
        repo_name/summary/tech/category/finished_at/url. Refreshes scanned_at.
        Returns the number of rows written."""
        if not user_uuid or not rows:
            return 0
        payload = []
        for r in rows:
            name = (r.get("repo_name") or "").strip()
            if not name:
                continue
            payload.append(
                {
                    "user_id": user_uuid,
                    "repo_name": name,
                    "summary": r.get("summary") or None,
                    "tech": r.get("tech") or [],
                    "category": r.get("category") or None,
                    "finished_at": r.get("finished_at") or None,
                    "url": r.get("url") or None,
                }
            )
        if not payload:
            return 0
        try:
            (
                self.supabase.table("github_projects")
                .upsert(payload, on_conflict="user_id,repo_name")
                .execute()
            )
            return len(payload)
        except Exception:
            self.logger.exception(
                "Failed upserting github_projects for %s", user_uuid
            )
            return 0

    def get_github_projects(self, user_uuid):
        """Return the user's stored GitHub projects (most-recent finish first),
        or []. Reads from github_projects — no GitHub call."""
        if not user_uuid:
            return []
        try:
            return (
                self.supabase.table("github_projects")
                .select("*")
                .eq("user_id", user_uuid)
                .order("finished_at", desc=True)
                .execute()
                .data
                or []
            )
        except Exception:
            self.logger.exception("Failed reading github_projects for %s", user_uuid)
            return []

    def get_setting(self, key, default=None):
        """Read a server-wide bot setting (bot_settings key/value). Returns the
        stored string value, or `default` if unset / on error."""
        try:
            data = (
                self.supabase.table("bot_settings")
                .select("value")
                .eq("key", key)
                .limit(1)
                .execute()
                .data
            )
            if data and data[0].get("value") is not None:
                return data[0]["value"]
            return default
        except Exception:
            self.logger.exception("Failed reading setting %s", key)
            return default

    def set_setting(self, key, value):
        """Upsert a server-wide bot setting. Pass value=None to clear it (defer to
        the env/default). Returns True on success."""
        try:
            self.supabase.table("bot_settings").upsert(
                {"key": key, "value": value}, on_conflict="key"
            ).execute()
            return True
        except Exception:
            self.logger.exception("Failed writing setting %s", key)
            return False

    _PERSONA_KEY = "silver_wolf_persona"

    def get_persona_override(self):
        """The persisted persona override: True (on), False (off), or None (defer
        to the SILVER_WOLF_PERSONA env default). Loaded at startup into memory."""
        val = self.get_setting(self._PERSONA_KEY)
        if val is None:
            return None
        return str(val).strip().lower() in ("1", "true", "on", "yes")

    def set_persona_override(self, enabled):
        """Persist the persona override. `enabled`=True/False forces it; None
        clears the override (back to the env default)."""
        if enabled is None:
            return self.set_setting(self._PERSONA_KEY, None)
        return self.set_setting(self._PERSONA_KEY, "on" if enabled else "off")

    def get_discord_name_for_user(self, user_uuid):
        """display_name (or username) for a users.id UUID — used to label the
        leaderboard. Returns None if unknown."""
        try:
            data = (
                self.supabase.table(self.users_table)
                .select("display_name,username")
                .eq("id", user_uuid)
                .limit(1)
                .execute()
                .data
            )
            if not data:
                return None
            return data[0].get("display_name") or data[0].get("username")
        except Exception:
            self.logger.exception("Failed resolving name for user %s", user_uuid)
            return None

    def get_discord_names_for_users(self, user_uuids):
        """Batch name resolver: {users.id -> display_name/username} in ONE query.
        Used by the leaderboard so it doesn't fire N serial round-trips."""
        ids = [u for u in (user_uuids or []) if u]
        if not ids:
            return {}
        try:
            rows = (
                self.supabase.table(self.users_table)
                .select("id,display_name,username")
                .in_("id", ids)
                .execute()
                .data
                or []
            )
            return {
                r["id"]: (r.get("display_name") or r.get("username"))
                for r in rows
            }
        except Exception:
            self.logger.exception("Failed batch-resolving user names")
            return {}

    def add_subscription(self, user_uuid, discord_id, category=None, keyword=None,
                         smart=False):
        """Create an alert subscription. Returns the row, or None on error.
        (category, keyword) both None means 'every new job'. smart=True flags a
        résumé-match subscription (the DM hook AI-scores new jobs against the
        user's résumé and DMs only strong fits)."""
        try:
            row = {
                "user_id": user_uuid,
                "discord_id": discord_id,
                "category": category,
                "keyword": (keyword or None),
                "smart": bool(smart),
            }
            resp = (
                self.supabase.table("subscriptions")
                .upsert(row, on_conflict="user_id,category,keyword")
                .execute()
            )
            return resp.data[0] if resp.data else None
        except Exception:
            self.logger.exception("Failed adding subscription for %s", user_uuid)
            return None

    def get_smart_subscribers(self):
        """Subscriptions flagged smart (résumé-match alerts). Small table."""
        try:
            return (
                self.supabase.table("subscriptions")
                .select("*")
                .eq("smart", True)
                .execute()
                .data
                or []
            )
        except Exception:
            self.logger.exception("Failed fetching smart subscribers")
            return []

    def was_smart_alert_sent(self, user_uuid, job_table, job_id):
        """True if this (user, job) already got a smart alert — dedup across
        cycles so we never DM the same role twice."""
        try:
            data = (
                self.supabase.table("smart_alerts_sent")
                .select("id")
                .eq("user_id", user_uuid)
                .eq("job_table", job_table)
                .eq("job_id", int(job_id))
                .limit(1)
                .execute()
                .data
            )
            return bool(data)
        except Exception:
            self.logger.exception("Failed checking smart-alert dedup")
            return False  # On error, prefer to allow the send over silent misses.

    def mark_smart_alert_sent(self, user_uuid, job_table, job_id, score=None):
        """Record that a smart alert was DM'd for (user, job) so it isn't repeated."""
        try:
            self.supabase.table("smart_alerts_sent").upsert(
                {
                    "user_id": user_uuid,
                    "job_table": job_table,
                    "job_id": int(job_id),
                    "score": score,
                },
                on_conflict="user_id,job_table,job_id",
            ).execute()
        except Exception:
            self.logger.exception("Failed recording smart-alert send")

    # --- LeetCode solve tracking (grind channel streaks) ------------------
    def mark_leetcode_solved(
        self, user_uuid, problem_slug, difficulty=None,
        status="solved", topics=None, review_at=None,
    ):
        """Record an attempt. Idempotent on (user, slug). To preserve history on a
        re-react, an EXISTING row is not blindly overwritten:
          - review_at is set only on FIRST insert — a re-react won't reset the
            spaced-repetition clock (which would defeat /review), unless the caller
            passes a review_at AND the row has none.
          - a 'struggled'/'failed' status is NOT downgraded to 'solved' by a plain
            re-react — the weak-spot signal survives. An explicit non-solved status
            always writes through.
        `status` is solved/struggled/failed; `topics` is a comma-joined tag list."""
        try:
            existing = (
                self.supabase.table("leetcode_solves")
                .select("status,review_at")
                .eq("user_id", user_uuid)
                .eq("problem_slug", problem_slug)
                .limit(1)
                .execute()
                .data
            )
            prior = existing[0] if existing else None

            row = {
                "user_id": user_uuid,
                "problem_slug": problem_slug,
                "difficulty": difficulty,
                "status": status,
            }
            if prior:
                # Don't downgrade a recorded struggle to 'solved' on a re-react.
                if status == "solved" and prior.get("status") in ("struggled", "failed"):
                    row["status"] = prior["status"]
                # Preserve an already-scheduled review; only fill if none exists.
                if prior.get("review_at"):
                    row["review_at"] = prior["review_at"]
                elif review_at is not None:
                    row["review_at"] = review_at
            elif review_at is not None:
                row["review_at"] = review_at
            if topics is not None:
                row["topics"] = topics

            self.supabase.table("leetcode_solves").upsert(
                row, on_conflict="user_id,problem_slug"
            ).execute()
            return True
        except Exception:
            self.logger.exception("Failed recording leetcode solve")
            return False

    def unmark_leetcode_solved(self, user_uuid, problem_slug):
        """Remove a solve record (user un-reacted). Returns True on success."""
        try:
            self.supabase.table("leetcode_solves").delete().eq(
                "user_id", user_uuid
            ).eq("problem_slug", problem_slug).execute()
            return True
        except Exception:
            self.logger.exception("Failed removing leetcode solve")
            return False

    def get_leetcode_solves(self, user_uuid):
        """All of a user's attempt rows, newest first. Streak/count/weakspots are
        derived from these by the command layer."""
        try:
            return (
                self.supabase.table("leetcode_solves")
                .select("problem_slug,difficulty,status,topics,solved_at,review_at")
                .eq("user_id", user_uuid)
                .order("solved_at", desc=True)
                .execute()
                .data
                or []
            )
        except Exception:
            self.logger.exception("Failed fetching leetcode solves for %s", user_uuid)
            return []

    def get_leetcode_due_reviews(self, user_uuid, now_iso):
        """Attempts whose spaced-repetition review_at is due (<= now). Oldest-due
        first. Powers /review."""
        try:
            return (
                self.supabase.table("leetcode_solves")
                .select("problem_slug,difficulty,status,topics,review_at")
                .eq("user_id", user_uuid)
                .not_.is_("review_at", "null")
                .lte("review_at", now_iso)
                .order("review_at", desc=False)
                .execute()
                .data
                or []
            )
        except Exception:
            self.logger.exception("Failed fetching due reviews for %s", user_uuid)
            return []

    def get_leetcode_leaderboard(self, since_iso=None, limit=15):
        """Top solvers by solved count. `since_iso` restricts to recent attempts
        (weekly/daily boards); None = all-time. Returns [{user_id, solves}]. The
        command layer resolves user_id → display name. Counts only status='solved'.
        Aggregation is done client-side (small table; PostgREST lacks GROUP BY)."""
        try:
            q = (
                self.supabase.table("leetcode_solves")
                .select("user_id,solved_at,status")
                .eq("status", "solved")
            )
            if since_iso:
                q = q.gte("solved_at", since_iso)
            rows = q.execute().data or []
            counts = {}
            for r in rows:
                counts[r["user_id"]] = counts.get(r["user_id"], 0) + 1
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
            return [{"user_id": u, "solves": n} for u, n in ranked]
        except Exception:
            self.logger.exception("Failed building leetcode leaderboard")
            return []

    # --- DS&A learning progress (roadmap) ---------------------------------
    def mark_pattern_learned(self, user_uuid, pattern_key):
        """Record that a user learned a roadmap pattern. Idempotent on
        (user, pattern). Returns True on success."""
        try:
            self.supabase.table("leetcode_learned").upsert(
                {"user_id": user_uuid, "pattern_key": pattern_key},
                on_conflict="user_id,pattern_key",
            ).execute()
            return True
        except Exception:
            self.logger.exception("Failed recording learned pattern")
            return False

    def get_learned_patterns(self, user_uuid):
        """List of pattern_keys the user has learned (for /roadmap progress and the
        'learn next' suggestion)."""
        try:
            rows = (
                self.supabase.table("leetcode_learned")
                .select("pattern_key")
                .eq("user_id", user_uuid)
                .execute()
                .data
                or []
            )
            return [r["pattern_key"] for r in rows]
        except Exception:
            self.logger.exception("Failed fetching learned patterns for %s", user_uuid)
            return []

    def get_or_seed_roadmap_sequence(self, user_uuid, default_sequence):
        """Return the user's personalized roadmap study ORDER (list of pattern
        keys). On first call the row doesn't exist, so seed it with
        `default_sequence` (the roadmap's topological order) and return that.
        'Done' is tracked separately in leetcode_learned — this is only the ORDER.
        Falls back to `default_sequence` (unpersisted) on any DB error."""
        if not user_uuid:
            return list(default_sequence or [])
        try:
            rows = (
                self.supabase.table("leetcode_roadmap_queue")
                .select("sequence")
                .eq("user_id", user_uuid)
                .limit(1)
                .execute()
                .data
                or []
            )
            if rows and rows[0].get("sequence"):
                return list(rows[0]["sequence"])
            # Seed on first view.
            seq = list(default_sequence or [])
            self.supabase.table("leetcode_roadmap_queue").upsert(
                {"user_id": user_uuid, "sequence": seq}, on_conflict="user_id"
            ).execute()
            return seq
        except Exception:
            self.logger.exception(
                "Failed get/seed roadmap sequence for %s", user_uuid
            )
            return list(default_sequence or [])

    def set_roadmap_sequence(self, user_uuid, sequence):
        """Overwrite the user's roadmap study order (e.g. if you ever let them
        reorder). Returns True on success."""
        if not user_uuid:
            return False
        try:
            self.supabase.table("leetcode_roadmap_queue").upsert(
                {"user_id": user_uuid, "sequence": list(sequence or [])},
                on_conflict="user_id",
            ).execute()
            return True
        except Exception:
            self.logger.exception("Failed setting roadmap sequence for %s", user_uuid)
            return False

    def was_daily_posted(self, problem_date):
        """True if the daily LeetCode post for this calendar date is already
        recorded — the gate that stops double-posting across the scheduled loop
        and startup catch-up. `problem_date` is a 'YYYY-MM-DD' string."""
        try:
            data = (
                self.supabase.table("leetcode_daily_posts")
                .select("id")
                .eq("problem_date", problem_date)
                .limit(1)
                .execute()
                .data
            )
            return bool(data)
        except Exception:
            self.logger.exception("Failed checking daily-post gate")
            # On error, prefer NOT posting over spamming a duplicate.
            return True

    def mark_daily_posted(self, problem_date, problem_slug, message_id=None):
        """Record that the daily for `problem_date` was posted. Idempotent on
        problem_date so a race can't create two rows for one day."""
        try:
            self.supabase.table("leetcode_daily_posts").upsert(
                {
                    "problem_date": problem_date,
                    "problem_slug": problem_slug,
                    "message_id": int(message_id) if message_id else None,
                },
                on_conflict="problem_date",
            ).execute()
            return True
        except Exception:
            self.logger.exception("Failed recording daily post")
            return False

    def was_announcement_sent(self, version):
        """True if the announcement for this changelog version was already posted —
        the gate that stops the startup auto-announce from re-posting on every
        restart. `version` is a content hash of the changelog."""
        try:
            data = (
                self.supabase.table("sent_announcements")
                .select("id")
                .eq("version", version)
                .limit(1)
                .execute()
                .data
            )
            return bool(data)
        except Exception:
            self.logger.exception("Failed checking announcement gate")
            # On error, prefer NOT posting over spamming a duplicate announcement.
            return True

    def mark_announcement_sent(self, version, message_id=None):
        """Record that the announcement for `version` was posted. Idempotent on
        version so a race can't create two rows / two posts for one changelog."""
        try:
            self.supabase.table("sent_announcements").upsert(
                {
                    "version": version,
                    "message_id": int(message_id) if message_id else None,
                },
                on_conflict="version",
            ).execute()
            return True
        except Exception:
            self.logger.exception("Failed recording announcement send")
            return False

    def get_subscriptions(self, user_uuid):
        try:
            return (
                self.supabase.table("subscriptions")
                .select("*")
                .eq("user_id", user_uuid)
                .order("created_at", desc=False)
                .execute()
                .data
                or []
            )
        except Exception:
            self.logger.exception("Failed fetching subscriptions for %s", user_uuid)
            return []

    def delete_subscription(self, sub_id):
        try:
            self.supabase.table("subscriptions").delete().eq("id", sub_id).execute()
            return True
        except Exception:
            self.logger.exception("Failed deleting subscription %s", sub_id)
            return False

    def get_all_subscriptions(self):
        """Every subscription (for the DM-on-new-job hook). Small table."""
        try:
            return (
                self.supabase.table("subscriptions").select("*").execute().data or []
            )
        except Exception:
            self.logger.exception("Failed fetching all subscriptions")
            return []

    def get_saved_jobs(self, user_uuid):
        """The user's saved postings, newest-saved first, each as a full job
        row (joined with company_info) plus a _job_table marker. Fetches the
        saved_jobs rows, then the job rows per table in one query each."""
        try:
            saves = (
                self.supabase.table(self.saved_jobs_table)
                .select("job_table,job_id,saved_at")
                .eq("user_id", user_uuid)
                .order("saved_at", desc=True)
                .execute()
                .data
                or []
            )
        except Exception:
            self.logger.exception("Failed fetching saved_jobs for %s", user_uuid)
            return []

        if not saves:
            return []

        # Group the wanted ids by table, then one select per table.
        by_table = {}
        order = []  # preserve saved_at order
        for s in saves:
            by_table.setdefault(s["job_table"], []).append(s["job_id"])
            order.append((s["job_table"], s["job_id"]))

        rows_by_key = {}
        for table, ids in by_table.items():
            try:
                data = (
                    self.supabase.table(table)
                    .select("*, company_info(*)")
                    .in_("id", ids)
                    .execute()
                    .data
                    or []
                )
                for row in data:
                    row["_job_table"] = table
                    rows_by_key[(table, row["id"])] = row
            except Exception:
                self.logger.exception("Failed fetching saved job rows from %s", table)

        # Return in saved_at order, skipping any that no longer exist.
        return [rows_by_key[key] for key in order if key in rows_by_key]

    def toggle_saved_job(self, user_uuid, job_table, job_id):
        """Save the job if not saved, unsave it if already saved. Returns True
        when it ends up saved, False when unsaved, None on error."""
        try:
            existing = (
                self.supabase.table(self.saved_jobs_table)
                .select("id")
                .eq("user_id", user_uuid)
                .eq("job_table", job_table)
                .eq("job_id", job_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                self.supabase.table(self.saved_jobs_table).delete().eq(
                    "id", existing.data[0]["id"]
                ).execute()
                return False

            self.supabase.table(self.saved_jobs_table).insert(
                {"user_id": user_uuid, "job_table": job_table, "job_id": job_id}
            ).execute()
            return True
        except Exception:
            self.logger.exception(
                "Failed toggling saved job %s/%s for %s", job_table, job_id, user_uuid
            )
            return None

    @staticmethod
    def _normalize_title(title):
        """Collapse the variants aggregators put on the same role so they
        dedup to one key: strip trailing parentheticals/brackets ('(Fall
        2026)', '[Remote]'), unify dash characters, drop a trailing 'Team NN',
        a leading/trailing standalone year ('2026'), and common intern/eng
        wording variants, then squeeze whitespace. Only trailing (…) is removed
        — a leading or mid-title paren is kept, so 'Intern (AI) Backend' stays
        distinct."""
        text = (title or "").lower()
        # Repeatedly peel a trailing (...) or [...] group.
        while True:
            stripped = re.sub(r"\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$", "", text).strip()
            if stripped == text:
                break
            text = stripped
        text = text.replace("–", "-").replace("—", "-")  # en/em dash -> hyphen
        text = re.sub(r"\s*-?\s*team\s+\d+\s*$", "", text)  # trailing 'Team 01'
        # A standalone year anywhere ('2026 Software Engineer', 'SWE - 2026').
        text = re.sub(r"\b20\d{2}\b", " ", text)
        # Wording variants that mean the same role across aggregators.
        text = re.sub(r"\bsoftware engineering\b", "software engineer", text)
        text = re.sub(r"\bswe\b", "software engineer", text)
        text = re.sub(r"\bco-?op\b", "intern", text)
        text = re.sub(r"\bentry[- ]level\b", "", text)
        text = re.sub(r"[/\-,]", " ", text)  # unify separators before squeeze
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # Company legal-suffix / descriptor noise that varies by aggregator.
    _COMPANY_NOISE_RE = re.compile(
        r"\b(inc|llc|corp|corporation|ltd|limited|co|company|technologies|"
        r"technology|solutions|labs|group|holdings|systems)\b"
    )
    # Location strings that carry NO city signal — treat as unknown (wildcard).
    _LOC_STOPWORDS = frozenset(
        {"us", "usa", "united states", "united states of america", "canada",
         "remote", "n/a", "various", "multiple locations", ""}
    )
    _CITY_ALIAS = {
        "new york city": "new york", "nyc": "new york", "sf": "san francisco",
        "san fran": "san francisco", "d.c.": "washington", "dc": "washington",
        "washington d.c.": "washington",
    }
    _US_STATE_NAMES = frozenset({
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming",
    })

    @classmethod
    def _normalize_company(cls, company):
        """Lowercased company with legal suffixes and generic descriptors
        stripped so 'Google', 'Google LLC', 'Google Inc.' collapse to one key."""
        text = (company or "").strip().lower()
        text = re.sub(r"[.,]", " ", text)
        text = cls._COMPANY_NOISE_RE.sub(" ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _normalize_location(cls, location):
        """Canonical city token, or '' when the string carries no city signal.

        Handles the aggregators' divergent formats: 'City, ST, United States',
        a bare 'United States'/'Canada', a 'State - City' form (Salesforce), a
        raw street address, and 'City (Hybrid)'. Returns '' for country-only /
        remote / street-only strings so a role with an unknown location folds
        into the SAME role that has a real city (see _dedup — blank is a
        wildcard), instead of splitting one posting into two."""
        s = (location or "").strip()
        # 'State - City' with no comma (e.g. 'California - San Francisco').
        if " - " in s and "," not in s:
            parts = [p.strip() for p in s.split(" - ")]
            picked = next(
                (p for p in parts if p.lower() not in cls._US_STATE_NAMES), None
            )
            if picked:
                s = picked
        first = s.split(",")[0].strip()
        first = re.sub(r"\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$", "", first)
        first = re.sub(
            r"\s+(office|hq|headquarters|remote|hybrid|onsite|on-site)\s*$",
            "", first, flags=re.IGNORECASE,
        )
        first = first.strip().lower()
        # A street address ('600 march road') carries no comparable city token.
        if re.match(r"^\d", first):
            return ""
        first = re.sub(r"\s+", " ", first).strip()
        if first in cls._LOC_STOPWORDS:
            return ""
        return cls._CITY_ALIAS.get(first, first)

    @classmethod
    def _dedup_key(cls, row):
        """Identify a posting by normalized company + title + location, NOT by
        URL. The same job appears under different aggregator URLs (simplify,
        jobright, the raw ATS link), so URL dedup posts a role multiple times.
        All three parts are normalized so legal-suffix / seasonal / office /
        work-mode / wording variants don't split one role into several keys,
        while genuinely different postings (Optiver Austin vs Chicago) stay
        distinct. Location can be '' (unknown) — _insert treats that as a
        wildcard against a same company+title row that DOES have a city."""
        company = cls._normalize_company(row.get("company_name"))
        title = cls._normalize_title(row.get("job_title"))
        location = cls._normalize_location(row.get("job_location"))
        return (company, title, location)

    def insert_internships(self, internships, table=None):
        table = table or self.internships_table
        try:
            # Drop any internal helper fields (leading underscore) that would
            # be rejected as non-existent columns by Supabase.
            internships = [
                {k: v for k, v in row.items() if not k.startswith("_")}
                for row in internships
            ]

            # US-only filter: drop roles clearly located outside the US before
            # they're ever inserted (this is a US-focused bot). Ambiguous/blank
            # locations are KEPT (is_us_location errs toward keeping).
            before_us = len(internships)
            internships = [r for r in internships if is_us_location(r.get("job_location"))]
            dropped = before_us - len(internships)
            if dropped:
                self.logger.info(
                    "US filter: dropped %d non-US role(s) for %s", dropped, table
                )

            existing = self.get_existing_internships(table)
            existing_keys = {self._dedup_key(item) for item in existing}
            # (company, title) pairs that already have a KNOWN city — so a new
            # row with an unknown/blank location (a wildcard) is recognized as
            # the same role and skipped, instead of posting a second time.
            existing_located = {
                (k[0], k[1]) for k in existing_keys if k[2]
            }

            # Dedup within this scrape too (a single feed can list the same
            # role several times under different URLs / location formats).
            new_internships = []
            seen = set(existing_keys)
            seen_located = set(existing_located)
            for internship in internships:
                key = self._dedup_key(internship)
                if key in seen:
                    continue
                company_title = (key[0], key[1])
                # Blank-location wildcard: this role with no city == the same
                # role already seen WITH a city. Don't post the vaguer copy.
                if not key[2] and company_title in seen_located:
                    continue
                seen.add(key)
                if key[2]:
                    seen_located.add(company_title)
                new_internships.append(internship)

            self.logger.info(
                "Found %d new rows out of %d scraped for %s",
                len(new_internships),
                len(internships),
                table,
            )

            if not new_internships:
                self.logger.info("No new rows to insert into %s", table)
                return []

            # Use upsert with ignore_duplicates so a single row that collides with
            # the table's UNIQUE(company_name, job_title, job_url) constraint no
            # longer aborts the WHOLE batch. The app-side _dedup_key normalizes
            # title/location and ignores URL, so a role already in the DB under a
            # different location or a rotated utm URL can still slip past the app
            # filter and hit the raw DB constraint. A plain .insert() is
            # all-or-nothing → one dup threw the entire insert, commit time never
            # advanced, and the same rows failed every cycle (new grads stalled).
            # ignore_duplicates inserts the genuinely-new rows and skips the dups.
            response = (
                self.supabase.table(table)
                .upsert(
                    new_internships,
                    on_conflict="company_name,job_title,job_url",
                    ignore_duplicates=True,
                )
                .execute()
            )

            self.logger.info(
                "Inserted %d new rows into %s (dups skipped)",
                len(response.data), table,
            )

            return response.data
        except Exception:
            self.logger.exception("Failed inserting into %s", table)
            return None

    def insert_companies(self, companies):
        if not companies:
            self.logger.info("No new companies to insert")
            return []

        try:
            # Upsert on the company_name key: new rows insert, existing rows
            # refresh their website/domain/linkedin/logo (heals stale/bare
            # rows from older versions)
            response = (
                self.supabase.table(self.companies_table)
                .upsert(companies, on_conflict="company_name")
                .execute()
            )

            self.logger.info("Upserted %d company rows", len(response.data))

            return response.data
        except Exception:
            self.logger.exception("Failed inserting companies")
            return None

    def get_unsent_internships(self, table=None, limit=None):
        """Unsent rows (joined with company_info), oldest-posted first. `limit`
        caps the fetch SERVER-SIDE — critical on the 512 MB Render box: without
        it a backlog (e.g. 600+ new grads) pulls every row + its join into RAM at
        once and OOMs. The poster only handles MAX_POSTS_PER_CYCLE per cycle
        anyway, so fetching more is pure waste; the rest come next cycle."""
        table = table or self.internships_table
        try:
            query = (
                self.supabase.table(table)
                .select("*, company_info(*)")
                .eq("sent_to_discord", False)
                .order("job_posted_at", desc=False)
            )
            if limit is not None:
                query = query.limit(limit)
            response = query.execute()

            self.logger.info(
                "Found %d unsent rows in %s (limit=%s)",
                len(response.data), table, limit,
            )

            return response.data
        except Exception:
            self.logger.exception("Failed to fetch unsent rows from %s", table)
            return None

    def get_posted_internships(self, table=None, enriched_only=True, limit=None):
        """Rows already posted to Discord (have a discord_message_id), joined
        with company_info for the embed. Used by /refreshembeds to re-render
        old messages in place. enriched_only skips rows that gained no detail
        data (nothing new to show). limit caps the row count server-side."""
        table = table or self.internships_table
        try:
            query = (
                self.supabase.table(table)
                .select("*, company_info(*)")
                .not_.is_("discord_message_id", "null")
            )
            if enriched_only:
                query = query.not_.is_("job_summary", "null")
            query = query.order("job_posted_at", desc=False)
            if limit is not None:
                query = query.limit(limit)
            response = query.execute()
            self.logger.info(
                "Found %d posted rows in %s (enriched_only=%s)",
                len(response.data), table, enriched_only,
            )
            return response.data
        except Exception:
            self.logger.exception("Failed fetching posted rows from %s", table)
            return []

    def get_rows_to_recheck(self, table=None, limit=150):
        """Posted-and-open rows that have a detail page to check, oldest-checked
        first (NULLs — never checked — come first). The rolling closed-status
        sweep pulls a slice each cycle so no single run scans the whole table."""
        table = table or self.internships_table
        try:
            response = (
                self.supabase.table(table)
                .select("id,discord_message_id,detail_url")
                .not_.is_("discord_message_id", "null")
                .not_.is_("detail_url", "null")
                .eq("is_closed", False)
                .order("last_checked_at", desc=False, nullsfirst=True)
                .limit(limit)
                .execute()
            )
            return response.data or []
        except Exception:
            self.logger.exception("Failed fetching recheck rows from %s", table)
            return []

    def mark_checked(self, ids, table=None):
        """Stamp last_checked_at=now() on a batch of rows so they rotate to the
        back of the recheck queue."""
        if not ids:
            return
        table = table or self.internships_table
        try:
            self.supabase.table(table).update(
                {"last_checked_at": datetime.now(timezone.utc).isoformat()}
            ).in_("id", ids).execute()
        except Exception:
            self.logger.exception("Failed stamping last_checked_at on %s", table)

    def mark_closed(self, internship_id, table=None):
        """Flag a posting closed (found dead during the recheck sweep)."""
        table = table or self.internships_table
        try:
            self.supabase.table(table).update({"is_closed": True}).eq(
                "id", internship_id
            ).execute()
        except Exception:
            self.logger.exception("Failed marking %s %s closed", table, internship_id)

    def get_existing_internships(self, table=None):
        table = table or self.internships_table
        try:
            response = (
                self.supabase.table(table)
                .select("company_name,job_title,job_location,job_url")
                .execute()
            )

            return response.data

        except Exception:
            self.logger.exception("Failed fetching existing rows from %s", table)
            return []

    def get_repo_update_time(self, repo_name):
        try:
            response = (
                self.supabase.table(self.repo_table)
                .select("last_updated_at")
                .eq("source_repo", repo_name)
                .execute()
            )

            if (len(response.data) != 0):
                return response.data[0].get("last_updated_at")
            return None
        except Exception:
            self.logger.exception("Failed fetching repo last updated time")
            return None

    @staticmethod
    def _to_utc(value):
        """Parse a stored ISO string or a datetime into a tz-aware UTC
        datetime, so comparisons don't depend on string formatting."""
        if value is None:
            return None
        if isinstance(value, str):
            # Supabase returns e.g. '2026-07-23T00:36:04+00:00'
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def repo_has_new_commits(self, repo_name, pushed_at):
        """True if the repo's latest push is newer than what we last stored.
        Compares real datetimes (GitHub pushed_at vs stored value) rather
        than formatted strings, which sorted incorrectly."""
        stored = self.get_repo_update_time(repo_name)
        if stored is None:
            return True
        return self._to_utc(pushed_at) > self._to_utc(stored)

    def commit_scrape(self, repo_name, pushed_at, internships, companies, table):
        """Persist a scrape atomically-ish, in FK-safe order:
        1. upsert companies (internships FK-reference them)
        2. insert the internship/new-grad rows
        3. advance the repo's stored commit time ONLY if both succeeded, so a
           failed insert is retried next cycle instead of being skipped as
           'no new commits'.
        Returns True if the commit time was advanced."""
        # Companies must land first — the rows FK-reference company_info.
        # insert_companies returns None on failure; bail so we don't insert
        # internships that would trip the foreign key.
        if companies:
            if self.insert_companies(companies) is None:
                self.logger.error(
                    "Company upsert failed for %s — skipping row insert, "
                    "will retry next cycle",
                    repo_name,
                )
                return False

        if internships:
            if self.insert_internships(internships, table) is None:
                self.logger.error(
                    "Row insert failed for %s — not advancing commit time, "
                    "will retry next cycle",
                    repo_name,
                )
                return False

        self.insert_repo_update_time(repo_name, pushed_at.isoformat())
        return True

    def get_company_info(self, company_name):
        try:
            response = (
                self.supabase.table(self.companies_table)
                .select(
                    "company_name,company_website,company_domain,"
                    "company_linkedin,company_logo"
                )
                .eq("company_name", company_name)
                .execute()
            )
            self.logger.info("Found %d company info", len(response.data))
            if (len(response.data) != 0):
                return response.data[0]
            return None
        except Exception:
            self.logger.exception("Failed fetching company info")
            return None

    def get_existing_company_names(self):
        try:
            response = (
                self.supabase.table(self.companies_table)
                .select("company_name")
                .execute()
            )
            self.logger.info("Found %d existing company names", len(response.data))
            if (len(response.data) != 0):
                return [row["company_name"] for row in response.data]
            return []
        except Exception:
            self.logger.exception("Failed fetching existing company names")
            return []

    def mark_as_sent(self, internship_id, table=None):
        table = table or self.internships_table
        (
            self.supabase.table(table)
            .update({"sent_to_discord": True})
            .eq("id", internship_id)
            .execute()
        )

    # Columns details/ may fill in. Anything not in here is ignored, so a
    # row carrying joined company_info or other extras is safe to pass in.
    DETAIL_COLUMNS = (
        "job_summary",
        "job_responsibilities",
        "job_requirements",
        "job_preferred",
        "job_benefits",
        "job_tags",
        "comp_min",
        "comp_max",
        "salary_desc",
        "employment_type",
        "seniority",
        "work_model",
        "is_closed",
        "job_location",
    )

    def update_job_details(self, internship_id, details, table=None, replace=False):
        """Persist enrichment onto an existing row.

        Default: empty values are dropped, so a failed lookup never blanks out
        data the scrape already had.

        replace=True: write every detail column present in `details`, including
        empties — used by the force backfill so a field that's now legitimately
        empty (e.g. jobright Skills after the reroute) clears its stale value
        instead of persisting. Requires the enrichment actually succeeded
        (details carries a job_summary); the caller guards that."""
        table = table or self.internships_table

        if replace:
            payload = {
                key: details.get(key)
                for key in self.DETAIL_COLUMNS
                if key in details
            }
        else:
            payload = {
                key: value
                for key, value in details.items()
                if key in self.DETAIL_COLUMNS and value not in (None, "", [])
            }
        if not payload:
            return []

        try:
            response = (
                self.supabase.table(table)
                .update(payload)
                .eq("id", internship_id)
                .execute()
            )
            return response.data
        except Exception:
            self.logger.exception(
                "Failed updating details for %s %s", table, internship_id
            )
            return None

    def insert_repo_update_time(self, repo_name, last_updated_at):
        try:
            response = (
                self.supabase.table(self.repo_table)
                .upsert(
                    {
                        "source_repo": repo_name,
                        "last_updated_at": last_updated_at
                    },
                )
                .execute()
            )
            self.logger.info(
                "Updated repo %s last updated time to %s",
                repo_name,
                last_updated_at
            )
            return response.data
        except Exception:
            self.logger.exception("Failed updating repo last updated time")
            return

    def update_internship_message_id(self, internship_id, message_id, table=None):
        table = table or self.internships_table
        try:
            response = (
                self.supabase.table(table)
                .update({"discord_message_id": message_id})
                .eq("id", internship_id)
                .execute()
            )
            self.logger.info(
                "Updated %s %s message ID to %s", table, internship_id, message_id
            )
            return response.data
        except Exception:
            self.logger.exception(
                "Failed updating %s %s message ID", table, internship_id
            )
            return None


if __name__ == "__main__":
    database = SupabaseDatabase()
    print(database.get_existing_company_names())
