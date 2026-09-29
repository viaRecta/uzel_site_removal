"""URL normalization and domain helpers."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

TRACKING_PARAMS = {
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "yclid", "igshid", "igsh", "mc_cid",
    "mc_eid", "_ga", "_gl", "ref", "ref_src", "ref_url", "spm", "srsltid", "si", "feature",
    "trk", "trackingid", "originalsubdomain", "hsctatracking", "_hsenc", "_hsmi", "mkt_tok",
    "cmpid", "ocid", "wt.mc_id", "s_cid",
}
TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Offline public-suffix snapshot bundled with tldextract: no network call. Private suffixes
# are on so that e.g. foo.blogspot.com and bar.github.io count as separate sites.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or k.startswith(TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """Canonical form: lowercase scheme/host, no default port, no fragment, no tracking
    params, remaining params sorted, no trailing slash."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and _DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def url_key(url: str) -> str:
    """Matching key used to dedupe and to match tracker rows: normalized URL without
    scheme and without a leading 'www.'."""
    parts = urlsplit(normalize_url(url))
    host = parts.netloc.removeprefix("www.")
    key = host + parts.path
    if parts.query:
        key += "?" + parts.query
    return key


@lru_cache(maxsize=4096)
def registered_domain(url_or_host: str) -> str:
    """'https://tr.linkedin.com/in/x' -> 'linkedin.com'. Falls back to the host."""
    host = urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host
    host = (host or "").lower()
    ext = _extract(host)
    return ext.top_domain_under_public_suffix or host.removeprefix("www.")


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def domain_matches(host_or_url: str, domain: str) -> bool:
    """True if the host equals `domain` or is a subdomain of it."""
    host = hostname(host_or_url) if "//" in host_or_url else host_or_url.lower()
    domain = domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)
