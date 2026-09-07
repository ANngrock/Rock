"""Отпечатки и нормализация: «этот предмет уже был» должно решаться детерминированно.

Fingerprint считается по нормализованному (url-каноном + заголовок + текст) — перепубликация
ленты с новым `<guid isPermaLink>` или utm-допиской не рождает «новость». Для tg-страниц
url-канал один (пост = id), так что стабилен и сам по себе.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

__all__ = ["canonical_url", "fingerprint"]

_TRACKER_PREFIXES = ("utm_", "mc_", "pk_", "_ga")
_TRACKER_SUFFIXES = ("clid",)  # fbclid, gclid, yclid, ttclid — «клики» аналитики
_TRACKER_EXACT = frozenset({"ref", "referer", "source", "igshid"})
_WS = re.compile(r"\s+")


def canonical_url(url: str) -> str:
    """Срезает трекерные параметры и мусор хвоста; ниже и без слэша на конце."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = []
    for seg in parts.query.split("&"):
        if not seg:
            continue
        key = seg.split("=", 1)[0].lower()
        if (
            key in _TRACKER_EXACT
            or key.startswith(_TRACKER_PREFIXES)
            or key.endswith(_TRACKER_SUFFIXES)
        ):
            continue
        query.append(seg)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "&".join(query), ""))


def fingerprint(*, url: str, title: str, text: str) -> str:
    """sha256 по канону url + схлопнутому тексту: стабильный против вёрстки и переносов."""
    norm = (
        " ".join((title or "").split()).lower() + "\u0000" + " ".join((text or "").split()).lower()
    )
    body = canonical_url(url) + "\u0000" + norm
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
