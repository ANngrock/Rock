"""Сетевые checker'ы наблюдателей: точка композиции web ⇄ planning (контракт слоёв).

``planning.watchers`` намеренно не знает, откуда берётся текст страницы — домены не импортируют
друг друга (import-linter). Здесь склейка: fetch со всей SSRF-политикой и поиск через движки;
ошибка сети превращается в CheckError — наблюдение копит падения и уходит в паузу с причиной,
а не «не нашло».
"""

from __future__ import annotations

from typing import Any

from aegis.planning.watchers import CheckError, Watch


class PageChecker:
    def __init__(self, fetcher: Any = None) -> None:
        self._fetcher = fetcher

    async def body(self, watch: Watch) -> str:
        fetcher = self._fetcher
        if fetcher is None:
            from aegis.web.fetch import WebFetch

            fetcher = WebFetch()
        try:
            result = await fetcher.fetch(watch.target, max_chars=200_000)
        except Exception as exc:  # noqa: BLE001 - чужая сеть/SSRF-политика: причина важнее стека
            raise CheckError(f"страница не получена: {str(exc)[:160]}") from exc
        return str(getattr(result, "text", result))


class SearchChecker:
    def __init__(self, searcher: Any = None) -> None:
        self._searcher = searcher

    async def body(self, watch: Watch) -> str:
        searcher = self._searcher
        if searcher is None:
            from aegis.web.search import WebSearch

            searcher = WebSearch()
        try:
            hits = await searcher.search(watch.target, count=8)
        except Exception as exc:  # noqa: BLE001
            raise CheckError(f"поиск недоступен: {str(exc)[:160]}") from exc
        return "\n".join(f"{h.title}\n{h.url}\n{h.snippet}" for h in hits)


def default_checkers() -> dict[str, Any]:
    """kwargs для run_watches: ``run_watches(store, **default_checkers())``."""
    return {"page": PageChecker(), "search": SearchChecker()}
