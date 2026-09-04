"""Рендер ответа модели в Telegram HTML.

Модель обязана отвечать Telegram-разметкой (правило 7 системного промпта), но обязать её нельзя:
в ответе встречаются Markdown-звёздочки, незакрытые ``<b>``, ``<code>`` без закрывающего тега.
Telegram отвечает на такое ``400 Bad Request``, и владелец видит ошибку вместо ответа.

Перед отправкой:

1. Markdown-частотности (``**bold**``, `` `code` ``, `` [текст](url) ``) переводятся в HTML;
2. неподдерживаемые теги вырезаются (их содержимое остаётся), у ``<a>`` сохраняется только
   ``href`` с безопасной схемой — никакой инъекции ``onclick``/``style``;
3. незакрытые теги дозакрываются, лишние закрывающие выбрасываются;
4. длинный текст режется по абзацам, открытая разметка закрывается в конце чанка и переоткрывается
   в начале следующего — иначе обрыв тегов ломает сообщение.

Функция детерминирована и не трогает содержимое: только разметку. Ей можно доверять в тестах.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

__all__ = [
    "MAX_MESSAGE_LEN",
    "RenderedMessage",
    "chunk_html",
    "render_for_telegram",
    "sanitize_html",
    "strip_tags",
]

MAX_MESSAGE_LEN = 4096  # лимит sendMessage

_ALLOWED = frozenset(
    {"b", "i", "u", "s", "span", "tg-spoiler", "code", "pre", "blockquote", "a", "br"}
)
_VOID = frozenset({"br"})
_ANY_TAG = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^<>]*)?)(/?)\s*>")
_ATTR = re.compile(r"""([a-zA-Z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_SAFE_URL = re.compile(r"^\s*(?:https?://|mailto:|tg://)[^\s\"'<>]*$", re.I)
_MD_BOLD = re.compile(r"\*\*(?P<t>.+?)\*\*", re.S)
_MD_ITALIC = re.compile(r"(?<!\*)\*(?P<t>[^*\n]+)\*(?!\*)")
_MD_CODE = re.compile(r"(?<!`)`(?P<t>[^`\n]+)`(?!`)")
_MD_LINK = re.compile(r"\[(?P<t>[^\]]+)\]\((?P<u>[^)\s]+)\)")


@dataclass(slots=True)
class RenderedMessage:
    """Готовые к отправке кусцы + служебная информация для логов."""

    chunks: list[str]
    repaired: list[str] = field(default_factory=list)

    @property
    def is_plain(self) -> bool:
        return len(self.chunks) == 1 and not self.repaired


def _md_to_html(text: str) -> str:
    """Частичный перевод Markdown, которым модель всё равно продолжает писать."""
    out = _MD_CODE.sub(r"<code>\g<t></code>", text)
    out = _MD_BOLD.sub(r"<b>\g<t></b>", out)
    out = _MD_ITALIC.sub(r"<i>\g<t></i>", out)
    return _MD_LINK.sub(lambda m: f'<a href="{m.group("u")}">{m.group("t")}</a>', out)


def _clean_attrs(raw: str, tag: str) -> str:
    if tag != "a":
        return ""
    for match in _ATTR.finditer(raw or ""):
        if match.group(1).lower() != "href":
            continue
        value = next((g for g in match.groups()[1:] if g), "")
        if _SAFE_URL.match(value):
            return f' href="{html.escape(value.strip(), quote=True)}"'
    return ""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sanitize_html(text: str) -> tuple[str, list[str]]:
    """-> (безопасный HTML, список починки). Второй аргумент полезен для логов/алертов."""
    repaired: list[str] = []
    converted = _md_to_html(text)
    if converted != text:
        repaired.append("markdown->html")
    out: list[str] = []
    stack: list[tuple[str, str]] = []  # (tag, attrs)
    pos = 0
    for match in _ANY_TAG.finditer(converted):
        out.append(_escape(converted[pos : match.start()]))
        pos = match.end()
        closing, tag, attrs, self_closing = (
            match.group(1) == "/",
            match.group(2).lower(),
            match.group(3),
            match.group(4) == "/",
        )
        if tag not in _ALLOWED:
            repaired.append(f"dropped<{tag}>")
            continue
        if attrs and tag != "a" and not closing:
            # «если a<b и c>d» — это текст про сравнения, а не тег с атрибутами: показываем как
            # есть, вместо того чтобы съесть «и c» и оставить висящий </b>
            out.append(_escape(match.group(0)))
            repaired.append(f"text<{tag}>")
            continue
        if tag in _VOID:
            out.append("<br>" if not closing else "")
            continue
        if closing:
            if stack and stack[-1][0] == tag:
                stack.pop()
                out.append(f"</{tag}>")
            else:
                repaired.append(f"stray</{tag}>")
            continue
        if self_closing:
            continue
        clean_attrs = _clean_attrs(attrs, tag)
        if tag == "a" and " href=" not in clean_attrs:
            repaired.append("link-without-href")
        out.append(f"<{tag}{clean_attrs}>")
        stack.append((tag, clean_attrs))
    out.append(_escape(converted[pos:]))
    body = "".join(out)
    if stack:
        body += "".join(f"</{tag}>" for tag, _ in reversed(stack))
        repaired.append(f"unclosed<{stack[-1][0]}>")
    return body, repaired


def _scan_open(fragment: str) -> list[tuple[str, str]]:
    """Теги, оставленные фрагментом открытыми (в порядке вложенности)."""
    stack: list[tuple[str, str]] = []
    for match in _ANY_TAG.finditer(fragment):
        closing, tag, attrs, self_closing = (
            match.group(1) == "/",
            match.group(2).lower(),
            match.group(3),
            match.group(4) == "/",
        )
        if tag not in _ALLOWED or tag in _VOID:
            continue
        if closing:
            if stack and stack[-1][0] == tag:
                stack.pop()
        elif not self_closing:
            stack.append((tag, _clean_attrs(attrs, tag)))
    return stack


def _cut_point(text: str, budget: int) -> int:
    if len(text) <= budget:
        return len(text)
    window = text[:budget]
    for sep in ("\n\n", "\n", ". ", "; ", " "):
        idx = window.rfind(sep)
        if idx > budget * 0.35:
            return idx + len(sep)
    return budget


def chunk_html(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Разбить на чанки ≤ limit, не разрывая теги и URL; разметка переносится между чанками.

    Лимит считаем по итоговому чанку: префикс воссозданных тегов и обязательные докрытия
    ``</tag>`` занимают место. Иначе «длинный абзац внутри <b>» молча даёт чанк на 520
    символов и Telegram отвечает 400 — ровно там, где рендер и обязан спасать.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    carried: list[tuple[str, str]] = []
    rest = text
    while rest:
        prefix = "".join(f"<{tag}{attrs}>" for tag, attrs in carried)
        cut = _cut_point(rest, max(limit - len(prefix), 200))
        piece = rest[:cut]
        open_after: list[tuple[str, str]] = []
        # шаг за шагом укорачиваем, пока чанк с докрытиями не влезет (сходится за 2–3 итерации)
        for _ in range(8):
            while cut > 0 and cut < len(rest) and _inside_tag(rest, cut):
                cut -= 1  # граница реза не должна попадать внутрь тега/URL
            piece = rest[:cut]
            open_after = _scan_open(prefix + piece)
            suffix = "".join(f"</{tag}>" for tag, _ in reversed(open_after))
            overflow = len(prefix) + len(piece) + len(suffix) - limit
            if overflow <= 0 or cut <= 1:
                break
            cut -= min(overflow, max(cut // 4, 1))
            cut = max(cut, 1)
        suffix = "".join(f"</{tag}>" for tag, _ in reversed(open_after))
        chunks.append(prefix + piece + suffix)
        rest = rest[cut:]
        carried = open_after
    return [c for c in chunks if c.strip()]


def _inside_tag(text: str, pos: int) -> bool:
    """True, если позиция попадает внутрь угловой скобки тега (граница реза недопустима)."""
    return text.rfind("<", 0, pos) > text.rfind(">", 0, pos)


def strip_tags(text: str) -> str:
    """Разметку в обычный текст: ``<br>`` -> перевод строки, содержимое сохраняется.

    Нужен для запасного пути отправки: экранировать HTML нельзя — владелец получит ``<code>``
    вместо ответа, а молча потерять сообщение тем более нельзя.
    """
    converted = _md_to_html(text)
    out: list[str] = []
    pos = 0
    for match in _ANY_TAG.finditer(converted):
        out.append(converted[pos : match.start()])
        pos = match.end()
        if match.group(2).lower() == "br" and match.group(1) != "/":
            out.append("\n")
    out.append(converted[pos:])
    return re.sub(r"\n{3,}", "\n\n", html.unescape("".join(out))).strip()


def render_for_telegram(text: str, *, limit: int = MAX_MESSAGE_LEN) -> RenderedMessage:
    """Главная функция слоя: безопасные чанки для ``sendMessage(parse_mode=HTML)``."""
    safe, repaired = sanitize_html(text)
    return RenderedMessage(chunks=chunk_html(safe, limit=limit), repaired=repaired)
