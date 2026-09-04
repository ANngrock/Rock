"""Чтение веб-страниц: httpx + trafilatura, с ручной политикой редиректов и лимитами."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
import structlog
import trafilatura

from aegis.platform.config import settings
from aegis.web.net import BlockedTarget, guard_url, safe_redirect
from aegis.web.search import wrap_untrusted

__all__ = ["FetchResult", "PageFetchError", "WebFetch", "extract_urls"]

log = structlog.get_logger(__name__)

_URL_RX = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)
_MAX_HOPS = 3
_MAX_BODY_BYTES = 3_000_000  # читаем не больше 3 МБ HTML, чтобы не кормить парсер произвольно


def extract_urls(text: str) -> list[str]:
    """Ссылки из сообщения владельца/страницы — для «скинь файл/ссылку» и будущего ingest."""
    seen: list[str] = []
    for raw in _URL_RX.findall(text):
        url = raw.rstrip(".,;:!?)»“”\"'")
        if url not in seen:
            seen.append(url)
    return seen


# httpx кодирует заголовки в ASCII — кириллица в User-Agent = UnicodeEncodeError
_UA_DEFAULT = "Mozilla/5.0 (compatible; aegis/0.1; personal-assistant)"


@dataclass(slots=True)
class FetchResult:
    url: str
    title: str | None
    text: str
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_untrusted(self, max_chars: int | None = None) -> str:
        cap = max_chars or settings().fetch_max_chars
        body = self.text[:cap]
        head = f"{self.title}\n{self.url}\n\n" if self.title else f"{self.url}\n\n"
        tail = "\n…[обрезано]" if len(self.text) > cap else ""
        return wrap_untrusted(self.url, head + body + tail)


class PageFetchError(RuntimeError):
    """Страница не получена/заблокирована политикой — агент сообщит об этом честно."""


class WebFetch:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def fetch(self, url: str, *, max_chars: int | None = None) -> FetchResult:
        cfg = settings()
        target = self._resolve(url)
        headers = {
            "User-Agent": _UA_DEFAULT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru,en;q=0.8",
        }
        client = self._client or httpx.AsyncClient(timeout=cfg.fetch_timeout_s, headers=headers)
        owns = self._client is None
        try:
            resp = await self._get_with_policy(client, target, headers)
        finally:
            if owns:
                await client.aclose()
        raw = resp.content[:_MAX_BODY_BYTES].decode(resp.encoding or "utf-8", errors="replace")
        if resp.status_code >= 400:
            raise PageFetchError(f"HTTP {resp.status_code} для {resp.url}")
        extracted = trafilatura.extract(
            raw,
            include_links=False,
            include_comments=False,
            favor_recall=True,
            url=str(resp.url),
        )
        if not extracted:
            # trafilatura не нашла статьи (SPA/лестница/403-заглушка) — отдаём очищенный текст
            extracted = _fallback_text(raw)
        meta = trafilatura.extract_metadata(raw)
        title = getattr(meta, "title", None)
        cap = max_chars or cfg.fetch_max_chars
        return FetchResult(
            url=str(resp.url),
            title=title,
            text=extracted[: max(cap * 3, cap)],
            truncated=len(extracted) > cap,
        )

    def _resolve(self, url: str) -> str:
        try:
            return guard_url(url)
        except BlockedTarget as exc:
            raise PageFetchError(f"запрошен запрещённый адрес: {exc}") from exc

    async def _get_with_policy(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str]
    ) -> httpx.Response:
        """Редиректы шагаем сами: каждый hop проходит SSRF-фильтр (httpx бы промолчал)."""
        current = url
        for _ in range(_MAX_HOPS + 1):
            resp = await client.get(current, follow_redirects=False, headers=headers)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return resp
                try:
                    current = safe_redirect(str(httpx.URL(current).join(location)), base=current)
                except BlockedTarget as exc:
                    raise PageFetchError(f"редирект запрещён политикой: {exc}") from exc
                continue
            return resp
        raise PageFetchError(f"слишком много редиректов для {url}")


def _fallback_text(html: str) -> str:
    stripped = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", stripped)
    return re.sub(r"\s{2,}", " ", text).strip()
