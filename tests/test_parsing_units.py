"""Юниты парсера: виджет t.me, ленты, дедуп, иголки — всё без сети и без БД.

Сеть здесь подменяет fixture (тот же HTML, что живой виджет), БД — не нужна вовсе.
Живой провод проверяет integration-тест и ручной e2e; этот файл сторожит разбор:
именно он ловит «Telegram переверстал карточку, и канал молча читается пустым».
"""

from __future__ import annotations

import json
import pathlib

from aegis.parsing.dedup import canonical_url, fingerprint
from aegis.parsing.feeds import detect_feed, parse_feed, parse_jsonfeed
from aegis.parsing.telegram import channel_name, page_url, parse_channel_page, post_id

REPO = pathlib.Path(__file__).resolve().parents[1]


def _widget(post: str, text: str, *, day: int = 7, views: str = "1 000") -> str:
    return (
        '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message js-widget_message" data-post="{post}">'
        '<div class="tgme_widget_message_author"><a class="tgme_widget_message_owner_name">'
        "Автор</a></div>"
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
        f'<a href="https://t.me/{post}" class="tgme_widget_message_photo_wrap" '
        "style=\"background-image:url('https://cdn1.telesco.pe/file/img.jpg')\"></a>"
        f'<time class="tgme_widget_message_date" datetime="2026-09-0{day}T15:00:00+00:00">x</time>'
        f'<span class="tgme_widget_message_views">{views}</span>'
        "</div></div>"
    )


def _page(*posts: str) -> str:
    return (
        "<html><body><div class='tgme_channel_history js-message_history'>"
        + "".join(posts)
        + "</div></body></html>"
    )


class TestTelegramWidget:
    def test_channel_name_variants(self) -> None:
        assert channel_name("@durov") == "durov"
        assert channel_name("https://t.me/durov") == "durov"
        assert channel_name("t.me/s/durov/") == "durov"
        assert channel_name("DUROV") == "durov"

    def test_channel_name_rejects_garbage(self) -> None:
        for bad in ("https://example.com/durov", "@a", "@too", "@дюров", "http://x/", "t.me/"):
            try:
                channel_name(bad)
            except ValueError:
                continue
            raise AssertionError(f"должно отвергать {bad!r}")

    def test_posts_full_parse(self) -> None:
        posts = parse_channel_page(
            _page(_widget("durov/2", "второй<br>текст"), _widget("durov/1", "первый")), "durov"
        )
        assert [post_id(p.post) for p in posts] == [1, 2]
        p2 = posts[1]
        assert p2.text == "второй\nтекст"
        assert p2.views == "1 000"
        assert p2.time is not None and p2.time.day == 7
        assert p2.media == ["https://cdn1.telesco.pe/file/img.jpg"]

    def test_forward_envelope_does_not_split_post(self) -> None:
        body = (
            '<div class="tgme_widget_message_wrap">'
            '<div class="tgme_widget_message" data-post="c/9">'
            '<div class="tgme_widget_message_forwarded_from_name">оригинал</div>'
            '<div class="tgme_widget_message_text">тело&nbsp;с &lt;тегом&gt;</div>'
            "</div></div>"
        )
        posts = parse_channel_page(body, "c")
        assert len(posts) == 1
        assert "оригинал" not in posts[0].text
        assert " ".join(posts[0].text.split()) == "тело с <тегом>"

    def test_unknown_layout_is_empty_not_exception(self) -> None:
        assert parse_channel_page("<html><body><p>редизайн</p></body></html>", "x") == []
        assert parse_channel_page("", "x") == []

    def test_page_url_pagination(self) -> None:
        assert page_url("durov") == "https://t.me/s/durov"
        assert "after=9" in page_url("durov", after=9)
        assert "before=1" in page_url("durov", before=1)


class TestFeeds:
    def test_rss(self) -> None:
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
        <item><title>Раз</title><link>https://e/1</link>
        <description>&lt;b&gt;html&lt;/b&gt; текст</description>
        <pubDate>Mon, 07 Sep 2026 04:00:00 GMT</pubDate></item></channel></rss>"""
        items = parse_feed(xml)
        assert len(items) == 1
        assert items[0].title == "Раз"
        assert items[0].text == "html текст"
        assert items[0].published_at is not None and items[0].published_at.hour == 4

    def test_atom_namespaced(self) -> None:
        xml = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Два</title><link href="https://e/2"/><summary>кратко</summary>
        <updated>2026-09-07T06:07:08Z</updated></entry></feed>"""
        items = parse_feed(xml)
        assert items[0].title == "Два"
        assert items[0].url == "https://e/2"
        assert items[0].published_at is not None and items[0].published_at.minute == 7

    def test_jsonfeed(self) -> None:
        raw = json.dumps(
            {
                "version": "https://jsonfeed.org/version/1",
                "title": "J",
                "items": [
                    {"id": "1", "url": "https://e/1", "title": "Три", "content_text": "тело"}
                ],
            }
        )
        items = parse_jsonfeed(raw)
        assert items[0].title == "Три" and items[0].text == "тело"

    def test_detect(self) -> None:
        assert detect_feed("application/rss+xml", "") == "feed"
        assert detect_feed("text/html", '<?xml version="1.0"?><rss><channel>') == "feed"
        assert (
            detect_feed(
                "application/json", '{"version":"https://jsonfeed.org/version/1","items":[]}'
            )
            == "jsonfeed"
        )
        assert detect_feed("text/html", "<html><body>просто страница") == ""

    def test_entity_bomb_refused(self) -> None:
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "'
            + "x" * 300
            + '">]><rss><item><title>&a;</title></item></rss>'
        )
        try:
            parse_feed(bomb)
        except ValueError:
            return
        raise AssertionError("сущностная бомба обязана отбиваться до парсера")


class TestDedup:
    def test_trackers_stripped(self) -> None:
        a = canonical_url("https://e.ru/p?utm_source=tg&mc_id=5#frag")
        b = canonical_url("https://e.ru/p")
        assert a == b == "https://e.ru/p"

    def test_clid_stripped(self) -> None:
        assert canonical_url("https://e.ru/p?yclid=99&gclid=1") == "https://e.ru/p"

    def test_fingerprint_whitespace_insensitive(self) -> None:
        f1 = fingerprint(url="https://e/1", title="Заголовок  тут", text="абв\n гд")
        f2 = fingerprint(url="https://e/1", title="Заголовок тут", text="абв гд")
        assert f1 == f2
        f3 = fingerprint(url="https://e/1", title="Другой", text="абв гд")
        assert f3 != f1


class TestFilter:
    def test_needles(self) -> None:
        from aegis.parsing.engine import _filter
        from aegis.parsing.store import SourceRow

        def draft(title: str, text: str = "") -> object:
            from aegis.parsing.engine import ItemDraft

            return ItemDraft(
                fingerprint="f", title=title, url="u", text=text, excerpt="", author=""
            )

        src = SourceRow(
            id="0",
            owner_id=1,
            kind="web",
            target="t",
            label="",
            interval_sec=60,
            include_kw="криптовалют",
            exclude_kw="спонсор",
        )
        out = _filter(
            [
                draft("новости про Криптовалюты"),
                draft("спонсор: криптовалюты", "спонсорская реклама"),
                draft("погода"),
            ],
            src,
        )
        assert [d.title for d in out] == ["новости про Криптовалюты"]


class TestVaultVectorsCrossLanguage:
    """Python обязан открывать то, что запечатал мини-апп (тот же файл, что читает node --test)."""

    def test_open_ts_seals(self) -> None:
        import base64

        from aegis.platform.crypto import BlobCipher
        from aegis.platform.vault import derive_kek, open_text

        vectors = json.loads((REPO / "miniapp" / "testdata" / "vault_vectors.json").read_text())
        master = base64.b64decode(vectors["master_kek_b64"])
        gen = max(int(c["gen"]) for c in vectors["cases"])
        ring = {v: derive_kek(master, v) for v in range(1, gen + int(vectors["keep"]) + 1)}
        cipher = BlobCipher(ring, active_version=gen)
        for case in vectors["cases"]:
            assert open_text(cipher, case["ts_sealed"]) == case["plaintext"]
            assert open_text(cipher, case["py_sealed"]) == case["plaintext"]
