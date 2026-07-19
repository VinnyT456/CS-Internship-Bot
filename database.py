import os
import logging
from dotenv import load_dotenv
from supabase import create_client
from pprint import pprint


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
        self.companies_table = "company_info"
        self.repo_table = "repo_info"

    def insert_internships(self, internships):
        try:
            existing = self.get_existing_internships()
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
                "Found %d new internships out of %d scraped",
                len(new_internships),
                len(internships),
            )

            if not new_internships:
                self.logger.info("No new internships to insert")
                return []

            response = (
                self.supabase.table(self.internships_table)
                .insert(new_internships)
                .execute()
            )

            self.logger.info("Inserted %d new internships", len(response.data))

            return response.data
        except Exception:
            self.logger.exception("Failed inserting internships")
            return None

    def insert_companies(self, companies):
        if not companies:
            self.logger.info("No new companies to insert")
            return []

        try:
            # ignore_duplicates keeps existing rows untouched instead of
            # failing the whole batch on a primary-key collision
            response = (
                self.supabase.table(self.companies_table)
                .upsert(
                    companies,
                    on_conflict="company_name",
                    ignore_duplicates=True,
                )
                .execute()
            )

            self.logger.info("Inserted %d new companies info", len(response.data))

            return response.data
        except Exception:
            self.logger.exception("Failed inserting companies")
            return None

    def get_existing_company_names(self):
        try:
            response = (
                self.supabase.table(self.companies_table)
                .select("company_name")
                .execute()
            )

            return [row["company_name"] for row in response.data]
        except Exception:
            self.logger.exception("Failed fetching existing company names")
            return []

    def get_all_internships(self):
        try:
            self.logger.info("Fetching internships")

            response = (
                self.supabase.table(self.internships_table)
                .select("*")
                .order("id", desc=False)
                .execute()
            )

            self.logger.debug("Supabase returned %d row(s)", len(response.data))

            self.logger.info("Found %d internships", len(response.data))

            return response.data

        except Exception:
            self.logger.exception("Failed to fetch internships")
            return None

    def get_unsent_internships(self):
        try:
            response = (
                self.supabase.table(self.internships_table)
                .select("""
                    *,
                    company_info(*)
                """)
                .eq("sent_to_discord", False)
                .order("job_posted_at", desc=False)
                .execute()
            )

            self.logger.debug("Supabase returned %d unsent row(s)", len(response.data))

            self.logger.info("Found %d unsent internships", len(response.data))

            return response.data
        except Exception:
            self.logger.exception("Failed to fetch unsent internships")
            return None
        return response.data

    def delete_internship(self, internship_id):
        try:
            self.logger.info("Deleting internship %s", internship_id)

            response = (
                self.supabase.table(self.internships_table)
                .delete()
                .eq("id", internship_id)
                .execute()
            )

            self.logger.debug("Supabase returned %d row(s)", len(response.data))

            self.logger.info("Internship deleted successfully")

            return response.data

        except Exception:
            self.logger.exception("Failed to delete internship")
            return None

    def find_internship(self, query: str):
        try:
            self.logger.info("Searching internships for: %s", query)

            response = (
                self.supabase.table(self.internships_table)
                .select("*")
                .or_(f"company_name.ilike.%{query}%,job_title.ilike.%{query}%")
                .execute()
            )

            self.logger.debug(
                "Supabase returned %d matching row(s)", len(response.data)
            )

            self.logger.info("Found %d matching internships", len(response.data))

            return response.data

        except Exception:
            self.logger.exception("Failed to search internships")
            return None

    def get_existing_internships(self):
        try:
            response = (
                self.supabase.table(self.internships_table)
                .select("company_name,job_title,job_url")
                .execute()
            )

            return response.data

        except Exception:
            self.logger.exception("Failed fetching existing internships")
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

    def get_company_info(self, company_name):
        try:
            response = (
                self.supabase.table(self.companies_table)
                .select("""
                    company_name,
                    company_website,
                    company_domain,
                    company_linkedin,
                    company_logo,
                """)
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

    def mark_as_sent(self, internship_id):
        (
            self.supabase.table(self.internships_table)
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

    def update_internship_message_id(self, internship_id, message_id):
        try:
            response = (
                self.supabase.table(self.internships_table)
                .update(
                    {
                        "discord_message_id": message_id
                    }
                )
                .eq("id", internship_id)
                .execute()
            )
            self.logger.info("Updated internship %s message ID to %s", internship_id, message_id)
            return response.data
        except Exception:
            self.logger.exception("Failed updating internship %s message ID to %s", internship_id, message_id)
            return None

if __name__ == "__main__":
    database = SupabaseDatabase()
    print(database.get_repo_last_updated_time("vanshb03/Summer2027-Internships"))
