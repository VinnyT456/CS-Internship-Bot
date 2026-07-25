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

    def update_job_details(self, internship_id, details, table=None):
        """Persist enrichment onto an existing row. Empty values are dropped
        so a failed lookup never blanks out data the scrape already had."""
        table = table or self.internships_table

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
