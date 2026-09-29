"""Polite page fetching: robots.txt, per-domain delay, timeouts, concurrency limit.

httpx does the normal fetch. Playwright (headless Chromium) takes the full-page screenshot and
re-renders pages whose static HTML has almost no visible text (JS-heavy pages).
Pages that robots.txt disallows are neither fetched nor screenshotted.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, UnicodeDammit

from .config import FetchConfig
from .textnorm import collapse_ws

log = logging.getLogger(__name__)

HTML_TYPES = ("text/html", "application/xhtml+xml")


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int | None = None
    html: str = ""
    content_type: str = ""
    error: str = ""
    robots_blocked: bool = False
    rendered: bool = False  # HTML came from Playwright
    screenshot: str = ""  # path as written to the tracker (relative)
    screenshot_error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)

    @property
    def gone(self) -> bool:
        return self.status in (404, 410)


def visible_text_length(html: str) -> int:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "template", "svg", "head"]):
        t.decompose()
    return len(collapse_ws(soup.get_text(" ")))


class Browser:
    """Lazy headless Chromium via Playwright. If Playwright or its browser isn't installed,
    screenshots are skipped with a warning instead of failing the run.

    `channel` uses an installed browser instead of Playwright's download: "msedge" (present on
    every Windows 10/11 machine) or "chrome"."""

    name = "playwright"

    def __init__(self, user_agent: str, timeout: float, channel: str = ""):
        self.user_agent = user_agent
        self.timeout_ms = int(timeout * 1000)
        self.channel = channel or None
        self._pw = None
        self._browser = None
        self.unavailable: str = ""
        self.credits_used = 0
        self._lock = asyncio.Lock()

    async def _ensure(self) -> bool:
        if self._browser or self.unavailable:
            return self._browser is not None
        async with self._lock:
            if self._browser or self.unavailable:
                return self._browser is not None
            try:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True, channel=self.channel)
            except Exception as exc:  # ImportError, missing browser binary, ...
                self.unavailable = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
                log.warning("Playwright unavailable, no screenshots/JS rendering: %s", self.unavailable)
                log.warning(
                    "Fix with: playwright install chromium, or set fetch.browser_channel: msedge, "
                    "or fetch.renderer: zenrows"
                )
        return self._browser is not None

    async def capture(self, url: str, screenshot: Path | None, want_html: bool) -> tuple[str, int | None]:
        """Navigate once; optionally save a full-page screenshot and/or return rendered HTML."""
        if not await self._ensure():
            raise RuntimeError("browser not available (see the run report warning)")
        ctx = await self._browser.new_context(
            user_agent=self.user_agent, viewport={"width": 1366, "height": 900}, locale="tr-TR"
        )
        try:
            page = await ctx.new_page()
            resp = await page.goto(url, wait_until="load", timeout=self.timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            if screenshot is not None:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True)
            html = await page.content() if want_html else ""
            return html, (resp.status if resp else None)
        finally:
            await ctx.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"
_ZENROWS_RETRY = {429, 500, 503, 504}


class ZenRowsBrowser:
    """Screenshots and JS rendering through the ZenRows API instead of a local browser.

    Same interface as Browser. Only URLs that robots.txt allows reach this class, and only
    when a screenshot or rendered HTML is needed; plain HTML is still fetched directly.
    ZenRows receives the page URL (not the client's other identifiers).

    Credits (per ZenRows pricing): js_render = 5 per successful request. A screenshot and a
    rendered-HTML fetch are separate requests. Anti-bot / premium proxy features are not used.
    """

    name = "zenrows"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 180.0,
        max_credits: int = 1000,
        wait_ms: int = 0,
        max_retries: int = 4,
        backoff_base: float = 10.0,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.api_key = api_key
        self.max_credits = max_credits
        self.wait_ms = wait_ms
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.credits_used = 0
        self.unavailable: str = "" if api_key else "ZENROWS_API_KEY is not set"
        if not api_key:
            log.warning("ZenRows renderer selected but ZENROWS_API_KEY is not set; no screenshots")

    def _params(self, url: str, **extra: str) -> dict[str, str]:
        params = {"apikey": self.api_key, "url": url, "js_render": "true", "original_status": "true", **extra}
        if self.wait_ms:
            params["wait"] = str(self.wait_ms)
        return params

    async def _request(self, url: str, **extra: str) -> httpx.Response:
        if self.unavailable:
            raise RuntimeError(f"ZenRows unavailable: {self.unavailable}")
        if self.credits_used + 5 > self.max_credits:
            self.unavailable = f"per-run credit budget ({self.max_credits}) reached"
            log.warning("ZenRows %s; remaining screenshots skipped", self.unavailable)
            raise RuntimeError(f"ZenRows unavailable: {self.unavailable}")
        attempt = 0
        while True:
            try:
                resp = await self.client.get(ZENROWS_ENDPOINT, params=self._params(url, **extra))
                status = resp.status_code
            except httpx.TransportError as exc:
                resp, status = None, None
                err = type(exc).__name__
            if resp is not None:
                cost = resp.headers.get("X-Request-Cost")
                if cost:
                    try:
                        self.credits_used += int(float(cost))
                    except ValueError:
                        pass
                if status == 402:
                    self.unavailable = "no ZenRows credits left or feature not in plan (HTTP 402)"
                    log.error("ZenRows: %s", self.unavailable)
                    raise RuntimeError(f"ZenRows unavailable: {self.unavailable}")
                if status == 401:
                    self.unavailable = "API key rejected (HTTP 401)"
                    log.error("ZenRows: %s", self.unavailable)
                    raise RuntimeError(f"ZenRows unavailable: {self.unavailable}")
            if resp is not None and status not in _ZENROWS_RETRY:
                return resp
            if attempt >= self.max_retries:
                raise RuntimeError(f"ZenRows: {f'HTTP {status}' if status else err} after {attempt + 1} tries")
            delay = self.backoff_base * (2**attempt) + random.uniform(0, 1)
            log.warning("ZenRows %s, retrying in %.0fs", f"HTTP {status}" if status else err, delay)
            await self.sleep(delay)
            attempt += 1

    async def capture(self, url: str, screenshot: Path | None, want_html: bool) -> tuple[str, int | None]:
        html, status = "", None
        if screenshot is not None:
            resp = await self._request(url, screenshot_fullpage="true")
            status = resp.status_code
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                screenshot.write_bytes(resp.content)
            else:
                raise RuntimeError(f"ZenRows screenshot failed (HTTP {resp.status_code})")
        if want_html:
            resp = await self._request(url)
            status = resp.status_code
            if resp.status_code == 200:
                html = resp.text
        return html, status

    async def close(self) -> None:
        if self._own_client:
            await self.client.aclose()


def make_renderer(cfg: FetchConfig, api_keys: dict[str, str]) -> Browser | ZenRowsBrowser | None:
    """The screenshot/JS renderer selected by fetch.renderer (playwright | zenrows | none)."""
    renderer = (cfg.renderer or "playwright").lower()
    if renderer == "none":
        return None
    if renderer == "zenrows":
        z = cfg.zenrows
        return ZenRowsBrowser(
            api_keys.get("zenrows", ""),
            timeout=float(z.get("timeout", 180)),
            max_credits=int(z.get("max_credits", 1000)),
            wait_ms=int(z.get("wait_ms", 0)),
        )
    if renderer == "playwright":
        return Browser(cfg.user_agent, cfg.playwright_timeout, cfg.browser_channel)
    raise ValueError(f"Unknown fetch.renderer '{cfg.renderer}' (use playwright, zenrows or none)")


class Fetcher:
    def __init__(
        self,
        cfg: FetchConfig,
        client: httpx.AsyncClient | None = None,
        browser: Browser | ZenRowsBrowser | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": cfg.user_agent, "Accept-Language": "tr,en;q=0.8"},
            timeout=cfg.timeout,
            follow_redirects=True,
        )
        # Renderer for screenshots / JS pages (see make_renderer); None = static HTML only.
        self.browser = browser
        self.sleep = sleep
        self.clock = clock
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, asyncio.Task[robotparser.RobotFileParser]] = {}

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self.browser:
            await self.browser.close()
        if self._own_client:
            await self.client.aclose()

    # ------------------------------------------------------------ politeness

    async def _polite(self, host: str) -> None:
        """Serialize requests per host and keep at least per_domain_delay between them.
        Call while holding the host lock."""
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.cfg.per_domain_delay - (self.clock() - last)
            if wait > 0:
                await self.sleep(wait)
        self._last_hit[host] = self.clock()

    def _lock(self, host: str) -> asyncio.Lock:
        return self._domain_locks.setdefault(host, asyncio.Lock())

    async def _load_robots(self, origin: str, host: str) -> robotparser.RobotFileParser:
        rp = robotparser.RobotFileParser()
        try:
            async with self._lock(host):
                await self._polite(host)
                resp = await self.client.get(origin + "/robots.txt")
            if resp.status_code in (401, 403):
                rp.disallow_all = True
            elif 400 <= resp.status_code < 500:
                rp.allow_all = True
            elif resp.status_code >= 500:
                rp.disallow_all = True  # server trouble: be conservative
            else:
                rp.parse(resp.text.splitlines())
        except httpx.HTTPError:
            rp.disallow_all = True
        return rp

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.hostname or ""
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = asyncio.ensure_future(self._load_robots(origin, host))
        rp = await self._robots[origin]
        return rp.can_fetch(self.cfg.user_agent, url)

    # ------------------------------------------------------------ fetching

    async def _http_get(self, url: str, host: str) -> FetchResult:
        res = FetchResult(url=url)
        async with self._lock(host):
            await self._polite(host)
            try:
                async with self.client.stream("GET", url) as resp:
                    res.status = resp.status_code
                    res.final_url = str(resp.url)
                    res.content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if resp.status_code == 200 and res.content_type in HTML_TYPES:
                        body = bytearray()
                        async for chunk in resp.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self.cfg.max_bytes:
                                break
                        charset = resp.charset_encoding
                        if charset:
                            res.html = bytes(body).decode(charset, errors="replace")
                        else:
                            res.html = UnicodeDammit(bytes(body), is_html=True).unicode_markup or ""
            except httpx.HTTPError as exc:
                res.error = type(exc).__name__
        return res

    async def fetch(self, url: str, screenshot_path: Path | None = None, screenshot_rel: str = "") -> FetchResult:
        """Fetch one page politely. `screenshot_path` is where to save the PNG; `screenshot_rel`
        is the path recorded in the tracker."""
        host = urlsplit(url).hostname or ""
        async with self._sem:
            try:
                if not await self.allowed(url):
                    return FetchResult(url=url, robots_blocked=True, error="robots.txt disallows")
            except Exception as exc:
                return FetchResult(url=url, error=f"robots check failed: {type(exc).__name__}")

            res = await self._http_get(url, host)
            if res.status != 200:
                return res

            want_html = res.content_type in HTML_TYPES and (
                not res.html or visible_text_length(res.html) < self.cfg.js_text_threshold
            )
            shot = screenshot_path if (self.cfg.screenshots and screenshot_path) else None
            if self.browser is not None and (shot or want_html):
                try:
                    async with self._lock(host):
                        await self._polite(host)
                        html, _status = await self.browser.capture(url, shot, want_html)
                    if want_html and html:
                        res.html, res.rendered = html, True
                    if shot and shot.exists():
                        res.screenshot = screenshot_rel
                except Exception as exc:
                    res.screenshot_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:150] if str(exc) else ''}"
            return res
