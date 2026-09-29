from pathlib import Path

import httpx
import pytest
import respx

from footprint_scanner.config import FetchConfig
from footprint_scanner.fetch import Browser, Fetcher, ZenRowsBrowser, make_renderer

ZR = "https://api.zenrows.com/v1/"
PNG = b"\x89PNG\r\n\x1a\nfake"


async def no_sleep(_):
    return None


def zr(**kw):
    return ZenRowsBrowser("ZR-SECRET-KEY", backoff_base=0, sleep=no_sleep, **kw)


def shot_response(cost="5"):
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png", "X-Request-Cost": cost})


@respx.mock
async def test_screenshot_request_and_credits(tmp_path: Path):
    route = respx.get(ZR).mock(return_value=shot_response())
    z = zr()
    out = tmp_path / "evidence" / "0001_a.com.png"
    html, status = await z.capture("https://a.com/p?x=1", out, want_html=False)
    assert out.read_bytes() == PNG and status == 200 and html == ""
    params = route.calls[0].request.url.params
    assert params["url"] == "https://a.com/p?x=1"
    assert params["js_render"] == "true" and params["screenshot_fullpage"] == "true"
    assert params["original_status"] == "true" and params["apikey"] == "ZR-SECRET-KEY"
    assert "premium_proxy" not in params and "antibot" not in params
    assert z.credits_used == 5
    await z.close()


@respx.mock
async def test_rendered_html(tmp_path):
    respx.get(ZR).mock(
        side_effect=[shot_response(), httpx.Response(200, text="<p>rendered</p>", headers={"X-Request-Cost": "5"})]
    )
    z = zr()
    html, _ = await z.capture("https://a.com", tmp_path / "s.png", want_html=True)
    assert html == "<p>rendered</p>" and z.credits_used == 10


@respx.mock
async def test_retry_on_429_then_success(tmp_path):
    route = respx.get(ZR).mock(side_effect=[httpx.Response(429), httpx.Response(503), shot_response()])
    await zr().capture("https://a.com", tmp_path / "s.png", want_html=False)
    assert route.call_count == 3


@respx.mock
async def test_402_disables_renderer(tmp_path):
    route = respx.get(ZR).mock(return_value=httpx.Response(402))
    z = zr()
    with pytest.raises(RuntimeError, match="402"):
        await z.capture("https://a.com", tmp_path / "s.png", want_html=False)
    with pytest.raises(RuntimeError):
        await z.capture("https://b.com", tmp_path / "t.png", want_html=False)
    assert route.call_count == 1 and z.unavailable


@respx.mock
async def test_credit_budget_stops_requests(tmp_path):
    route = respx.get(ZR).mock(return_value=shot_response("5"))
    z = zr(max_credits=10)
    await z.capture("https://a.com/1", tmp_path / "1.png", want_html=False)
    await z.capture("https://a.com/2", tmp_path / "2.png", want_html=False)
    with pytest.raises(RuntimeError, match="budget"):
        await z.capture("https://a.com/3", tmp_path / "3.png", want_html=False)
    assert route.call_count == 2


def test_missing_key_is_unavailable():
    assert ZenRowsBrowser("").unavailable


@respx.mock
async def test_fetcher_with_zenrows_respects_robots(tmp_path):
    respx.get("https://a.com/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private\n"))
    respx.get("https://a.com/ok").mock(return_value=httpx.Response(200, html="<p>" + "Ahmet Yılmaz " * 100 + "</p>"))
    zr_route = respx.get(ZR).mock(return_value=shot_response())
    cfg = FetchConfig(per_domain_delay=0, screenshots=True, renderer="zenrows")
    async with Fetcher(cfg, browser=zr()) as f:
        blocked = await f.fetch("https://a.com/private/x", tmp_path / "e1.png", "evidence/e1.png")
        ok = await f.fetch("https://a.com/ok", tmp_path / "e2.png", "evidence/e2.png")
    assert blocked.robots_blocked and blocked.screenshot == ""
    assert ok.ok and ok.screenshot == "evidence/e2.png" and not ok.rendered
    # Only the allowed page went to ZenRows, and only for the screenshot (HTML came directly).
    assert [c.request.url.params["url"] for c in zr_route.calls] == ["https://a.com/ok"]


def test_make_renderer():
    assert isinstance(make_renderer(FetchConfig(renderer="zenrows"), {"zenrows": "k"}), ZenRowsBrowser)
    b = make_renderer(FetchConfig(renderer="playwright", browser_channel="msedge"), {})
    assert isinstance(b, Browser) and b.channel == "msedge"
    assert make_renderer(FetchConfig(renderer="none"), {}) is None
    with pytest.raises(ValueError):
        make_renderer(FetchConfig(renderer="selenium"), {})
