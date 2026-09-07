"""Публичные Telegram-каналы через t.me/s — web-превью без API-ключей.

Почему t.me/s: единственный способ читать публичные каналы без входа в аккаунт — и он же
единственный, что не требует от владельца ни токенов, ни «доверь мне сессию». Приватные
каналы сюда не входят принципиально: там законный путь один — юзербот-мост (cognition),
а не самоделка поверх чужой сессии.

Формат страницы стабилен годами (классы tgme_widget_message_*), но «стабилен» не значит
«безопасно»: неизвестный макет отдаёт пустой список, а не traceback — на редизайн Telegram
система обязана отвечать «канал не читается», а не падением тика. Форварднутые сообщения
(вложенный div.tgme_widget_message) не расщепляют пост: вложенный конверт игнорируется.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

__all__ = ["TgPost", "channel_name", "page_url", "parse_channel_page", "post_id"]

_STYLE_URL_RE = re.compile(r"url\(['\"]?([^'\")]+)")


@dataclass(slots=True)
class TgPost:
    post: str  # «канал/123» — канонический id
    text: str = ""
    url: str = ""
    time: datetime | None = None
    author: str = ""
    views: str = ""
    media: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Cur:
    """Собираемый пост: буферы вместо dict — типы важнее ловкости."""

    post: str = ""
    time_raw: str = ""
    chunks: list[str] = field(default_factory=list)
    author_buf: list[str] = field(default_factory=list)
    views_buf: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def channel_name(target: str) -> str:
    """@name | https://t.me/name | t.me/s/name → name. Иначе — ValueError (вход не канал)."""
    raw = str(target).strip().lstrip("@")
    if not raw:
        raise ValueError("пустое имя канала")
    if "/" in raw or raw.startswith(("http://", "https://")) or "." in raw.split("/")[0]:
        url = raw if raw.startswith("http") else f"https://{raw}"
        split = urlsplit(url)
        # любой хост съел бы «https://filer.example/durov» как канал: берём только t.me-семейство
        if (split.hostname or "").lower() not in {"t.me", "telegram.me", "telegram.dog"}:
            raise ValueError(f"«{target}» — не адрес t.me")
        path_parts = [q for q in split.path.split("/") if q and q not in ("s", "joinchat")]
        name = path_parts[-1] if path_parts else ""
    else:
        name = raw
    name = name.lower()  # Telegram не различает регистр имён; у нас иначе дубли источников
    if not re.fullmatch(r"[a-z][a-z0-9_]{3,63}", name):
        raise ValueError(f"«{target}» не похоже на имя публичного канала t.me")
    return name


def post_id(post: str) -> int | None:
    tail = str(post).rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def page_url(name: str, *, after: int | None = None, before: int | None = None) -> str:
    """?after=X — сообщения свежее X (пагинация вперёд); до X — ?before."""
    base = f"https://t.me/s/{name}"
    if after is not None:
        return f"{base}?after={int(after)}"
    if before is not None:
        return f"{base}?before={int(before)}"
    return base


class _WidgetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[_Cur] = []
        self._cur: _Cur | None = None
        self._post_depth = 0
        self._depth = 0
        self._text_depth = 0  # 0 = вне текста поста
        self._author_depth = 0
        self._views = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = set(str(a.get("class") or "").split())
        cur = self._cur
        if cur is not None:
            if tag == "time":
                dt = a.get("datetime")
                if dt and not cur.time_raw:
                    cur.time_raw = str(dt)
            elif tag == "br" and self._text_depth:
                cur.chunks.append("\n")
            elif tag == "span" and "tgme_widget_message_views" in classes:
                self._views = True
            elif tag == "a" and a.get("href"):
                self._absorb_link(str(a["href"]), str(a.get("style") or ""), classes)
        if tag != "div":
            return
        self._depth += 1
        if "tgme_widget_message" in classes and cur is None:
            self._post_depth = self._depth
            self._cur = _Cur(post=str(a.get("data-post") or ""))
            return
        if self._cur is None:
            return
        if "tgme_widget_message_text" in classes and not self._text_depth:
            self._text_depth = self._depth
        elif "tgme_widget_message_author" in classes and not self._author_depth:
            self._author_depth = self._depth

    def handle_data(self, data: str) -> None:
        cur = self._cur
        if cur is None:
            return
        if self._text_depth:
            cur.chunks.append(data)
        elif self._author_depth:
            cur.author_buf.append(data)
        elif self._views:
            cur.views_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._views = False
            return
        if tag != "div" or self._cur is None:
            return
        if self._text_depth == self._depth:
            self._text_depth = 0
        if self._author_depth == self._depth:
            self._author_depth = 0
        if self._post_depth == self._depth:
            self._flush()
        self._depth = max(0, self._depth - 1)

    def _absorb_link(self, href: str, style: str, classes: set[str]) -> None:
        cur = self._cur
        assert cur is not None
        if {"tgme_widget_message_photo_wrap", "tgme_widget_message_video"} & classes:
            m = _STYLE_URL_RE.search(style)
            cur.media.append(m.group(1) if m else href)
        elif "tgme_widget_message_document" in classes:
            cur.media.append(href)
        elif self._text_depth:
            cur.links.append(href)

    def _flush(self) -> None:
        cur, self._cur = self._cur, None
        self._post_depth = 0
        self._text_depth = self._author_depth = 0
        self._views = False
        if cur is not None:
            self.posts.append(cur)


def parse_channel_page(html: str, channel: str) -> list[TgPost]:
    """Страница t.me/s/<channel> → посты по возрастанию id. Пустой список — страница не похожа."""
    parser = _WidgetParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — редизайн Telegram обязан стоить «пусто», не traceback
        return []
    if parser._cur is not None:  # хвост без закрытия (обрезанный ответ) — тоже пост
        parser._flush()
    seen: set[str] = set()
    out: list[TgPost] = []
    for cur in parser.posts:
        pid = cur.post
        if not pid or pid in seen:
            continue
        seen.add(pid)
        full = pid if "/" in pid else f"{channel}/{pid}"
        text = re.sub(r"[ \t]+", " ", "".join(cur.chunks)).strip()[:20_000]
        when: datetime | None = None
        if cur.time_raw:
            try:
                when = datetime.fromisoformat(cur.time_raw)
            except ValueError:
                when = None
        out.append(
            TgPost(
                post=full,
                text=text,
                url=f"https://t.me/{full}" if post_id(full) else "",
                time=when,
                author=" ".join("".join(cur.author_buf).split())[:120],
                views=" ".join("".join(cur.views_buf).split())[:20],
                media=list(dict.fromkeys(cur.media))[:12],
                links=list(dict.fromkeys(cur.links))[:20],
            )
        )
    out.sort(key=lambda p: post_id(p.post) or 0)
    return out
