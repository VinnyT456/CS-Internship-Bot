import os
import logging
from dotenv import load_dotenv
from supabase import create_client


class SupabaseDatabase:

    def __init__(self):

        load_dotenv()

        self.logger = logging.getLogger("logs/database.log")
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            file_handler = logging.FileHandler(
                "logs/database.log",
                mode="a"
            )
            file_handler.setLevel(logging.DEBUG)

            formatter = logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s"
            )

            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

        self.supabase = create_client(
            supabase_url=os.getenv("SUPABASE_URL"),
            supabase_key=os.getenv("SUPABASE_KEY")
        )

        self.supabase_table = "internships"

    def insert_internships(self, internships):
        try:
            existing = self.get_existing_internships()
            existing_keys = {
                (
                    item["company_name"],
                    item["job_title"],
                    item["job_url"]
                )
                for item in existing
            }

            new_internships = [
                internship
                for internship in internships
                if (
                    internship["company_name"],
                    internship["job_title"],
                    internship["job_url"]
                )
                not in existing_keys
            ]

            self.logger.info(
                "Found %d new internships out of %d scraped",
                len(new_internships),
                len(internships)
            )

            if not new_internships:
                self.logger.info(
                    "No new internships to insert"
                )
                return []

            response = (
                self.supabase
                .table(self.supabase_table)
                .insert(new_internships)
                .execute()
            )

            self.logger.info(
                "Inserted %d new internships",
                len(response.data)
            )

            return response.data
        except Exception:
            self.logger.exception(
                "Failed inserting internships"
            )
            return None

        except Exception:
            self.logger.exception("Failed to insert internships")
            return None

    def get_all_internships(self):
        try:
            self.logger.info("Fetching internships")

            response = (
                self.supabase.table(self.supabase_table)
                .select("*")
                .order("id", desc=False)
                .execute()
            )

            self.logger.debug(
                "Supabase returned %d row(s)",
                len(response.data)
            )

            self.logger.info(
                "Found %d internships",
                len(response.data)
            )

            return response.data

        except Exception:
            self.logger.exception("Failed to fetch internships")
            return None

    def get_unsent_internships(self):
        try:
            response = (
                self.supabase.table(self.supabase_table)
                .select("*")
                .eq("sent_to_discord", False)
                .limit(1)
                .order("job_posted_at", desc=False)
                .execute()
            )

            self.logger.debug(
                "Supabase returned %d unsent row(s)",
                len(response.data)
            )

            self.logger.info(
                "Found %d unsent internships",
                len(response.data)
            )

            return response.data
        except Exception:
            self.logger.exception("Failed to fetch unsent internships")
            return None
        return response.data

    def delete_internship(self, internship_id):
        try:
            self.logger.info(
                "Deleting internship %s",
                internship_id
            )

            response = (
                self.supabase.table(self.supabase_table)
                .delete()
                .eq("id", internship_id)
                .execute()
            )

            self.logger.debug(
                "Supabase returned %d row(s)",
                len(response.data)
            )

            self.logger.info("Internship deleted successfully")

            return response.data

        except Exception:
            self.logger.exception("Failed to delete internship")
            return None

    def find_internship(self, query: str):
        try:
            self.logger.info(
                "Searching internships for: %s",
                query
            )

            response = (
                self.supabase.table(self.supabase_table)
                .select("*")
                .or_(
                    f"company_name.ilike.%{query}%,"
                    f"job_title.ilike.%{query}%"
                )
                .execute()
            )

            self.logger.debug(
                "Supabase returned %d matching row(s)",
                len(response.data)
            )

            self.logger.info(
                "Found %d matching internships",
                len(response.data)
            )

            return response.data

        except Exception:
            self.logger.exception("Failed to search internships")
            return None

    def get_existing_internships(self):
        try:
            response = (
                self.supabase
                .table(self.supabase_table)
                .select(
                    "company_name,job_title,job_url"
                )
                .execute()
            )

            return response.data

        except Exception:
            self.logger.exception(
                "Failed fetching existing internships"
            )
            return []

    def mark_as_sent(self, internship_id):
        (
            self.supabase.table(self.supabase_table)
            .update({"sent_to_discord": True})
            .eq("id", internship_id)
            .execute()
        )

if __name__ == "__main__":
    database = SupabaseDatabase()
    print(database.get_all_internships())