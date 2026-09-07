"""Ленты: RSS 2.0, Atom, JSON Feed — стандартной библиотекой, без новых зависимостей.

Выбор xml.etree а не «ещё одной либы» выстрадан: форматы лент узкие и плоские, а lxml уже стоит
в зависимостях trafilatura — тянуть второй парсер XML значило бы плодить расхождения в «что такое
well-formed». Здесь важно ровно три вещи: терпимость к чужим namespace'ам, dateutil для десяти
форматов даты и то, что битая запись не роняет всю ленту (её пропускаем, ленту отдаём).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dateutil import parser as dtparse

__all__ = ["FeedItem", "detect_feed", "parse_feed"]


@dataclass(slots=True)
class FeedItem:
    title: str
    url: str = ""
    text: str = ""
    author: str = ""
    published_at: datetime | None = None
    tags: dict[str, Any] = field(default_factory=dict)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _find_text(el: ET.Element, *names: str) -> str:
    for child in el:
        if _local(child.tag) in names and child.text:
            return " ".join(child.text.split())
    return ""


def _find_attr(el: ET.Element, name: str, *names: str) -> str:
    for child in el:
        if _local(child.tag) in names:
            return (child.get(name) or _find_text(child) or "").strip()
    return ""


def _ts(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return dtparse.parse(raw)
    except (ValueError, OverflowError):
        return None


def _strip_html(text: str) -> str:
    import re

    return " ".join(re.sub(r"(?s)<[^>]+>", " ", text).split())


def parse_feed(body: str | bytes) -> list[FeedItem]:
    """XML-лента (RSS/Atom) → предметы; для JSON Feed — :func:`parse_jsonfeed`."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    lowered = body[:64_000].lower()
    if "<!entity" in lowered:
        # ElementTree не резолвит внешние сущности (от XXE защищён по устройству), но
        # внутренние — expands: billion laughs летит через них. Легальные ленты их не содержат.
        raise ValueError("лента содержит объявление сущностей — не разбираем (защита от XML-бомб)")
    try:
        root = ET.fromstring(body)  # noqa: S314 — выше отсечены единственные опасные конструкции
    except ET.ParseError as exc:
        raise ValueError(f"лента не парсится как XML: {exc}") from exc
    root_local = _local(root.tag)
    items: list[FeedItem] = []
    if root_local == "rss":
        for node in root.iter("item"):
            items.append(_rss_item(node))
    elif root_local == "feed":  # Atom
        for node in root:
            if _local(node.tag) == "entry":
                items.append(_atom_entry(node))
    else:  # «channel где-то внутри» — последний шанс: Dublin Core/RDF и прочие помеси
        for node in root.iter():
            if _local(node.tag) == "item":
                items.append(_rss_item(node))
    return [i for i in items if i.title or i.text]


def _rss_item(node: ET.Element) -> FeedItem:
    title = _find_text(node, "title")
    link = _find_attr(node, "href", "link") or _find_text(node, "link")
    desc = _strip_html(_find_text(node, "description", "summary"))
    date = _ts(_find_text(node, "pubdate", "date", "published", "updated"))
    author = _find_text(node, "author", "creator")
    return FeedItem(title=title, url=link, text=desc, author=author, published_at=date)


def _atom_entry(node: ET.Element) -> FeedItem:
    title = _find_text(node, "title")
    link = ""
    alt = ""
    for child in node:
        if _local(child.tag) == "link":
            href = child.get("href") or ""
            rel = (child.get("rel") or "alternate").lower()
            if rel == "alternate" and not alt:
                alt = href
            elif not link:
                link = href
    text = _strip_html(_find_text(node, "content", "summary"))
    date = _ts(_find_text(node, "published", "updated", "date"))
    author = ""
    for child in node:
        if _local(child.tag) == "author":
            author = _find_text(child, "name") or " ".join("".join(child.itertext()).split())[:80]
            break
    return FeedItem(title=title, url=link or alt, text=text, author=author, published_at=date)


def parse_jsonfeed(body: str | bytes) -> list[FeedItem]:
    """JSON Feed 1.x (и терпимо к «похожему»): items[] с content_text/content_html."""
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("JSON Feed без массива items")
    out: list[FeedItem] = []
    for raw in data["items"]:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("content_text") or "").strip() or _strip_html(
            str(raw.get("content_html") or "")
        )
        author = raw.get("author")
        names = (
            ", ".join(str(a.get("name", "")) for a in author if isinstance(a, dict))
            if isinstance(author, list)
            else ""
        )
        out.append(
            FeedItem(
                title=str(raw.get("title") or "").strip()[:300],
                url=str(raw.get("url") or raw.get("url_external") or "")[:2000],
                text=text[:20_000],
                author=names[:200],
                published_at=_ts(str(raw.get("date_published") or "")),
            )
        )
    return [i for i in out if i.title or i.text]


def detect_feed(content_type: str, body_head: str) -> str:
    """'rss' | 'atom' | 'jsonfeed' | '' — по заголовку и первым байтам, как это делают браузеры."""
    ctype = content_type.lower()
    if "rss" in ctype or "atom" in ctype:
        return "feed"
    if "json" in ctype and '"items"' in body_head:
        return "jsonfeed"
    head = body_head.lstrip().lower()
    if head.startswith("<?xml") and ("<rss" in head[:600] or "<feed" in head[:600]):
        return "feed"
    if head.startswith("{") and '"version"' in head[:200] and "feed" in head[:200]:
        return "jsonfeed"
    return ""
