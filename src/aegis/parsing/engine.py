"""Движок: один проход по источнику — скачать, распознать, превратить в предметы.

Распознавание «на лету», а не по ярлыку: kind='auto' определяется content-type и первыми
байтами (лента есть лента, даже если подписана как страница), канал — по форме цели. Это
значит, что «кинь ссылку, следи за ней» работает без категории: универсальность тут не
маркетинговая, а из структуры функции `scan_source`.

Политика сети — общая с fetch-инструментом агента: SSRF-фильтр на каждый hop, лимиты веса,
никаких cookies. Разница одна: парсеру положено быть прожорливым по разрешению владельца
(PARSER_MAX_BYTES), а не аскетичным, как chat-догрузчик.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from aegis.parsing.dedup import canonical_url, fingerprint
from aegis.parsing.extract import extract_page
from aegis.parsing.feeds import FeedItem, detect_feed, parse_feed, parse_jsonfeed
from aegis.parsing.store import SourceRow
from aegis.parsing.telegram import channel_name, page_url, parse_channel_page, post_id
from aegis.web.net import guard_url, safe_redirect

__all__ = ["FetchDoc", "ItemDraft", "fetch_doc", "scan_source"]

_UA = "Mozilla/5.0 (compatible; aegis/0.1; personal-assistant) "
_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5"
_TG_PAGES_MAX = 3  # страниц t.me за проход — «догнать архив после простоя», но не выкачать канал
_TG_PAGE_SIZE = 20  # столько постов на одной странице t.me/s


class FetchError(RuntimeError):
    """Страница не получена/заблокирована — тик запишет это в last_error источника."""


@dataclass(slots=True)
class FetchDoc:
    url: str
    body: str
    content_type: str
    status: int


@dataclass(slots=True)
class ItemDraft:
    fingerprint: str
    title: str
    url: str
    text: str
    excerpt: str
    author: str = ""
    media: list[str] = field(default_factory=list)
    published_at: datetime | None = None


def _excerpt(text: str, around: str = "", width: int = 280) -> str:
    text = " ".join(text.split())
    if around and around.lower() in text.lower():
        i = text.lower().index(around.lower())
        start = max(0, i - width // 2)
        return ("…" if start else "") + text[start : start + width]
    return text[:width] + ("…" if len(text) > width else "")


def _draft(fp_src: dict[str, str], **kw: Any) -> ItemDraft:
    return ItemDraft(fingerprint=fingerprint(**fp_src), **kw)


async def fetch_doc(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_bytes: int = 20_000_000,
    timeout_s: float = 30.0,
) -> FetchDoc:
    """GET с SSRF-политикой на каждый hop; 4xx/5xx — FetchError."""
    target = guard_url(url)
    own = client is None
    cli = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False, max_redirects=0)
    try:
        current = target
        resp: httpx.Response | None = None
        for _ in range(4):  # noqa: PLR2004 — редиректов шагнём немного: лента ≠ лабиринт
            resp = await cli.get(current, headers={"User-Agent": _UA, "Accept": _ACCEPT})
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    break
                current = safe_redirect(str(httpx.URL(str(resp.url)).join(loc)), base=current)
                continue
            break
        assert resp is not None
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} для {current}")
        raw = resp.content[:max_bytes]
        return FetchDoc(
            url=str(resp.url),
            body=raw.decode(resp.encoding or "utf-8", errors="replace"),
            content_type=str(resp.headers.get("content-type") or ""),
            status=int(resp.status_code),
        )
    finally:
        if own:
            await cli.aclose()


def _feed_drafts(items: list[FeedItem], base_url: str) -> list[ItemDraft]:
    out = []
    for it in items:
        url = urljoin(base_url, it.url) if it.url else base_url
        out.append(
            _draft(
                {"url": canonical_url(url), "title": it.title, "text": it.text},
                title=it.title[:300] or url[:120],
                url=url[:2000],
                text=it.text[:20_000],
                excerpt=_excerpt(it.text, it.title),
                author=it.author,
                published_at=it.published_at or datetime.now(UTC),
            )
        )
    return out


async def scan_source(
    source: SourceRow,
    *,
    cfg: Any,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[ItemDraft], dict[str, Any], list[str]]:
    """(предметы как видим сейчас, курсор, замечания). Сети нет — исключение наверх: тик считает."""
    notes: list[str] = []
    kind = source.kind
    target = source.target
    if kind == "auto":
        kind = "tg_channel" if target.startswith("@") or "t.me/" in target else "web"
    if kind == "tg_channel":
        drafts, cursor = await _scan_channel(source, cfg=cfg, client=client, notes=notes)
    else:
        drafts, cursor = await _scan_http(source, kind=kind, cfg=cfg, client=client, notes=notes)
    return drafts, cursor, notes


async def _scan_channel(
    source: SourceRow, *, cfg: Any, client: httpx.AsyncClient | None, notes: list[str]
) -> tuple[list[ItemDraft], dict[str, Any]]:
    name = channel_name(source.target)
    after = source.cursor.get("after")
    seen_max = int(after) if after is not None and str(after).isdigit() else None
    drafts: list[ItemDraft] = []
    cursor: dict[str, Any] = dict(source.cursor)
    for _page in range(_TG_PAGES_MAX):
        doc = await fetch_doc(
            page_url(name, after=seen_max),
            client=client,
            max_bytes=int(cfg.parser_max_bytes),
            timeout_s=float(cfg.parser_timeout_s),
        )
        posts = parse_channel_page(doc.body, name)
        fresh = [p for p in posts if seen_max is None or (post_id(p.post) or 0) > seen_max]
        for p in fresh:
            body = p.text or "(медиа без текста)"
            title = (p.text[:90] + "…" if len(p.text) > 90 else p.text) or f"{name}: медиа"
            drafts.append(
                _draft(
                    {"url": p.url, "title": "", "text": body},
                    title=title,
                    url=p.url[:2000],
                    text=body[:20_000],
                    excerpt=_excerpt(body),
                    media=list(p.media),
                    author=p.author or name,
                    published_at=p.time or datetime.now(UTC),
                )
            )
        if posts:
            new_max = max(post_id(p.post) or 0 for p in posts)
            if new_max > (seen_max or 0):
                seen_max = new_max
            cursor["after"] = int(seen_max or 0)
        if len(posts) < _TG_PAGE_SIZE or not fresh:
            break
    if not drafts and seen_max is None:
        notes.append(f"канал «{name}»: страница пустая — проверь, публичный ли он")
    return _filter(drafts, source), cursor


async def _scan_http(
    source: SourceRow, *, kind: str, cfg: Any, client: httpx.AsyncClient | None, notes: list[str]
) -> tuple[list[ItemDraft], dict[str, Any]]:
    url = guard_url(source.target)
    doc = await fetch_doc(
        url,
        client=client,
        max_bytes=int(cfg.parser_max_bytes),
        timeout_s=float(cfg.parser_timeout_s),
    )
    head = doc.body[:4096].lower()
    feed_kind = detect_feed(doc.content_type, head) if kind in ("auto", "web") else kind
    if kind in ("rss", "atom"):
        feed_kind = "feed"
    if feed_kind in ("feed", "rss", "atom"):
        items = parse_feed(doc.body)
        notes.append(f"лента: {len(items)} записей")
        return _filter(_feed_drafts(items, doc.url), source), {}
    if feed_kind == "jsonfeed":
        return _filter(_feed_drafts(parse_jsonfeed(doc.body), doc.url), source), {}
    page = extract_page(doc.body, url=doc.url, max_chars=40_000)
    notes.extend(page.warnings[:2])
    body = page.text or ""
    fp_base = canonical_url(doc.url)
    sha = fingerprint(url=fp_base, title=page.title, text=body)[:32]
    changed = str(source.cursor.get("sha") or "") != sha
    cursor = {"sha": sha, "at": int(datetime.now(UTC).timestamp())}
    if not changed:  # «changed»-семантика: страница как есть, предмет только при отличии
        return [], cursor
    draft = _draft(
        {"url": fp_base, "title": page.title, "text": body},
        title=page.title or url[:120],
        url=doc.url[:2000],
        text=body[:20_000],
        excerpt=_excerpt(body),
        author=page.author,
        media=[str(page.meta["image"])] if page.meta.get("image") else [],
        published_at=_ts(page.published),
    )
    return _filter([draft], source), cursor


def _ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        from dateutil import parser as dtparse  # noqa: PLC0415 — один путь

        return dtparse.parse(raw)
    except (ValueError, OverflowError):
        return None


def _filter(drafts: list[ItemDraft], source: SourceRow) -> list[ItemDraft]:
    """Иголки include/exclude — подстрока по title+text, регистронезависимо. exclude сильнее."""
    inc = (source.include_kw or "").strip().lower()
    exc = (source.exclude_kw or "").strip().lower()
    out: list[ItemDraft] = []
    for d in drafts:
        hay = (d.title + " " + d.text).lower()
        if inc and inc not in hay:
            continue
        if exc and exc in hay:
            continue
        out.append(d)
    return out
