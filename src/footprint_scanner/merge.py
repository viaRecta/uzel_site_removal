"""Merging search results into unique URLs, and the re-scan merge rules.

Re-scan rules (plan_rescan):
- Existing rows get Last Checked, Mentions on Page, Data Types Exposed, Google Rank for Name
  (and Indexed in Google? = Yes when Google returned it; Evidence only if empty) updated.
- A row whose Status is Removed / Deindexed only is set to Re-appeared, with a note appended, when:
    Deindexed only -> a search returned the URL again;
    Removed        -> a search returned it again, or the page loads (HTTP 200) and still names the client.
- Match Confidence, other Status values, request dates/reference and existing Notes text are never changed.
- Rows whose URL now returns 404/410 are only *reported* as possibly removed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .analyze import PageAnalysis
from .excel_io import (
    H_CHECKED,
    H_DATA_TYPES,
    H_EVIDENCE,
    H_INDEXED,
    H_MENTIONS,
    H_NOTES,
    H_RANK,
    H_STATUS,
    ExistingRow,
)
from .fetch import FetchResult
from .queries import Query
from .search import SearchResult
from .urls import normalize_url, url_key

STATUS_REMOVED = "Removed"
STATUS_DEINDEXED = "Deindexed only"
STATUS_REAPPEARED = "Re-appeared"
STATUS_NOT_STARTED = "Not started"


@dataclass
class Hit:
    url: str  # normalized
    key: str
    title: str = ""
    snippet: str = ""
    queries: list[str] = field(default_factory=list)
    providers: set[str] = field(default_factory=set)
    main_ranks: dict[str, int] = field(default_factory=dict)  # provider -> best rank on the main name query
    found_by_google: bool = False

    def rank(self, google_providers: set[str]) -> tuple[int | None, str]:
        """Best main-query rank, preferring Google-backed providers. Returns (rank, provider)."""
        google = {p: r for p, r in self.main_ranks.items() if p in google_providers}
        pool = google or self.main_ranks
        if not pool:
            return None, ""
        provider = min(pool, key=lambda p: pool[p])
        return pool[provider], provider


def merge_results(results: list[tuple[Query, SearchResult]], google_providers: set[str]) -> dict[str, Hit]:
    hits: dict[str, Hit] = {}
    for q, r in results:
        key = url_key(r.url)
        h = hits.get(key)
        if h is None:
            h = hits[key] = Hit(url=normalize_url(r.url), key=key, title=r.title, snippet=r.snippet)
        if q.text not in h.queries:
            h.queries.append(q.text)
        h.providers.add(r.provider)
        if r.provider in google_providers:
            h.found_by_google = True
        if not h.snippet and r.snippet:
            h.title, h.snippet = r.title or h.title, r.snippet
        if q.is_main:
            prev = h.main_ranks.get(r.provider)
            if prev is None or r.position < prev:
                h.main_ranks[r.provider] = r.position
    return hits


@dataclass
class Observation:
    """What this run learned about one URL."""

    key: str
    url: str
    hit: Hit | None = None
    fetch: FetchResult | None = None
    analysis: PageAnalysis | None = None


@dataclass
class CellUpdate:
    row: int
    header: str
    value: Any


@dataclass
class RescanPlan:
    updates: list[CellUpdate] = field(default_factory=list)
    notes: list[tuple[int, str]] = field(default_factory=list)
    reappeared: list[dict[str, Any]] = field(default_factory=list)
    possibly_removed: list[dict[str, Any]] = field(default_factory=list)
    checked: int = 0


def plan_rescan(
    rows: list[ExistingRow],
    observations: dict[str, Observation],
    today: dt.date,
    *,
    main_query_ran: bool,
    google_providers: set[str],
    provider_labels: dict[str, str] | None = None,
) -> RescanPlan:
    labels = provider_labels or {}
    plan = RescanPlan()
    stamp = f"[scanner {today.isoformat()}]"
    for row in rows:
        obs = observations.get(row.key)
        if obs is None:
            continue
        plan.checked += 1
        up = plan.updates
        up.append(CellUpdate(row.row, H_CHECKED, today))

        fetch, analysis, hit = obs.fetch, obs.analysis, obs.hit
        if fetch is not None and fetch.ok and analysis is not None and analysis.source == "page":
            up.append(CellUpdate(row.row, H_MENTIONS, analysis.mentions))
            up.append(CellUpdate(row.row, H_DATA_TYPES, ", ".join(analysis.data_types)))

        rank, provider = hit.rank(google_providers) if hit else (None, "")
        if rank is not None:
            up.append(CellUpdate(row.row, H_RANK, rank))
            notes = str(row.values.get(H_NOTES) or "")
            if provider not in google_providers:
                label = labels.get(provider, provider)
                if f"rank source: {label}".lower() not in notes.lower():
                    plan.notes.append((row.row, f"{stamp} Rank source: {label} (not Google)."))
        elif main_query_ran and row.values.get(H_RANK) not in (None, ""):
            up.append(CellUpdate(row.row, H_RANK, None))  # no longer in the top results

        if hit is not None and hit.found_by_google:
            up.append(CellUpdate(row.row, H_INDEXED, "Yes"))

        if fetch is not None and fetch.screenshot and not row.values.get(H_EVIDENCE):
            up.append(CellUpdate(row.row, H_EVIDENCE, fetch.screenshot))

        status = str(row.values.get(H_STATUS) or "").strip()
        reason = ""
        if status == STATUS_DEINDEXED and hit is not None:
            reason = f"returned by search again ({', '.join(sorted(hit.providers))})"
        elif status == STATUS_REMOVED:
            if hit is not None:
                reason = f"returned by search again ({', '.join(sorted(hit.providers))})"
            elif fetch is not None and fetch.ok and analysis is not None and analysis.mentions > 0:
                reason = f"page is live again (HTTP 200, {analysis.mentions} mention(s))"
        if reason:
            up.append(CellUpdate(row.row, H_STATUS, STATUS_REAPPEARED))
            plan.notes.append((row.row, f"{stamp} Re-appeared (was {status}): {reason}."))
            plan.reappeared.append({"row": row.row, "id": row.id, "url": row.url, "was": status, "reason": reason})

        if fetch is not None and fetch.gone:
            plan.possibly_removed.append(
                {"row": row.row, "id": row.id, "url": row.url, "http_status": fetch.status, "status": status}
            )
    return plan
