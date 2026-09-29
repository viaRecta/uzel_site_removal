"""Loading of brokers.yaml and domain_categories.yaml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from .urls import domain_matches

log = logging.getLogger(__name__)

BROKER_CATEGORY = "Data broker / people search"


@dataclass
class Broker:
    domain: str
    name: str = ""
    opt_out_url: str = ""
    country: str = ""


@dataclass
class SiteDirectory:
    brokers: list[Broker] = field(default_factory=list)
    categories: dict[str, list[str]] = field(default_factory=dict)
    suffix_rules: dict[str, list[str]] = field(default_factory=dict)
    keyword_rules: dict[str, list[str]] = field(default_factory=dict)
    social_query_sites: list[str] = field(default_factory=list)
    professional_query_sites: list[str] = field(default_factory=list)

    def broker_for(self, url_or_host: str) -> Broker | None:
        for b in self.brokers:
            if domain_matches(url_or_host, b.domain):
                return b
        return None

    def all_category_names(self) -> set[str]:
        return {BROKER_CATEGORY, *self.categories, *self.suffix_rules, *self.keyword_rules}


def _read_yaml(path: Path | None, packaged: str) -> dict:
    if path is not None and path.exists():
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    log.info("%s not found; using packaged default", path or packaged)
    text = resources.files("footprint_scanner").joinpath("data", packaged).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def load_sites(brokers_path: Path | None, categories_path: Path | None) -> SiteDirectory:
    b = _read_yaml(brokers_path, "brokers.yaml")
    c = _read_yaml(categories_path, "domain_categories.yaml")
    brokers = []
    for item in b.get("brokers") or []:
        if isinstance(item, str):
            item = {"domain": item}
        if not item.get("domain"):
            continue
        brokers.append(
            Broker(
                domain=str(item["domain"]).lower().strip(),
                name=str(item.get("name") or ""),
                opt_out_url=str(item.get("opt_out_url") or ""),
                country=str(item.get("country") or "").upper(),
            )
        )
    sq = c.get("site_queries") or {}
    return SiteDirectory(
        brokers=brokers,
        categories={k: [d.lower() for d in v or []] for k, v in (c.get("categories") or {}).items()},
        suffix_rules={k: [s.lower() for s in v or []] for k, v in (c.get("suffix_rules") or {}).items()},
        keyword_rules={k: [s.lower() for s in v or []] for k, v in (c.get("keyword_rules") or {}).items()},
        social_query_sites=[d.lower() for d in sq.get("social") or []],
        professional_query_sites=[d.lower() for d in sq.get("professional") or []],
    )
