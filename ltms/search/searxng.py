"""SearXNG JSON client."""

from __future__ import annotations

import asyncio

import httpx

from .base import SearchResult


class SearxngBackend:
    name = "searxng"

    def __init__(self, base_url: str, timeout: float = 25.0, concurrency: int = 6) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._gate = asyncio.Semaphore(concurrency)

    async def search_with_engines(
        self, query: str, limit: int, client: httpx.AsyncClient | None = None
    ) -> tuple[list[SearchResult], dict[str, str]]:
        """Results, plus which engines refused and why.

        SearXNG answers 200 with an empty result list when every engine it
        tried was blocked, rate-limited or serving a CAPTCHA. Reporting that as
        "no results" sends people looking for a bug in their query.
        """
        payload = await self._get(query, client)
        trouble = {
            str(entry[0]): str(entry[1])
            for entry in payload.get("unresponsive_engines") or []
            if isinstance(entry, (list, tuple)) and len(entry) >= 2
        }
        return self._parse(payload, query, limit), trouble

    async def search(self, query: str, limit: int, client: httpx.AsyncClient | None = None) -> list[SearchResult]:
        return self._parse(await self._get(query, client), query, limit)

    async def _get(self, query: str, client: httpx.AsyncClient | None = None) -> dict:
        owns_client = client is None
        client = client or httpx.AsyncClient(timeout=self.timeout)
        try:
            async with self._gate:
                response = await client.get(
                    f"{self.base_url}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "general",
                        "language": "all",
                        "safesearch": "0",
                    },
                    headers={"accept": "application/json"},
                )
            response.raise_for_status()
            return response.json()
        finally:
            if owns_client:
                await client.aclose()

    def _parse(self, payload: dict, query: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        for index, item in enumerate(payload.get("results", [])[:limit]):
            url = item.get("url")
            title = item.get("title")
            if not url or not title:
                continue
            engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
            # SearXNG's own score is sparse; fall back to rank so ordering survives.
            score = float(item.get("score") or 0.0) or max(0.1, 1.0 - index / max(limit, 1))
            results.append(
                SearchResult(
                    title=title.strip(),
                    url=url,
                    snippet=(item.get("content") or "").strip(),
                    engine=",".join(engines)[:40],
                    score=score,
                    published=(item.get("publishedDate") or "")[:10],
                    query=query,
                )
            )
        return results

    async def search_many(
        self,
        queries: list[str],
        per_query: int,
        on_done: "callable | None" = None,
    ) -> tuple[list[SearchResult], list[str], dict[str, str]]:
        """Returns (results, warnings, engines that refused and why)."""
        warnings: list[str] = []
        collected: list[SearchResult] = []
        engines: dict[str, str] = {}

        async with httpx.AsyncClient(timeout=self.timeout) as client:

            async def one(query: str) -> None:
                try:
                    found, trouble = await self.search_with_engines(query, per_query, client=client)
                    collected.extend(found)
                    engines.update(trouble)
                    if on_done:
                        on_done(query, len(found), None)
                except Exception as error:  # noqa: BLE001 - one bad query must not kill the run
                    message = f"{type(error).__name__}: {error}"
                    warnings.append(f"query failed ({query[:40]}): {message}")
                    if on_done:
                        on_done(query, 0, message)

            await asyncio.gather(*(one(query) for query in queries))

        return collected, warnings, engines
