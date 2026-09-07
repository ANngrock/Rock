"""Карманный разработчик: hash/base64/uuid/slug/json/stats — вместо вкладки в браузере.

Один инструмент с полем ``action`` вместо десяти: модели проще выбрать действие из
перечисления, чем запомнить десять имён; регистрация в реестре от этого только чище.
Никакой сетевой активности и никакой исполняемой магии — только детерминированные
преобразования текста, записанные здесь же функциями (тестируются без LLM).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
import uuid
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk

__all__ = ["ACTIONS", "devbox_transform"]

_MAX_INPUT = 20_000
ACTIONS = (
    "hash",
    "b64e",
    "b64d",
    "url",
    "unurl",
    "uuid",
    "slug",
    "case",
    "json",
    "stats",
    "ts",
    "escape",
)


def devbox_transform(action: str, text: str, arg: str = "") -> str:
    """Чистая функция преобразования. Ошибки — ValueError с человеческим текстом."""
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"action обязан быть одним из {', '.join(ACTIONS)}")
    text = text or ""
    if len(text) > _MAX_INPUT:
        raise ValueError(f"вход длиннее {_MAX_INPUT} символов — не почта, а файл какой-то")
    if action == "uuid":
        return str(uuid.uuid4())
    if action == "hash":
        algo = (arg or "sha256").lower()
        try:
            digest = hashlib.new(algo, text.encode("utf-8"))
        except ValueError:
            supported = "sha256, sha1, md5, blake2b, sha512"
            raise ValueError(f"алгоритм «{algo}»? есть: {supported}") from None
        return f"{algo}:{digest.hexdigest()}"
    if action == "b64e":
        raw = base64.b64encode(text.encode("utf-8")).decode("ascii")
        if arg.strip().lower() == "url":
            return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
        return raw
    if action == "b64d":
        compact = "".join(text.split())
        pad = "=" * (-len(compact) % 4)
        try:
            data = base64.urlsafe_b64decode(compact + pad)
        except (binascii.Error, ValueError):
            raise ValueError("это не base64") from None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return f"<бинарные {len(data)} байт, hex>\n{data[:512].hex()}"
    if action == "url":
        return quote(text, safe=arg or "")
    if action == "unurl":
        return unquote(text)
    if action == "escape":
        return html.escape(text, quote=True)
    if action == "slug":
        slug = re.sub(r"[^\w]+", "-", text.lower(), flags=re.UNICODE).strip("-")
        cyr = {
            "а": "a",
            "б": "b",
            "в": "v",
            "г": "g",
            "д": "d",
            "е": "e",
            "ё": "e",
            "ж": "zh",
            "з": "z",
            "и": "i",
            "й": "y",
            "к": "k",
            "л": "l",
            "м": "m",
            "н": "n",
            "о": "o",
            "п": "p",
            "р": "r",
            "с": "s",
            "т": "t",
            "у": "u",
            "ф": "f",
            "х": "kh",
            "ц": "ts",
            "ч": "ch",
            "ш": "sh",
            "щ": "sch",
            "ъ": "",
            "ы": "y",
            "ь": "",
            "э": "e",
            "ю": "yu",
            "я": "ya",
        }
        if any(ch in cyr for ch in slug):
            slug = "".join(cyr.get(ch, ch) for ch in slug)
            slug = re.sub(r"[^\w]+", "-", slug.lower()).strip("-")
        return slug or "— пусто —"
    if action == "case":
        mode = (arg or "lower").strip().lower()
        words = re.findall(r"[^\W\d_]+|\d+", text, flags=re.UNICODE)
        if mode == "upper":
            return text.upper()
        if mode == "lower":
            return text.lower()
        if mode == "title":
            return text.title()
        if mode == "snake":
            return "_".join(w.lower() for w in words)
        if mode == "kebab":
            return "-".join(w.lower() for w in words)
        if mode == "camel":
            return (words[0].lower() + "".join(w.capitalize() for w in words[1:])) if words else ""
        if mode == "pascal":
            return "".join(w.capitalize() for w in words)
        raise ValueError("case: upper|lower|title|snake|kebab|camel|pascal")
    if action == "json":
        mode = (arg or "pretty").strip().lower()
        try:
            data = json.loads(text)
        except ValueError as exc:
            return f"не JSON: {exc}"
        if mode == "min" or mode == "compact":
            return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if mode == "keys":
            return ", ".join(sorted(_json_keys(data)))
        return json.dumps(data, ensure_ascii=False, indent=2)
    if action == "stats":
        chars = len(text)
        n_words = len(text.split())
        lines = text.count("\n") + 1
        readable_min = round(n_words / 200, 1) if n_words else 0
        return (
            f"символов {chars} · слов {n_words} · строк {lines} · "
            f"длинных строк (>120): {sum(1 for ln in text.splitlines() if len(ln) > 120)} · "
            f"чтение ~{readable_min} мин"
        )
    # ts
    stripped = text.strip()
    if re.fullmatch(r"-?\d{1,12}(\.\d+)?", stripped):
        ts = float(stripped)
        if abs(ts) > 1e11:
            ts = ts / 1000.0
        local = datetime.fromtimestamp(ts)
        return (
            f"unix {ts:.0f} = {local:%Y-%m-%d %H:%M:%S} локальное · "
            f"{datetime.fromtimestamp(ts, UTC):%Y-%m-%d %H:%M:%S} UTC"
        )
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d",
    ):
        try:
            moment = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        return f"{moment:%Y-%m-%d %H:%M:%S} → unix {int(moment.timestamp())}"
    raise ValueError(
        "не понял дату: жду unix-секунды/миллисекунды или 2026-09-07 08:00 / 07.09.2026"
    )


def _json_keys(data: object, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            keys.add(path)
            keys |= _json_keys(v, path)
    elif isinstance(data, list):
        for v in data[:25]:
            keys |= _json_keys(v, prefix)
    return keys


class DevboxArgs(BaseModel):
    action: str = Field(description=", ".join(ACTIONS))
    input: str = Field(default="", max_length=_MAX_INPUT, description="текст для преобразования")
    arg: str = Field(
        default="",
        max_length=40,
        description=(
            "уточнение: для hash — алгоритм (sha256…); для case — режим (snake/kebab/camel/"
            "pascal/title/upper/lower); для json — pretty|min|keys; для b64e — url; для url — "
            "сохранить символы (по умолчанию пуст)"
        ),
    )


@registry.register(
    "devbox",
    "Карманные утилиты разработчика: hash (sha256/md5/…), base64 encode/decode, url-encode/"
    "decode, uuid4, slug (в т.ч. с кириллицы), смена регистра (snake/kebab/camel/…), разбор "
    "JSON (pretty/min/keys), статистика текста (символы/слова/строки), перевод unix-"
    "timestamp↔дата. Детерминированно, без интернета.",
    DevboxArgs,
    risk=Risk.NONE,
)
async def devbox(args: DevboxArgs, ctx: ToolContext) -> str:  # noqa: ARG001
    try:
        out = devbox_transform(args.action, args.input, args.arg)
    except ValueError as exc:
        return f"Не вышло: {exc}"
    if len(out) > 8000:
        out = out[:8000] + "\n…обрезано"
    return out or "— пусто —"
