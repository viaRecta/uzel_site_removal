"""Scan / re-scan orchestration: queries -> search -> fetch -> analyze -> classify -> tracker copy.

Discovery and documentation only. Nothing here contacts a site owner or submits any form.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import classify as C
from .analyze import PageAnalysis, analyze_html, analyze_snippet, match_confidence, owner_from_text
from .config import Config
from .enrich import DomainInfo, Enricher
from .excel_io import (
    H_ARCHIVED,
    H_CATEGORY,
    H_CHECKED,
    H_CONFIDENCE,
    H_CONTACT,
    H_CONTENT,
    H_DATA_TYPES,
    H_DATE_FOUND,
    H_DOMAIN,
    H_EVIDENCE,
    H_FALLBACK,
    H_INDEXED,
    H_JURISDICTION,
    H_MENTIONS,
    H_METHOD,
    H_NOTES,
    H_OWN,
    H_OWNER,
    H_PLACES,
    H_RANK,
    H_SEVERITY,
    H_STATUS,
    H_URL,
    ExistingRow,
    Tracker,
    TrackerError,
    timestamped_name,
)
from .fetch import Fetcher, FetchResult, make_renderer
from .identifiers import ClientIdentifiers
from .merge import STATUS_NOT_STARTED, STATUS_REAPPEARED, Hit, Observation, RescanPlan, merge_results, plan_rescan
from .queries import Query, build_queries
from .search import BudgetExhausted, QueryBudget, ResponseCache, SearchError, SearchProvider, SearchRunner
from .sites import SiteDirectory
from .urls import hostname, registered_domain

log = logging.getLogger(__name__)

PROVIDER_LABELS = {"serpapi": "SerpAPI (Google)", "brave": "Brave"}


class AuthorizationError(Exception):
    pass


def check_authorization(ident: ClientIdentifiers, override: bool) -> str:
    """Returns the basis used, or raises. Logged and written to the run report."""
    if ident.authorized:
        basis = f"'Signed authorization received?' = Yes ({ident.source})"
    elif override:
        basis = "--i-have-authorization flag (profile does not say Yes)"
    else:
        raise AuthorizationError(
            "Signed authorization is not recorded as 'Yes' on the Client Profile. "
            "Record it, or pass --i-have-authorization if you hold a signed authorization."
        )
    log.info("Authorization basis: %s", basis)
    return basis


# ---------------------------------------------------------------- run bookkeeping


@dataclass
class QueryLog:
    query: str
    kind: str
    provider: str
    results: int = 0
    error: str = ""
    skipped: bool = False


@dataclass
class Target:
    key: str
    url: str
    row: int
    is_new: bool
    hit: Hit | None
    existing: ExistingRow | None = None


@dataclass
class Processed:
    target: Target
    fetch: FetchResult | None
    analysis: PageAnalysis | None
    domain: str = ""
    domain_info: DomainInfo | None = None
    archived: str = ""
    owner: str = ""
    category: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    mode: str
    case_id: str
    started: dt.datetime
    run_dir: Path
    input_tracker: Path
    output_tracker: Path | None = None
    authorization_basis: str = ""
    providers: list[str] = field(default_factory=list)
    queries: list[Query] = field(default_factory=list)
    query_logs: list[QueryLog] = field(default_factory=list)
    api_calls: dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    budget_max: int = 0
    budget_exhausted: bool = False
    renderer: str = ""
    render_credits: int = 0
    unique_urls: int = 0
    new_rows: list[dict[str, Any]] = field(default_factory=list)
    already_tracked: list[dict[str, Any]] = field(default_factory=list)
    reappeared: list[dict[str, Any]] = field(default_factory=list)
    possibly_removed: list[dict[str, Any]] = field(default_factory=list)
    rescanned: int = 0
    example_row_replaced: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers


def run_timestamp(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-%d_%H%M")


def evidence_name(row_id: int, domain: str, suffix: str = "") -> str:
    safe = re.sub(r"[^a-z0-9.-]+", "_", domain.lower()) or "site"
    return f"evidence/{row_id:04d}_{safe}{suffix}.png"


def emitted_values(sites: SiteDirectory) -> dict[str, set[str]]:
    """Every dropdown value the scanner can write, checked against Lists before anything runs."""
    return {
        H_CATEGORY: sites.all_category_names() | {C.CAT_OTHER},
        H_CONFIDENCE: {"Unverified", "Likely"},
        H_OWN: {"Yes", "No", "Unknown"},
        H_INDEXED: {"Yes", "Unknown"},
        H_ARCHIVED: {"Yes", "No", "Unknown"},
        H_CONTENT: {"Text", "Image", "Video", "PDF / document", "Profile page", "Listing", "Comment", "Cached copy"},
        H_SEVERITY: {"Critical", "High", "Medium", "Low"},
        H_METHOD: {C.METHOD_OWN, C.METHOD_OPT_OUT, C.METHOD_KVKK, C.METHOD_GDPR, C.METHOD_DIRECT},
        H_FALLBACK: {C.FALLBACK_GOOGLE},
        H_STATUS: {STATUS_NOT_STARTED, STATUS_REAPPEARED},
    }


def validate_vocabulary(tracker: Tracker, sites: SiteDirectory) -> None:
    problems = []
    for header, values in emitted_values(sites).items():
        missing = tracker.check_values_exist(header, values)
        if missing:
            problems.append(f"{header}: {missing}")
    if problems:
        raise TrackerError(
            "These values are not on the Lists sheet (fix Lists or domain_categories.yaml): " + "; ".join(problems)
        )


def apply_rescan_plan(tracker: Tracker, plan: RescanPlan) -> None:
    for u in plan.updates:
        tracker.set(u.row, u.header, u.value)
    for row, line in plan.notes:
        tracker.append_note(row, line)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


# ---------------------------------------------------------------- the pipeline


class Scanner:
    def __init__(
        self,
        cfg: Config,
        ident: ClientIdentifiers,
        sites: SiteDirectory,
        providers: list[SearchProvider],
        *,
        mode: str,
        tracker_path: Path,
        authorization_basis: str,
        max_queries: int | None = None,
        now: dt.datetime | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        fetcher_factory: Callable[[], Fetcher] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ):
        assert mode in ("scan", "rescan")
        self.cfg = cfg
        self.ident = ident
        self.sites = sites
        self.providers = providers
        self.mode = mode
        self.tracker_path = Path(tracker_path)
        self.max_queries = max_queries
        self.now = now or dt.datetime.now()
        self.today = self.now.date()
        self.transport = http_transport
        self.fetcher_factory = fetcher_factory
        self.progress = progress or (lambda stage, done, total: None)
        self.client_dir = cfg.client_dir(ident.case_id)
        ts = run_timestamp(self.now)
        run_dir = self.client_dir / "runs" / ts
        if run_dir.exists():
            ts = self.now.strftime("%Y-%m-%d_%H%M%S")
            run_dir = self.client_dir / "runs" / ts
        self.ts = ts
        self.run_dir = run_dir
        self.google_providers = {p.name for p in providers if p.is_google}
        self.result = RunResult(
            mode=mode, case_id=ident.case_id, started=self.now, run_dir=run_dir,
            input_tracker=self.tracker_path, authorization_basis=authorization_basis,
            providers=[p.name for p in providers], budget_max=cfg.search.max_api_calls,
        )

    def _client(self, **kw: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, **kw)

    # ------------------------------------------------------------ search

    def queries(self) -> list[Query]:
        qs = build_queries(self.ident, self.sites, self.cfg.search.site_query_mode, self.cfg.search.site_group_size)
        return qs[: self.max_queries] if self.max_queries else qs

    async def search(self, queries: list[Query]) -> dict[str, Hit]:
        results = []
        cache = ResponseCache(self.client_dir / "cache" / "search")
        budget = QueryBudget(self.cfg.search.max_api_calls)
        s = self.cfg.search
        async with self._client(timeout=s.timeout) as client:
            runner = SearchRunner(
                client, cache, budget, max_results=s.max_results_per_query, request_delay=s.request_delay,
                max_retries=s.max_retries, backoff_base=s.backoff_base,
            )
            total = len(queries) * len(self.providers)
            done = 0
            for q in queries:
                for p in self.providers:
                    qlog = QueryLog(q.text, q.kind, p.name)
                    self.result.query_logs.append(qlog)
                    if self.result.budget_exhausted:
                        qlog.skipped = True
                        continue
                    try:
                        found = await runner.search(p, q.text)
                        qlog.results = len(found)
                        results.extend((q, r) for r in found)
                    except BudgetExhausted:
                        qlog.skipped = True
                        self.result.budget_exhausted = True
                        log.warning("API call budget (%d) exhausted; remaining queries skipped", budget.max_calls)
                    except SearchError as exc:
                        qlog.error = str(exc)
                        self.result.errors.append(f"Search '{q.text}' on {p.name}: {exc}")
                        log.error("Search failed on %s: %s", p.name, exc)
                    done += 1
                    self.progress("search", done, total)
            self.result.api_calls = dict(budget.used)
            self.result.cache_hits = runner.stats.cache_hits
        return merge_results(results, self.google_providers)

    def main_query_ran(self) -> bool:
        return any(
            ql.kind == "name_main" and not ql.skipped and not ql.error for ql in self.result.query_logs
        )

    # ------------------------------------------------------------ per URL

    async def process(self, t: Target, fetcher: Fetcher, enricher: Enricher, fetch_allowed: bool) -> Processed:
        domain = registered_domain(t.url)
        p = Processed(target=t, fetch=None, analysis=None, domain=domain)
        row_id = t.row - 1
        if fetch_allowed:
            rel = evidence_name(row_id, domain, "" if t.is_new else f"_{self.today.isoformat()}")
            try:
                p.fetch = await fetcher.fetch(t.url, self.client_dir / rel, rel)
            except Exception as exc:  # never let one URL kill the run
                p.fetch = FetchResult(url=t.url, error=f"{type(exc).__name__}")
                p.errors.append(f"fetch: {type(exc).__name__}: {exc}")

        f = p.fetch
        if f is not None and f.ok:
            p.analysis = analyze_html(f.html, t.url, self.ident)
        elif t.hit is not None:
            p.analysis = analyze_snippet(t.hit.title, t.hit.snippet, t.url, self.ident)

        if f is None:
            if fetch_allowed is False:
                p.notes.append("Not fetched: max pages per run reached; analysis from search snippet only.")
        elif f.robots_blocked:
            p.notes.append("Not fetched: robots.txt disallows; analysis from search snippet only.")
        elif not f.ok:
            reason = f"HTTP {f.status}" if f.status else (f.error or "error")
            if f.status == 200 and not f.html:
                reason = f"non-HTML content ({f.content_type or 'unknown'})"
            p.notes.append(f"Not fetched: {reason}; analysis from search snippet only.")
        if f is not None and f.screenshot_error:
            p.notes.append(f"Screenshot failed: {f.screenshot_error}")

        if t.is_new:
            await self._enrich_new(p, fetcher, enricher)
        return p

    async def _imprint_owner(self, p: Processed, fetcher: Fetcher) -> str:
        if not (self.cfg.enrich.imprint_lookup and p.analysis and p.analysis.imprint_links):
            return ""
        try:
            res = await fetcher.fetch(p.analysis.imprint_links[0])
        except Exception:
            return ""
        if not res.ok:
            return ""
        from bs4 import BeautifulSoup

        return owner_from_text(BeautifulSoup(res.html, "lxml").get_text(" "))

    async def _enrich_new(self, p: Processed, fetcher: Fetcher, enricher: Enricher) -> None:
        t, a, f = p.target, p.analysis, p.fetch
        host = hostname(t.url)
        info_task = asyncio.ensure_future(enricher.domain_info(p.domain, host))
        archived_task = asyncio.ensure_future(enricher.archived(t.url))
        p.domain_info = await info_task
        p.archived = await archived_task
        p.errors.extend(p.domain_info.errors)

        p.owner = p.domain_info.registrant_org or (a.owner_hint if a else "")
        if not p.owner and a and a.source == "page":
            p.owner = await self._imprint_owner(p, fetcher)

        cat = C.classify_category(t.url, self.sites)
        p.category = cat
        broker = self.sites.broker_for(host)
        own = C.own_account(t.url, cat, self.ident.usernames_clean)
        data_types = a.data_types if a else []
        countries = [broker.country if broker else "", p.domain_info.registrant_country, p.domain_info.hosting_country]
        opt_out = (broker.opt_out_url if broker else "") or ""
        contact = opt_out or (a.best_removal_link if a else "")
        has_opt_out = bool(opt_out) or bool(a and a.has_opt_out_link)
        rank, rank_provider = t.hit.rank(self.google_providers) if t.hit else (None, "")

        p.values = {
            H_DATE_FOUND: self.today,
            H_URL: t.url,
            H_DOMAIN: p.domain,
            H_OWNER: p.owner,
            H_CATEGORY: cat,
            H_JURISDICTION: C.jurisdiction_text(
                operator=broker.country if broker else "",
                registrant=p.domain_info.registrant_country,
                hosting=p.domain_info.hosting_country,
            ),
            H_CONFIDENCE: match_confidence(a) if a else "Unverified",
            H_OWN: own,
            H_CONTENT: C.content_type(t.url, cat, a, f.content_type if f else ""),
            H_DATA_TYPES: ", ".join(data_types),
            H_PLACES: ", ".join(a.places) if a else "",
            H_MENTIONS: a.mentions if (a and a.source == "page") else None,
            H_RANK: rank,
            H_INDEXED: "Yes" if (t.hit and t.hit.found_by_google) else "Unknown",
            H_ARCHIVED: p.archived,
            H_SEVERITY: C.severity(data_types, cat),
            H_METHOD: C.removal_method(cat, own, has_opt_out, countries, host),
            H_CONTACT: contact,
            H_FALLBACK: C.deindex_fallback(data_types),
            H_STATUS: STATUS_NOT_STARTED,
            H_CHECKED: self.today,
            H_EVIDENCE: f.screenshot if f else "",
        }

        stamp = f"[scanner {self.today.isoformat()}]"
        lines = []
        if t.hit:
            qs = t.hit.queries
            shown = "; ".join(qs[:3]) + (f" (+{len(qs) - 3} more in raw.jsonl)" if len(qs) > 3 else "")
            provs = ", ".join(PROVIDER_LABELS.get(x, x) for x in sorted(t.hit.providers))
            lines.append(f"{stamp} Found by {provs} via: {shown}")
        if rank is not None and rank_provider not in self.google_providers:
            lines.append(f"Rank source: {PROVIDER_LABELS.get(rank_provider, rank_provider)} (not Google).")
        if a and a.matched and p.values[H_CONFIDENCE] == "Likely":
            lines.append(f"Likely: name + {', '.join(sorted(a.matched))} on page.")
        if broker and broker.opt_out_url:
            lines.append("Opt-out URL from brokers.yaml; verify it is current.")
        if C.matches_keep_list(t.url, self.ident.keep_accounts):
            lines.append("Client wants to KEEP this account (Client Profile).")
        lines.extend(p.notes)
        lines.append("All suggested values need human review.")
        p.values[H_NOTES] = "\n".join(lines)

    # ------------------------------------------------------------ main

    async def run(self) -> RunResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        tracker = Tracker(self.tracker_path)
        validate_vocabulary(tracker, self.sites)
        self.result.example_row_replaced = tracker.clear_example_row()
        existing = tracker.existing_rows()
        existing_by_key = {r.key: r for r in existing}

        queries = self.queries()
        self.result.queries = queries
        log.info("Running %d queries on %s", len(queries), ", ".join(self.result.providers))
        hits = await self.search(queries)
        self.result.unique_urls = len(hits)

        new_hits = [h for k, h in hits.items() if k not in existing_by_key]
        new_hits.sort(key=lambda h: (h.rank(self.google_providers)[0] or 10_000, -len(h.queries)))
        for k in hits:
            if k in existing_by_key:
                r = existing_by_key[k]
                self.result.already_tracked.append({"row": r.row, "id": r.id, "url": r.url})

        targets: list[Target] = []
        if self.mode == "rescan":
            targets += [Target(r.key, r.url, r.row, False, hits.get(r.key), r) for r in existing]
        rows = tracker.allocate_rows(len(new_hits))
        targets += [Target(h.key, h.url, row, True, h) for h, row in zip(new_hits, rows, strict=True)]
        if tracker.formula_extent > 1000:
            self.result.warnings.append(
                "Findings now has more than 999 rows. Formulas and formatting were extended, "
                "but Summary sheet formulas only count rows 2-1000."
            )

        processed = await self._process_all(targets)

        # Write new rows.
        for p in processed:
            if p.target.is_new:
                for header, value in p.values.items():
                    tracker.set(p.target.row, header, value)
                self.result.new_rows.append(
                    {
                        "row": p.target.row, "id": p.target.row - 1, "url": p.target.url,
                        "category": p.values.get(H_CATEGORY), "confidence": p.values.get(H_CONFIDENCE),
                        "severity": p.values.get(H_SEVERITY), "data_types": p.values.get(H_DATA_TYPES),
                    }
                )

        # Update existing rows (re-scan).
        if self.mode == "rescan":
            obs = {
                p.target.key: Observation(p.target.key, p.target.url, p.target.hit, p.fetch, p.analysis)
                for p in processed if not p.target.is_new
            }
            plan = plan_rescan(
                existing, obs, self.today, main_query_ran=self.main_query_ran(),
                google_providers=self.google_providers, provider_labels=PROVIDER_LABELS,
            )
            apply_rescan_plan(tracker, plan)
            self.result.reappeared = plan.reappeared
            self.result.possibly_removed = plan.possibly_removed
            self.result.rescanned = plan.checked

        for p in processed:
            for e in p.errors:
                self.result.errors.append(f"{p.target.url}: {e}")

        out = self.client_dir / timestamped_name(self.tracker_path, self.now)
        if out.exists() or out.resolve() == self.tracker_path.resolve():
            out = out.with_name(out.stem + self.now.strftime("%S") + ".xlsx")
        self.result.output_tracker = tracker.save(out)
        self._write_raw(processed)
        log.info("Saved %s", self.result.output_tracker)
        return self.result

    async def _process_all(self, targets: list[Target]) -> list[Processed]:
        max_pages = self.cfg.fetch.max_pages
        fetcher = (
            self.fetcher_factory()
            if self.fetcher_factory
            else Fetcher(self.cfg.fetch, browser=make_renderer(self.cfg.fetch, self.cfg.api_keys))
        )
        e = self.cfg.enrich
        headers = {"User-Agent": self.cfg.fetch.user_agent}
        done = 0
        async with fetcher, self._client(timeout=e.timeout, headers=headers) as enr_client:
            enricher = Enricher(enr_client, rdap=e.rdap, wayback=e.wayback)

            async def one(i: int, t: Target) -> Processed:
                nonlocal done
                p = await self.process(t, fetcher, enricher, fetch_allowed=i < max_pages)
                done += 1
                self.progress("pages", done, len(targets))
                return p

            processed = list(await asyncio.gather(*(one(i, t) for i, t in enumerate(targets))))
            browser = fetcher.browser
            if browser is not None:
                self.result.renderer = browser.name
                self.result.render_credits = browser.credits_used
                if browser.unavailable:
                    fix = (
                        "check ZENROWS_API_KEY, your ZenRows credits, or fetch.zenrows.max_credits"
                        if browser.name == "zenrows"
                        else "run `playwright install chromium`, or set fetch.browser_channel: msedge, "
                        "or fetch.renderer: zenrows"
                    )
                    self.result.warnings.append(
                        f"Renderer '{browser.name}' became unavailable ({browser.unavailable}); screenshots "
                        f"after that point were skipped and JS-heavy pages analyzed from static HTML. Fix: {fix}."
                    )
            return processed

    def _write_raw(self, processed: list[Processed]) -> None:
        path = self.run_dir / "raw.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for p in processed:
                t, f, a = p.target, p.fetch, p.analysis
                rank = t.hit.rank(self.google_providers) if t.hit else (None, "")
                rec = {
                    "key": t.key,
                    "url": t.url,
                    "row": t.row,
                    "id": t.row - 1,
                    "new": t.is_new,
                    "queries": t.hit.queries if t.hit else [],
                    "providers": sorted(t.hit.providers) if t.hit else [],
                    "main_query_ranks": t.hit.main_ranks if t.hit else {},
                    "rank": {"value": rank[0], "provider": rank[1]},
                    "search_title": t.hit.title if t.hit else "",
                    "search_snippet": t.hit.snippet if t.hit else "",
                    "fetch": (
                        {k: v for k, v in asdict(f).items() if k != "html"} | {"html_chars": len(f.html)}
                        if f else None
                    ),
                    "analysis": asdict(a) if a else None,
                    "domain": p.domain,
                    "domain_info": asdict(p.domain_info) if p.domain_info else None,
                    "archived": p.archived,
                    "row_values": {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in p.values.items()},
                    "errors": p.errors,
                }
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
