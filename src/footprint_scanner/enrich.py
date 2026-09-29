"""Domain ownership (RDAP) and archive (Wayback Machine) lookups.

RDAP receives only domain names and IP addresses. The Wayback availability API receives the
found URL (it can't be checked otherwise); disable it with enrich.wayback: false.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import Any

import httpx

from .classify import COUNTRY_NAMES
from .textnorm import fold

log = logging.getLogger(__name__)

RDAP_DOMAIN = "https://rdap.org/domain/{}"
RDAP_IP = "https://rdap.org/ip/{}"
WAYBACK = "https://archive.org/wayback/available"

_PRIVACY_MARKERS = (
    "redacted", "privacy", "proxy", "withheld", "not disclosed", "data protected", "whoisguard",
    "gdpr", "masked", "private", "contact the registrar", "statutory masking",
)
_NAME_TO_CODE = {fold(v): k for k, v in COUNTRY_NAMES.items()} | {
    "united states": "US", "united states of america": "US", "turkiye": "TR", "republic of turkey": "TR",
}


@dataclass
class DomainInfo:
    registrant_org: str = ""
    registrant_country: str = ""
    hosting_country: str = ""
    ip: str = ""
    errors: list[str] = field(default_factory=list)


def _redacted(value: str) -> bool:
    v = fold(value)
    return any(m in v for m in _PRIVACY_MARKERS)


def _country_code(value: str) -> str:
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return _NAME_TO_CODE.get(fold(v), "")


def _walk_entities(entities: list[dict[str, Any]]):
    for e in entities or []:
        yield e
        yield from _walk_entities(e.get("entities") or [])


def parse_rdap_domain(data: dict[str, Any]) -> tuple[str, str]:
    """(registrant organisation, registrant country code); blanks when absent or redacted."""
    org, country = "", ""
    for ent in _walk_entities(data.get("entities") or []):
        if "registrant" not in (ent.get("roles") or []):
            continue
        vcard = ent.get("vcardArray") or []
        props = vcard[1] if len(vcard) > 1 else []
        fn = ""
        for prop in props:
            name, params, _type, value = (prop + [None, None, None, None])[:4]
            if name == "org" and isinstance(value, str) and not _redacted(value):
                org = value.strip()
            elif name == "fn" and isinstance(value, str) and not _redacted(value):
                fn = value.strip()
            elif name == "adr":
                cc = (params or {}).get("cc") if isinstance(params, dict) else None
                if cc:
                    country = _country_code(str(cc))
                elif isinstance(value, list) and value and isinstance(value[-1], str):
                    country = _country_code(value[-1])
        org = org or fn
        break
    return org, country


def parse_rdap_ip(data: dict[str, Any]) -> str:
    return _country_code(str(data.get("country") or ""))


class Enricher:
    def __init__(self, client: httpx.AsyncClient, *, rdap: bool = True, wayback: bool = True, concurrency: int = 2):
        self.client = client
        self.rdap_enabled = rdap
        self.wayback_enabled = wayback
        self._sem = asyncio.Semaphore(concurrency)
        self._domain_cache: dict[str, asyncio.Task[DomainInfo]] = {}

    async def _get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        async with self._sem:
            resp = await self.client.get(endpoint, params=params, follow_redirects=True)
        if resp.status_code != 200:
            return None
        return resp.json()

    async def _resolve(self, host: str) -> str:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        return infos[0][4][0] if infos else ""

    async def _domain_info(self, domain: str, host: str) -> DomainInfo:
        info = DomainInfo()
        if not self.rdap_enabled:
            return info
        try:
            data = await self._get_json(RDAP_DOMAIN.format(domain))
            if data:
                info.registrant_org, info.registrant_country = parse_rdap_domain(data)
        except (httpx.HTTPError, ValueError) as exc:
            info.errors.append(f"RDAP domain: {type(exc).__name__}")
        try:
            info.ip = await self._resolve(host)
            if info.ip:
                data = await self._get_json(RDAP_IP.format(info.ip))
                if data:
                    info.hosting_country = parse_rdap_ip(data)
        except (OSError, httpx.HTTPError, ValueError) as exc:
            info.errors.append(f"RDAP ip: {type(exc).__name__}")
        return info

    async def domain_info(self, domain: str, host: str) -> DomainInfo:
        """Cached per registered domain for the run (concurrent callers share one lookup)."""
        if domain not in self._domain_cache:
            self._domain_cache[domain] = asyncio.ensure_future(self._domain_info(domain, host))
        return await self._domain_cache[domain]

    async def archived(self, url: str) -> str:
        """'Yes' / 'No' from the Wayback availability API, 'Unknown' if disabled or failed."""
        if not self.wayback_enabled:
            return "Unknown"
        try:
            data = await self._get_json(WAYBACK, {"url": url})
        except (httpx.HTTPError, ValueError):
            return "Unknown"
        if data is None:
            return "Unknown"
        closest = (data.get("archived_snapshots") or {}).get("closest") or {}
        return "Yes" if closest.get("available") else "No"
