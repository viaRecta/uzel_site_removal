"""Search provider interface, registry, response cache, retry/backoff and query budget.

Adding a backend: subclass SearchProvider, implement build_request() and parse(), and
decorate the class with @register("name"). Then list "name" under search.providers in config.yaml.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    position: int  # absolute 1-based rank across pages
    provider: str
    query: str


@dataclass
class HttpRequest:
    url: str
    params: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    # Params/headers holding secrets; excluded from the cache key and never logged.
    secret_params: tuple[str, ...] = ()


@dataclass
class ParsedPage:
    items: list[dict[str, str]]  # each: url, title, snippet
    has_more: bool


class SearchProvider(ABC):
    name: str = ""
    is_google: bool = False
    page_size: int = 10
    max_page_index: int | None = None  # last page the API can return, if limited

    def __init__(self, api_key: str, options: dict[str, Any] | None = None):
        if not api_key:
            raise ValueError(f"No API key configured for search provider '{self.name}'")
        self.api_key = api_key
        self.options = dict(options or {})

    @abstractmethod
    def build_request(self, query: str, page_index: int) -> HttpRequest:
        """Request for results page `page_index` (0-based)."""

    @abstractmethod
    def parse(self, data: dict[str, Any]) -> ParsedPage:
        """Extract organic results from a raw JSON response."""


PROVIDERS: dict[str, type[SearchProvider]] = {}


def register(name: str) -> Callable[[type[SearchProvider]], type[SearchProvider]]:
    def deco(cls: type[SearchProvider]) -> type[SearchProvider]:
        cls.name = name
        PROVIDERS[name] = cls
        return cls

    return deco


class BudgetExhausted(Exception):
    pass


class QueryBudget:
    """Counts paid API calls (one per results page). Cache hits are free."""

    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.used: dict[str, int] = {}

    @property
    def total_used(self) -> int:
        return sum(self.used.values())

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.total_used)

    def consume(self, provider: str) -> None:
        if self.total_used >= self.max_calls:
            raise BudgetExhausted(f"API call budget of {self.max_calls} reached")
        self.used[provider] = self.used.get(provider, 0) + 1


class ResponseCache:
    """Raw API responses on disk, keyed by a hash of provider + request params (no secrets)."""

    def __init__(self, root: Path | None):
        self.root = root

    @staticmethod
    def key(provider: str, req: HttpRequest) -> str:
        params = {k: v for k, v in req.params.items() if k not in req.secret_params}
        blob = json.dumps({"provider": provider, "url": req.url, "params": params}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, provider: str, key: str) -> Path:
        assert self.root is not None
        return self.root / provider / f"{key}.json"

    def get(self, provider: str, key: str) -> dict[str, Any] | None:
        if self.root is None:
            return None
        p = self._path(provider, key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))["response"]
        except (json.JSONDecodeError, KeyError):
            return None

    def put(self, provider: str, key: str, req: HttpRequest, response: dict[str, Any]) -> None:
        if self.root is None:
            return
        p = self._path(provider, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        params = {k: v for k, v in req.params.items() if k not in req.secret_params}
        p.write_text(json.dumps({"params": params, "response": response}, ensure_ascii=False), encoding="utf-8")


RETRY_STATUS = {429, 500, 502, 503, 504}


class SearchError(Exception):
    pass


@dataclass
class RunStats:
    api_calls: int = 0
    cache_hits: int = 0
    errors: list[str] = field(default_factory=list)


class SearchRunner:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: ResponseCache,
        budget: QueryBudget,
        *,
        max_results: int = 20,
        request_delay: float = 1.0,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.client = client
        self.cache = cache
        self.budget = budget
        self.max_results = max_results
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep
        self.stats = RunStats()
        self._last_call = 0.0

    async def _get_json(self, provider: SearchProvider, req: HttpRequest) -> dict[str, Any]:
        key = ResponseCache.key(provider.name, req)
        cached = self.cache.get(provider.name, key)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached

        self.budget.consume(provider.name)
        self.stats.api_calls += 1
        loop = asyncio.get_running_loop()
        attempt = 0
        while True:
            wait = self.request_delay - (loop.time() - self._last_call)
            if wait > 0:
                await self.sleep(wait)
            self._last_call = loop.time()
            try:
                resp = await self.client.get(req.url, params=req.params, headers=req.headers)
                status = resp.status_code
            except httpx.TransportError as exc:
                resp, status = None, None
                err = type(exc).__name__
            if resp is not None and status == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise SearchError(f"{provider.name}: response was not JSON") from exc
                self.cache.put(provider.name, key, req, data)
                return data
            retryable = status is None or status in RETRY_STATUS
            if not retryable or attempt >= self.max_retries:
                reason = f"HTTP {status}" if status else err
                raise SearchError(f"{provider.name}: {reason}")
            delay = self.backoff_base * (2**attempt) + random.uniform(0, 0.5)
            if resp is not None:
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = max(delay, float(ra))
            log.warning("%s: %s, retrying in %.1fs", provider.name, f"HTTP {status}" if status else err, delay)
            await self.sleep(delay)
            attempt += 1

    async def search(self, provider: SearchProvider, query: str) -> list[SearchResult]:
        """Fetch pages until max_results, no more pages, or budget exhausted.

        BudgetExhausted propagates only if not even the first page could be fetched."""
        results: list[SearchResult] = []
        seen: set[str] = set()
        page = 0
        while len(results) < self.max_results:
            if provider.max_page_index is not None and page > provider.max_page_index:
                break
            req = provider.build_request(query, page)
            try:
                data = await self._get_json(provider, req)
            except BudgetExhausted:
                if page == 0:
                    raise
                break
            parsed = provider.parse(data)
            for item in parsed.items:
                url = item.get("url") or ""
                if not url.startswith(("http://", "https://")) or url in seen:
                    continue
                seen.add(url)
                results.append(
                    SearchResult(
                        url=url,
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        position=len(results) + 1,
                        provider=provider.name,
                        query=query,
                    )
                )
            if not parsed.has_more or not parsed.items:
                break
            page += 1
        return results[: self.max_results]


def make_providers(
    names: list[str],
    api_keys: dict[str, str],
    options: dict[str, dict[str, Any]],
    *,
    require_keys: bool = True,
) -> list[SearchProvider]:
    """Instantiate providers by name. require_keys=False (dry runs) uses a placeholder key;
    such providers can build requests and cache keys but must never be used to search."""
    # Import side-effect registers the built-in providers.
    from . import brave, serpapi  # noqa: F401

    out = []
    for n in names:
        n = n.strip().lower()
        if n not in PROVIDERS:
            raise ValueError(f"Unknown search provider '{n}'. Available: {', '.join(sorted(PROVIDERS))}")
        key = api_keys.get(n, "") or ("" if require_keys else "DRY-RUN-NO-KEY")
        out.append(PROVIDERS[n](key, options.get(n)))
    return out
