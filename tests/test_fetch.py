from pathlib import Path

import httpx
import respx

from footprint_scanner.config import FetchConfig
from footprint_scanner.fetch import Fetcher

UA = "footprint-scanner/0.1 (test)"


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeBrowser:
    def __init__(self):
        self.calls = []

    async def capture(self, url, screenshot, want_html):
        self.calls.append((url, want_html))
        if screenshot:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            screenshot.write_bytes(b"png")
        return ("<html><body><p>" + "rendered Ahmet Yılmaz " * 50 + "</p></body></html>") if want_html else "", 200

    async def close(self):
        pass


def fetcher(clock, browser=None, **kw):
    cfg = FetchConfig(user_agent=UA, per_domain_delay=3.0, screenshots=browser is not None, **kw)
    client = httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True)
    return Fetcher(cfg, client=client, browser=browser, sleep=clock.sleep, clock=clock)


@respx.mock
async def test_robots_disallow_blocks_fetch():
    respx.get("https://a.com/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    page = respx.get("https://a.com/private/x").mock(return_value=httpx.Response(200, html="<p>x</p>"))
    clock = FakeClock()
    async with fetcher(clock) as f:
        res = await f.fetch("https://a.com/private/x")
    assert res.robots_blocked and not page.called


@respx.mock
async def test_fetch_ok_and_per_domain_delay():
    respx.get("https://a.com/robots.txt").mock(return_value=httpx.Response(404))
    html = "<html><body><p>" + "Ahmet Yılmaz metin " * 50 + "</p></body></html>"
    respx.get(url__regex=r"https://a\.com/p\d").mock(
        return_value=httpx.Response(200, html=html, headers={"content-type": "text/html; charset=utf-8"})
    )
    clock = FakeClock()
    async with fetcher(clock) as f:
        r1 = await f.fetch("https://a.com/p1")
        r2 = await f.fetch("https://a.com/p2")
    assert r1.ok and r2.ok and "Yılmaz" in r1.html
    # robots + p1 + p2 on the same host: two waits of the per-domain delay.
    assert clock.sleeps == [3.0, 3.0]


@respx.mock
async def test_404_reported_as_gone():
    respx.get("https://a.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://a.com/gone").mock(return_value=httpx.Response(410))
    async with fetcher(FakeClock()) as f:
        res = await f.fetch("https://a.com/gone")
    assert res.gone and not res.ok


@respx.mock
async def test_js_heavy_page_rendered_and_screenshot(tmp_path: Path):
    respx.get("https://spa.com/robots.txt").mock(return_value=httpx.Response(200, text=""))
    respx.get("https://spa.com/u").mock(
        return_value=httpx.Response(200, html='<html><body><div id="root"></div><script>app()</script></body></html>')
    )
    browser = FakeBrowser()
    shot = tmp_path / "evidence" / "0001_spa.com.png"
    async with fetcher(FakeClock(), browser=browser) as f:
        res = await f.fetch("https://spa.com/u", shot, "evidence/0001_spa.com.png")
    assert res.rendered and "rendered Ahmet" in res.html
    assert res.screenshot == "evidence/0001_spa.com.png" and shot.exists()
    assert browser.calls == [("https://spa.com/u", True)]


@respx.mock
async def test_robots_server_error_is_conservative():
    respx.get("https://b.com/robots.txt").mock(return_value=httpx.Response(503))
    page = respx.get("https://b.com/x").mock(return_value=httpx.Response(200))
    async with fetcher(FakeClock()) as f:
        res = await f.fetch("https://b.com/x")
    assert res.robots_blocked and not page.called
