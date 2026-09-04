"""Web: поиск, чтение страниц, (позже) браузерный агент и watchers.

Внешний контент по умолчанию untrusted: модуль отвечает и за оборачивание, и за SSRF-фильтр.
"""

from __future__ import annotations

from aegis.web.fetch import FetchResult, WebFetch, extract_urls
from aegis.web.search import SearchHit, WebSearch

__all__ = ["FetchResult", "SearchHit", "WebFetch", "WebSearch", "extract_urls"]
