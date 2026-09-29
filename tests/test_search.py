import httpx
import pytest
import respx

from footprint_scanner.search import (
    BudgetExhausted,
    QueryBudget,
    ResponseCache,
    SearchError,
    SearchRunner,
    make_providers,
)

SERP = "https://serpapi.com/search.json"
BRAVE = "https://api.search.brave.com/res/v1/web/search"


async def no_sleep(_):
    return None


def serp_page(start, n, more=True):
    return {
        "organic_results": [
            {"position": i + 1, "link": f"https://site{start + i}.com/p", "title": f"T{start + i}", "snippet": "s"}
            for i in range(n)
        ],
        "serpapi_pagination": {"next": "x"} if more else {},
    }


def runner(client, tmp_path, budget=100, max_results=20):
    return SearchRunner(
        client, ResponseCache(tmp_path / "cache"), QueryBudget(budget),
        max_results=max_results, request_delay=0, sleep=no_sleep,
    )


@respx.mock
async def test_serpapi_pagination_and_absolute_rank(tmp_path):
    route = respx.get(SERP).mock(
        side_effect=lambda req: httpx.Response(
            200, json=serp_page(int(req.url.params["start"]), 10, more=True)
        )
    )
    [prov] = make_providers(["serpapi"], {"serpapi": "SECRETKEY123"}, {"serpapi": {"gl": "tr"}})
    async with httpx.AsyncClient() as c:
        r = runner(c, tmp_path)
        results = await r.search(prov, '"Ahmet Yılmaz"')
    assert len(results) == 20
    assert [x.position for x in results[:3]] == [1, 2, 3]
    assert results[10].url == "https://site10.com/p" and results[10].position == 11
    assert route.call_count == 2
    assert route.calls[0].request.url.params["gl"] == "tr"
    assert r.budget.used == {"serpapi": 2}


@respx.mock
async def test_cache_prevents_second_spend_and_hides_key(tmp_path):
    route = respx.get(SERP).mock(return_value=httpx.Response(200, json=serp_page(0, 5, more=False)))
    [prov] = make_providers(["serpapi"], {"serpapi": "SECRETKEY123"}, {})
    async with httpx.AsyncClient() as c:
        await runner(c, tmp_path).search(prov, "q")
        r2 = runner(c, tmp_path)
        again = await r2.search(prov, "q")
    assert route.call_count == 1
    assert len(again) == 5 and r2.stats.cache_hits == 1 and r2.budget.total_used == 0
    for f in (tmp_path / "cache").rglob("*.json"):
        assert "SECRETKEY123" not in f.read_text(encoding="utf-8")


@respx.mock
async def test_backoff_on_429_then_success(tmp_path):
    route = respx.get(SERP).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(503),
            httpx.Response(200, json=serp_page(0, 3, more=False)),
        ]
    )
    [prov] = make_providers(["serpapi"], {"serpapi": "k" * 10}, {})
    async with httpx.AsyncClient() as c:
        r = runner(c, tmp_path)
        res = await r.search(prov, "q")
    assert len(res) == 3 and route.call_count == 3
    assert r.budget.total_used == 1  # retries don't count as extra credits


@respx.mock
async def test_non_retryable_error(tmp_path):
    respx.get(SERP).mock(return_value=httpx.Response(401))
    [prov] = make_providers(["serpapi"], {"serpapi": "k" * 10}, {})
    async with httpx.AsyncClient() as c:
        with pytest.raises(SearchError, match="HTTP 401"):
            await runner(c, tmp_path).search(prov, "q")


@respx.mock
async def test_budget_stops_pagination(tmp_path):
    respx.get(SERP).mock(side_effect=lambda req: httpx.Response(200, json=serp_page(int(req.url.params["start"]), 10)))
    [prov] = make_providers(["serpapi"], {"serpapi": "k" * 10}, {})
    async with httpx.AsyncClient() as c:
        r = runner(c, tmp_path, budget=1)
        res = await r.search(prov, "q1")
        assert len(res) == 10
        with pytest.raises(BudgetExhausted):
            await r.search(prov, "q2")


@respx.mock
async def test_brave_parse_and_headers(tmp_path):
    route = respx.get(BRAVE).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {"results": [{"url": "https://a.com/x", "title": "<strong>Ahmet</strong>", "description": "d"}]},
                "query": {"more_results_available": False},
            },
        )
    )
    [prov] = make_providers(["brave"], {"brave": "BRAVEKEY99"}, {"brave": {"country": "TR"}})
    async with httpx.AsyncClient() as c:
        res = await runner(c, tmp_path).search(prov, "q")
    assert res[0].title == "Ahmet" and res[0].provider == "brave"
    req = route.calls[0].request
    assert req.headers["X-Subscription-Token"] == "BRAVEKEY99"
    assert req.url.params["offset"] == "0" and req.url.params["country"] == "TR"


def test_missing_key_and_unknown_provider():
    with pytest.raises(ValueError, match="No API key"):
        make_providers(["brave"], {}, {})
    with pytest.raises(ValueError, match="Unknown search provider"):
        make_providers(["bing"], {"bing": "x"}, {})
