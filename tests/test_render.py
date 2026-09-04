"""Рендер в Telegram HTML: модель делает что хочет, интерфейс не имеет права падать."""

from __future__ import annotations

import html
import re

from aegis.agents.tools.registry import (
    Attachment,  # noqa: F401  - не нужен, но проверяет импорт слоя
)
from aegis.interaction.telegram.render import (
    MAX_MESSAGE_LEN,
    chunk_html,
    render_for_telegram,
    sanitize_html,
)


def test_plain_text_survives_unchanged() -> None:
    safe, _ = sanitize_html("обычный ответ без разметки")
    assert safe == "обычный ответ без разметки"


def test_markdown_is_converted() -> None:
    safe, repaired = sanitize_html("**важно** и `код`, см. [статью](https://e.com/a)")
    assert "<b>важно</b>" in safe
    assert "<code>код</code>" in safe
    assert '<a href="https://e.com/a">статью</a>' in safe
    assert "markdown->html" in repaired


def test_unsupported_tags_are_dropped_but_text_remains() -> None:
    safe, repaired = sanitize_html("<div><b>жирный</b><script>alert(1)</script></div>")
    assert "<b>жирный</b>" in safe
    assert "alert(1)" in safe
    assert "<div>" not in safe and "<script>" not in safe
    assert any(r.startswith("dropped<") for r in repaired)


def test_link_attributes_are_whitelisted() -> None:
    safe, _ = sanitize_html('<a href="https://e.com/x" onclick="steal()" style="x">текст</a>')
    assert "onclick" not in safe and "style" not in safe
    assert 'href="https://e.com/x"' in safe


def test_javascript_href_is_stripped() -> None:
    safe, _ = sanitize_html('<a href="javascript:alert(1)">link</a>')
    assert "javascript:" not in safe


def test_unclosed_tags_are_closed_and_stray_closers_dropped() -> None:
    safe, repaired = sanitize_html("<b>открыт и не закрыт")
    assert safe.endswith("</b>")
    assert any(r.startswith("unclosed") for r in repaired)
    stray, repaired_stray = sanitize_html("текст </i> без открытия")
    assert "</i>" not in stray
    assert any(r.startswith("stray") for r in repaired_stray)


def test_angle_brackets_in_text_stay_parser_compatible() -> None:
    """Двусмысленные угловые скобки: ответ обязан остаться валидным для Telegram."""
    safe, _ = sanitize_html("если a<b и c>d")
    assert safe.count("<b>") == safe.count("</b>")
    assert "если" in safe
    stripped = re.sub(r"</?[a-zA-Z][^>]*>", "", safe)
    assert "<" not in stripped


def test_chunking_keeps_limit_and_balances_tags() -> None:
    """Разбивка обязана уложиться в лимит и не оставить открытых тегов в середине."""
    long_text = "".join(f"<b>пункт {i}</b> — достаточно длинное описание " for i in range(500))

    chunks = chunk_html(long_text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    # чанк может дооткрыть тег, но обязан его закрыть: ни одного висящего <b>
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>"), chunk
    # текст не теряется: все 500 пунктов доезжают до клиента
    assert "".join(chunks).count("пункт") == 500


def test_single_short_message_is_one_chunk() -> None:
    rendered = render_for_telegram("привет")
    assert rendered.chunks == ["привет"]


def test_chunk_boundaries_never_split_a_tag() -> None:
    text = ("текст " * 900) + "<b>ключевое</b>"
    for chunk in render_for_telegram(text).chunks:
        assert "<b\n" not in chunk
        assert not chunk.endswith("<")
        assert "<b" not in chunk or "</b>" in chunk


def test_limit_respected_for_realistic_llm_answer() -> None:
    text = "Aegis: " + "данные из инструмента; " * 1500
    rendered = render_for_telegram(text)
    assert len(rendered.chunks) > 1
    assert all(len(c) <= MAX_MESSAGE_LEN for c in rendered.chunks)
    joined = "".join(chunk.replace("<b>", "").replace("</b>", "") for chunk in rendered.chunks)
    assert "данные из инструмента" in joined
    assert html.unescape(joined).replace(" ", "") != ""
