import logging
import re
from urllib.parse import quote, urlparse


class CompanySearch:
    # Companies whose domain isn't derivable from the name. Offline lookup,
    # keyed on the normalized (lowercase, alphanumeric-only) name.
    DOMAIN_ALIASES = {
        "susquehannainvestmentgroup": "sig.com",
        "susquehannainternationalgroup": "sig.com",
        "deshaw": "deshaw.com",
        "thedeshawgroup": "deshaw.com",
        "jpmorgan": "jpmorganchase.com",
        "jpmorganchase": "jpmorganchase.com",
        "jpmorganchaseco": "jpmorganchase.com",
        "goldmansachs": "goldmansachs.com",
        "boozallenhamilton": "boozallen.com",
        "boozallen": "boozallen.com",
        "towerresearchcapital": "tower-research.com",
        "towerresearch": "tower-research.com",
        "jumptradinggroup": "jumptrading.com",
        "jumptrading": "jumptrading.com",
        "rakuteninternational": "rakuten.com",
        "meta": "meta.com",
        "facebook": "meta.com",
        "alphabet": "abc.xyz",
        "x": "x.com",
        "twitter": "x.com",
        "waymo": "waymo.com",
        "boeing": "boeing.com",
        "theboeingcompany": "boeing.com",
    }

    # Legal / descriptive suffix words to strip before guessing a domain.
    SUFFIX_WORDS = {
        "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
        "co", "company", "group", "holdings", "technologies", "technology",
        "labs", "international", "global", "worldwide", "partners", "capital",
    }

    def __init__(self):
        self.logger = logging.getLogger("company_search")

    @staticmethod
    def _careers_url(company):
        return f"https://www.google.com/search?q={quote(company + ' careers')}"

    @staticmethod
    def _linkedin_url(company):
        return f"https://www.google.com/search?q={quote(company + ' linkedin')}"

    @staticmethod
    def _root_domain(value):
        """Normalize a full URL or bare host to its root domain:
        'https://docs.databricks.com/x' -> 'databricks.com',
        'careers.stripe.co.uk' -> 'stripe.co.uk'."""
        value = value.strip().lower()

        # urlparse only finds netloc when a scheme is present; add one if the
        # caller passed a bare host ('careers.stripe.com')
        parsed = urlparse(value if "//" in value else f"//{value}")
        host = (parsed.netloc or parsed.path).replace("www.", "")
        host = host.split("/")[0].split(":")[0]  # strip any path/port remnant

        parts = host.split(".")
        if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac", "gov"):
            return ".".join(parts[-3:])
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return host

    def _guess_domain(self, company):
        # No scraper-provided domain. 1) known aliases, 2) name with legal
        # suffixes/leading 'the' stripped -> '.com'.
        name = re.sub(r"\(.*?\)", "", company).lower()

        # Exact alias on the fully-normalized name
        core = re.sub(r"[^a-z0-9]", "", name)
        if not core:
            return None
        if core in self.DOMAIN_ALIASES:
            return self.DOMAIN_ALIASES[core]

        # Drop leading 'the' and trailing legal/descriptive words, keeping the
        # distinctive part: 'Uber Technologies, Inc.' -> 'uber',
        # 'The Boeing Company' -> 'boeing'
        words = re.findall(r"[a-z0-9]+", name)
        if words and words[0] == "the" and len(words) > 1:
            words = words[1:]
        while len(words) > 1 and words[-1] in self.SUFFIX_WORDS:
            words.pop()

        stripped = "".join(words)

        # Re-check aliases against the stripped form too
        if stripped in self.DOMAIN_ALIASES:
            return self.DOMAIN_ALIASES[stripped]

        return f"{stripped or core}.com"

    @staticmethod
    def _logo(domain):
        if not domain:
            return None
        return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

    def get_company_info(self, company, known_domain=None):
        if known_domain:
            domain = self._root_domain(known_domain)
        else:
            domain = self._guess_domain(company)

        return {
            "company_name": company,
            "company_website": self._careers_url(company),
            "company_domain": domain,
            "company_linkedin": self._linkedin_url(company),
            "company_logo": self._logo(domain),
        }


if __name__ == "__main__":
    import sys

    company = " ".join(sys.argv[1:]) or "Stripe"
    info = CompanySearch().get_company_info(company)
    for key, value in info.items():
        print(f"{key:18}: {value}")
