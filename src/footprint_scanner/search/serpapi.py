"""SerpAPI backend (Google results via https://serpapi.com). One API credit per results page."""

from __future__ import annotations

from typing import Any

from .base import HttpRequest, ParsedPage, SearchProvider, register


@register("serpapi")
class SerpApiProvider(SearchProvider):
    is_google = True
    page_size = 10
    endpoint = "https://serpapi.com/search.json"

    def build_request(self, query: str, page_index: int) -> HttpRequest:
        params: dict[str, Any] = {"engine": "google", **self.options}
        params.update(q=query, num=self.page_size, start=page_index * self.page_size, api_key=self.api_key)
        return HttpRequest(self.endpoint, params, secret_params=("api_key",))

    def parse(self, data: dict[str, Any]) -> ParsedPage:
        # "Google hasn't returned any results" comes back as an "error" key with HTTP 200.
        organic = data.get("organic_results") or []
        items = [
            {"url": r.get("link", ""), "title": r.get("title", ""), "snippet": r.get("snippet", "")}
            for r in organic
        ]
        has_more = bool((data.get("serpapi_pagination") or {}).get("next"))
        return ParsedPage(items, has_more)
