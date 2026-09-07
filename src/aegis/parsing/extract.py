"""Веб-страница → предмет: заголовок, текст, дата, ссылки, вес.

Тот же trafilatura, что и у fetch-инструмента агента (единая точка правды «что такое текст
страницы»), но с двумя поправками на «универсальность»: при пустом результате — fallback-выжимка
(SPA-заглушки, кривые вложенности), а ссылки и метаданные снимаются отдельным прохождением,
ибо парсеру они нужны структурой, а не «текстом рядом со ссылкой».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import trafilatura

__all__ = ["PageDoc", "extract_page"]

_TAG_STRIP = re.compile(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>")
_ANY_TAG = re.compile(r"(?s)<[^>]+>")


@dataclass(slots=True)
class PageDoc:
    title: str = ""
    text: str = ""
    author: str = ""
    published: str = ""
    links: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def _fallback_text(html: str) -> str:
    stripped = _TAG_STRIP.sub(" ", html)
    text = _ANY_TAG.sub(" ", stripped)
    return re.sub(r"\s{2,}", " ", text).strip()


def extract_page(html: str, *, url: str = "", max_chars: int = 40_000) -> PageDoc:
    doc = PageDoc()
    main = ""
    try:
        main = (
            trafilatura.extract(
                html, include_links=False, include_comments=False, favor_recall=True, url=url
            )
            or ""
        )
    except Exception as exc:  # noqa: BLE001 — экстрактор не диктует жизнь странице
        doc.warnings.append(f"trafilatura: {type(exc).__name__}")
    if not main.strip():
        main = _fallback_text(html)
        doc.warnings.append("извлечён fallback-текст (страница без читаемой статьи)")
    doc.text = main[:max_chars].strip()
    if len(main) > max_chars:
        doc.warnings.append(f"текст обрезан до {max_chars} символов")
    try:
        meta = trafilatura.extract_metadata(html, default_url=url or None)
        if meta is not None:
            doc.title = (meta.title or "").strip()[:300]
            doc.author = (meta.author or "").strip()[:200]
            doc.published = (meta.date or "").strip()[:40]
            doc.meta = {
                k: v
                for k, v in dict(getattr(meta, "__dict__", {})).items()
                if isinstance(v, str) and k in {"sitename", "categories", "tags", "image"}
            }
    except Exception as exc:  # noqa: BLE001
        doc.warnings.append(f"метаданные: {type(exc).__name__}")
    if not doc.title:
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        doc.title = _ANY_TAG.sub("", m.group(1)).strip()[:300] if m else ""
    hrefs = re.findall(r"""(?i)<a[^>]+href=["']([^"'#][^"']*)["']""", html)
    seen: set[str] = set()
    for h in hrefs:
        h = h.strip()
        if h and not h.startswith(("javascript:", "mailto:")) and h not in seen:
            seen.add(h)
            doc.links.append(h)
        if len(doc.links) >= 50:  # noqa: PLR2004 — витрина ссылок, не архив сайта
            break
    return doc
