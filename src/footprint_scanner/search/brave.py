"""Brave Search API backend (https://api.search.brave.com). One request per results page."""

from __future__ import annotations

import re
from typing import Any

from .base import HttpRequest, ParsedPage, SearchProvider, register

_TAGS = re.compile(r"<[^>]+>")


@register("brave")
class BraveProvider(SearchProvider):
    is_google = False
    page_size = 20  # Brave maximum per request
    endpoint = "https://api.search.brave.com/res/v1/web/search"
    max_page_index = 9  # Brave's `offset` is a page index, 0..9

    def build_request(self, query: str, page_index: int) -> HttpRequest:
        params: dict[str, Any] = dict(self.options)
        params.update(q=query, count=self.page_size, offset=page_index)
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key}
        return HttpRequest(self.endpoint, params, headers=headers)

    def parse(self, data: dict[str, Any]) -> ParsedPage:
        results = (data.get("web") or {}).get("results") or []
        items = [
            {
                "url": r.get("url", ""),
                "title": _TAGS.sub("", r.get("title", "")),
                "snippet": _TAGS.sub("", r.get("description", "")),
            }
            for r in results
        ]
        has_more = bool((data.get("query") or {}).get("more_results_available"))
        return ParsedPage(items, has_more)
