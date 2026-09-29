"""Configuration: config.yaml for settings, .env / environment for API keys."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "footprint-scanner/0.1 (privacy footprint audit on behalf of the data subject; "
    "contact: set fetch.user_agent in config.yaml)"
)


@dataclass
class SearchConfig:
    providers: list[str] = field(default_factory=lambda: ["serpapi"])
    max_results_per_query: int = 20
    request_delay: float = 1.0  # seconds between API calls
    max_api_calls: int = 150  # per-run budget; each results page is one call
    max_retries: int = 5
    backoff_base: float = 2.0
    timeout: float = 30.0
    site_query_mode: str = "grouped"  # "grouped" or "per_domain"
    site_group_size: int = 8
    serpapi: dict[str, Any] = field(
        default_factory=lambda: {"engine": "google", "google_domain": "google.com.tr", "gl": "tr", "hl": "tr"}
    )
    brave: dict[str, Any] = field(default_factory=lambda: {"country": "TR", "search_lang": "tr"})


@dataclass
class FetchConfig:
    user_agent: str = DEFAULT_USER_AGENT
    max_pages: int = 200
    concurrency: int = 4
    per_domain_delay: float = 3.0
    timeout: float = 20.0
    screenshots: bool = True
    # Screenshot / JS renderer: "playwright" (local browser), "zenrows" (ZenRows API) or "none".
    renderer: str = "playwright"
    # Playwright only: "" = Playwright's own Chromium, "msedge" / "chrome" = installed browser.
    browser_channel: str = ""
    playwright_timeout: float = 30.0
    js_text_threshold: int = 400  # below this many visible chars, re-render with Playwright
    max_bytes: int = 5_000_000
    zenrows: dict[str, Any] = field(default_factory=lambda: {"max_credits": 1000, "timeout": 180, "wait_ms": 0})


@dataclass
class EnrichConfig:
    rdap: bool = True
    wayback: bool = True
    imprint_lookup: bool = True
    timeout: float = 15.0


@dataclass
class ReportConfig:
    """Customer PDF (footprint-scanner pdf)."""

    prepared_by: str = ""  # your company / name, printed on the cover
    lang: str = "en"  # en or tr
    font: str = ""  # .ttf with Turkish characters; empty = auto (Arial / DejaVuSans)
    font_bold: str = ""


@dataclass
class PathsConfig:
    clients_dir: str = "clients"
    brokers_file: str = "brokers.yaml"
    categories_file: str = "domain_categories.yaml"


@dataclass
class Config:
    search: SearchConfig = field(default_factory=SearchConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    enrich: EnrichConfig = field(default_factory=EnrichConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    base_dir: Path = field(default_factory=Path.cwd)
    api_keys: dict[str, str] = field(default_factory=dict, repr=False)

    def resolve(self, p: str | Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.base_dir / path

    def client_dir(self, case_id: str) -> Path:
        return self.resolve(self.paths.clients_dir) / safe_case_id(case_id)

    def secrets(self) -> list[str]:
        return [v for v in self.api_keys.values() if v]


def safe_case_id(case_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id.strip()).strip("._")
    if not cleaned:
        raise ValueError("Client ID / Case # is empty; fill it in on the Client Profile sheet.")
    return cleaned


def _apply(obj: Any, data: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in (data or {}).items():
        if key not in known:
            log.warning("Unknown config key %s.%s ignored", where, key)
            continue
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, f"{where}.{key}")
        elif isinstance(current, dict) and isinstance(value, dict):
            current.update(value)
        else:
            setattr(obj, key, value)


API_KEY_ENV = {"serpapi": "SERPAPI_API_KEY", "brave": "BRAVE_API_KEY", "zenrows": "ZENROWS_API_KEY"}


def load_config(path: str | Path | None = "config.yaml", env_file: str | Path | None = ".env") -> Config:
    cfg = Config()
    if path is not None:
        p = Path(path)
        if p.exists():
            cfg.base_dir = p.resolve().parent
            with p.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            _apply(cfg, {k: v for k, v in data.items() if k != "base_dir"}, "config")
        else:
            log.info("No %s found; using defaults", p)
    if env_file is not None:
        load_dotenv(cfg.resolve(env_file), override=False)
    cfg.api_keys = {name: os.environ.get(var, "") for name, var in API_KEY_ENV.items()}
    cfg.fetch.user_agent = ascii_user_agent(cfg.fetch.user_agent)
    return cfg


def ascii_user_agent(ua: str) -> str:
    """HTTP headers must be ASCII: 'Önder Uzel' -> 'Onder Uzel'. Empty -> the default."""
    ua = " ".join(str(ua or "").split())
    if not ua:
        return DEFAULT_USER_AGENT
    try:
        ua.encode("ascii")
        return ua
    except UnicodeEncodeError:
        from .textnorm import ascii_translit

        fixed = ascii_translit(ua).encode("ascii", "ignore").decode().strip() or DEFAULT_USER_AGENT
        log.warning("fetch.user_agent has non-ASCII characters (not allowed in HTTP headers); using %r", fixed)
        return fixed
