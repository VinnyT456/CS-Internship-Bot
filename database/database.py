import os
import re
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client


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

    def add_subscription(self, user_uuid, discord_id, category=None, keyword=None):
        """Create an alert subscription. Returns the row, or None on error.
        (category, keyword) both None means 'every new job'."""
        try:
            row = {
                "user_id": user_uuid,
                "discord_id": discord_id,
                "category": category,
                "keyword": (keyword or None),
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
        and squeeze whitespace. Only trailing (…) is removed — a leading or
        mid-title paren is kept, so 'Intern (AI) Backend' stays distinct."""
        text = (title or "").lower()
        # Repeatedly peel a trailing (...) or [...] group.
        while True:
            stripped = re.sub(r"\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$", "", text).strip()
            if stripped == text:
                break
            text = stripped
        text = text.replace("–", "-").replace("—", "-")  # en/em dash -> hyphen
        text = re.sub(r"\s*-?\s*team\s+\d+\s*$", "", text)  # trailing 'Team 01'
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _normalize_location(location):
        """First segment, lowercased, with trailing office/work-mode noise
        dropped so 'Santa Clara Office' == 'Santa Clara' and
        'New York (Hybrid)' == 'New York'."""
        first = (location or "").split(",")[0]
        first = re.sub(r"\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$", "", first)
        first = re.sub(
            r"\s+(office|hq|headquarters|remote|hybrid|onsite|on-site)\s*$",
            "",
            first,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s+", " ", first).strip().lower()

    @classmethod
    def _dedup_key(cls, row):
        """Identify a posting by company + normalized title + normalized
        location, NOT by URL. The same job appears under different aggregator
        URLs (simplify.jobs, jobright.ai, the raw ATS link), so URL-based
        dedup posts the same role multiple times. Title and location are
        normalized (see helpers) so seasonal/office/work-mode suffixes don't
        split one role into several keys, while genuinely different postings
        (Optiver Austin vs Chicago) stay distinct."""
        company = (row.get("company_name") or "").strip().lower()
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

            existing = self.get_existing_internships(table)
            existing_keys = {self._dedup_key(item) for item in existing}

            # Dedup within this scrape too (a single feed can list the same
            # role several times under different URLs).
            new_internships = []
            seen = set(existing_keys)
            for internship in internships:
                key = self._dedup_key(internship)
                if key in seen:
                    continue
                seen.add(key)
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

            response = (
                self.supabase.table(table)
                .insert(new_internships)
                .execute()
            )

            self.logger.info("Inserted %d new rows into %s", len(response.data), table)

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

    def get_unsent_internships(self, table=None):
        table = table or self.internships_table
        try:
            response = (
                self.supabase.table(table)
                .select("""
                    *,
                    company_info(*)
                """)
                .eq("sent_to_discord", False)
                .order("job_posted_at", desc=False)
                .execute()
            )

            self.logger.info(
                "Found %d unsent rows in %s", len(response.data), table
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
