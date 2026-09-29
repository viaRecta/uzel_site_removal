"""Site category, own-account detection and the suggested values a human reviews.

Every string returned here that goes into a dropdown column must exist on the Lists sheet;
excel_io validates that before writing.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .analyze import (
    DT_ADDRESS,
    DT_DOB,
    DT_EMAIL,
    DT_EMPLOYER,
    DT_IBAN,
    DT_PHONE,
    DT_PHOTO,
    DT_RELATIVES,
    DT_SCHOOL,
    DT_TCKN,
    PLACE_COMMENTS,
    PageAnalysis,
)
from .sites import BROKER_CATEGORY, SiteDirectory
from .textnorm import fold
from .urls import domain_matches, hostname

CAT_SOCIAL = "Social media"
CAT_PRO = "Professional network"
CAT_MEDIA = "Image / video host"
CAT_FORUM = "Forum / community"
CAT_BLOG = "Blog"
CAT_DIRECTORY = "Business directory"
CAT_ARCHIVE = "Archive / cache"
CAT_OTHER = "Other"

# Categories where a URL segment equal to a username means "the client's own account".
ACCOUNT_CATEGORIES = {CAT_SOCIAL, CAT_PRO, CAT_MEDIA, CAT_BLOG, CAT_FORUM}

METHOD_OWN = "Client deletes own account"
METHOD_OPT_OUT = "Site opt-out form"
METHOD_KVKK = "KVKK request (Law 6698)"
METHOD_GDPR = "GDPR Art. 17 erasure request"
METHOD_DIRECT = "Direct request to site owner"
FALLBACK_GOOGLE = "Google 'Results about you' / personal info form"

EU_EEA = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
    "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO",
}
# ccTLDs that differ from the ISO code.
_CCTLD_ALIASES = {"uk": "GB", "eu": "EU"}

COUNTRY_NAMES = {
    "TR": "Turkey", "US": "USA", "GB": "United Kingdom", "DE": "Germany", "FR": "France",
    "NL": "Netherlands", "IE": "Ireland", "IT": "Italy", "ES": "Spain", "SE": "Sweden",
    "FI": "Finland", "DK": "Denmark", "NO": "Norway", "IS": "Iceland", "BE": "Belgium",
    "AT": "Austria", "CH": "Switzerland", "PL": "Poland", "CZ": "Czechia", "SK": "Slovakia",
    "HU": "Hungary", "RO": "Romania", "BG": "Bulgaria", "GR": "Greece", "CY": "Cyprus",
    "PT": "Portugal", "LU": "Luxembourg", "LT": "Lithuania", "LV": "Latvia", "EE": "Estonia",
    "SI": "Slovenia", "HR": "Croatia", "MT": "Malta", "LI": "Liechtenstein", "CA": "Canada",
    "AU": "Australia", "NZ": "New Zealand", "JP": "Japan", "CN": "China", "HK": "Hong Kong",
    "SG": "Singapore", "IN": "India", "RU": "Russia", "UA": "Ukraine", "IL": "Israel",
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "AZ": "Azerbaijan", "KZ": "Kazakhstan",
    "BR": "Brazil", "MX": "Mexico", "ZA": "South Africa", "KR": "South Korea", "TW": "Taiwan",
    "PA": "Panama", "VG": "British Virgin Islands", "SC": "Seychelles", "BZ": "Belize",
}


def country_name(code: str) -> str:
    code = (code or "").upper()
    return COUNTRY_NAMES.get(code, code)


def classify_category(url: str, sites: SiteDirectory) -> str:
    host = hostname(url)
    if sites.broker_for(host):
        return BROKER_CATEGORY
    for cat, domains in sites.categories.items():
        if any(domain_matches(host, d) for d in domains):
            return cat
    for cat, suffixes in sites.suffix_rules.items():
        if any(host.endswith(s) or host == s.lstrip(".") for s in suffixes):
            return cat
    hay = fold(host + " " + urlsplit(url).path)
    for cat, keywords in sites.keyword_rules.items():
        if any(k in hay for k in keywords):
            return cat
    return CAT_OTHER


def own_account(url: str, category: str, usernames: list[str]) -> str:
    """'Yes' if a username is a path segment or the site subdomain on an account-type site;
    'Unknown' for other account-type pages; 'No' otherwise."""
    if category not in ACCOUNT_CATEGORIES:
        return "No"
    parts = urlsplit(url)
    segments = {s.lower().lstrip("@~") for s in parts.path.split("/") if s}
    sub = (parts.hostname or "").lower().split(".")[0]
    for u in usernames:
        u = u.lower().lstrip("@")
        if u and (u in segments or u == sub):
            return "Yes"
    return "Unknown"


def matches_keep_list(url: str, keep_accounts: list[str]) -> bool:
    """True if the URL matches an account the client wants to KEEP (URL fragment or handle)."""
    u = fold(url)
    for k in keep_accounts:
        k = fold(k.strip()).removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")
        if k and k in u:
            return True
    return False


def content_type(url: str, category: str, analysis: PageAnalysis | None, mime: str = "") -> str:
    path = urlsplit(url).path.lower()
    mime = mime.lower()
    if "pdf" in mime or re.search(r"\.(pdf|docx?|xlsx?|pptx?|odt)$", path):
        return "PDF / document"
    if mime.startswith("image/") or re.search(r"\.(jpe?g|png|gif|webp)$", path):
        return "Image"
    if mime.startswith("video/") or re.search(r"/(watch|video|videos|shorts|reel)s?\b", path):
        return "Video"
    if category == CAT_ARCHIVE:
        return "Cached copy"
    if category == CAT_MEDIA:
        return "Image"
    if category in (CAT_SOCIAL, CAT_PRO):
        return "Profile page"
    if category in (BROKER_CATEGORY, CAT_DIRECTORY):
        return "Listing"
    if analysis and analysis.places == [PLACE_COMMENTS]:
        return "Comment"
    return "Text"


def severity(data_types: list[str], category: str) -> str:
    t = set(data_types)
    if DT_TCKN in t or DT_IBAN in t or {DT_ADDRESS, DT_PHOTO} <= t:
        return "Critical"
    if t & {DT_ADDRESS, DT_PHONE, DT_DOB, DT_EMAIL} or category == BROKER_CATEGORY:
        return "High"
    if t & {DT_EMPLOYER, DT_PHOTO, DT_RELATIVES, DT_SCHOOL} or category in (CAT_SOCIAL, CAT_FORUM):
        return "Medium"
    return "Low"


def cctld_country(host: str) -> str:
    tld = host.rsplit(".", 1)[-1].lower() if "." in host else ""
    if len(tld) != 2:
        return ""
    return _CCTLD_ALIASES.get(tld, tld.upper())


def removal_method(
    category: str,
    own: str,
    has_opt_out: bool,
    countries: list[str],
    host: str,
) -> str:
    """Suggested method. `countries` are known ISO codes (operator, registrant, hosting)."""
    if own == "Yes":
        return METHOD_OWN
    if category == BROKER_CATEGORY and has_opt_out:
        return METHOD_OPT_OUT
    codes = {c.upper() for c in countries if c}
    tld = cctld_country(host)
    if "TR" in codes or tld == "TR":
        return METHOD_KVKK
    if codes & EU_EEA or tld in EU_EEA or tld == "EU":
        return METHOD_GDPR
    return METHOD_DIRECT


def deindex_fallback(data_types: list[str]) -> str:
    if set(data_types) & {DT_PHONE, DT_ADDRESS, DT_TCKN, DT_EMAIL, DT_IBAN}:
        return FALLBACK_GOOGLE
    return ""


def jurisdiction_text(operator: str = "", registrant: str = "", hosting: str = "") -> str:
    """Human-readable jurisdiction from ISO codes; blank when nothing is known."""
    owner = operator or registrant
    if owner and hosting and owner == hosting:
        return country_name(owner)
    parts = []
    if owner:
        parts.append(f"{country_name(owner)} ({'operator' if operator else 'registrant'})")
    if hosting:
        parts.append(f"{country_name(hosting)} (hosting)")
    return "; ".join(parts)
