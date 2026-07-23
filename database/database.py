import os
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
            existing_keys = {
                (item["company_name"], item["job_title"], item["job_url"])
                for item in existing
            }

            new_internships = [
                internship
                for internship in internships
                if (
                    internship["company_name"],
                    internship["job_title"],
                    internship["job_url"],
                )
                not in existing_keys
            ]

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

    def get_existing_internships(self, table=None):
        table = table or self.internships_table
        try:
            response = (
                self.supabase.table(table)
                .select("company_name,job_title,job_url")
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
