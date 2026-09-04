"""DLP: маскирование PII до того, как текст покинет периметр (принцип 3).

Маскирование обратимо в рамках одного запроса: модель получает токены ``<AEGIS_PII:CARD:1>``,
а результат разворачивается обратно перед показом владельцу. Так LLM «понимает» структуру
(номер карты, телефон, почта), но конкретные значения не уходят провайдеру и не оседают в его
логах/кеше.

Обратимость гарантируется даже для текста, который сам содержит похожие токены: они
предварительно экранируются, поэтому `unmask` — строго `mask`-инверсия. Проверено
property-тестом на произвольных строках (tests/test_dlp.py).
"""

from __future__ import annotations

import re

__all__ = ["DLP", "PATTERNS"]

# Токен-заполнитель. Формат фиксируем здесь же, чтобы экранирование и генерация не разъехались.
_KIND_RX = "AEGIS_PII"
_TOKEN_RX = re.compile(rf"<{_KIND_RX}:([A-Z]+):(\d+)>")

# Порядок важен: сначала более специфичные длинные последовательности (карта), потом короткие.
PATTERNS: dict[str, re.Pattern[str]] = {
    "CARD": re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
    "PHONE": re.compile(
        r"(?<!\d)(?:\+7|8)[\s(.-]?\d{3}[\s).-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)"
    ),
    "EMAIL": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # серия+номер: «4509123456», «45 09 123456», «4509 123456»
    "PASSPORT": re.compile(r"\b\d{2}[ -]?\d{2}[ -]?\d{6}\b"),
}


class DLP:
    """Маскировщик PII. Потокобезопасен: не хранит состояние между вызовами."""

    def __init__(self, patterns: dict[str, re.Pattern[str]] | None = None) -> None:
        self._patterns = patterns or PATTERNS

    def mask(self, text: str) -> tuple[str, dict[str, str]]:
        if not text:
            return text, {}
        mapping: dict[str, str] = {}

        def new_token(kind: str, value: str) -> str:
            token = f"<{_KIND_RX}:{kind}:{len(mapping) + 1}>"
            mapping[token] = value
            return token

        # 1. Экранируем уже готовые токены: иначе unmask подменил бы чужую строку.
        def escape(match: re.Match[str]) -> str:
            return new_token("RAW", match.group(0))

        out = _TOKEN_RX.sub(escape, text)

        # 2. Маскируем PII.
        for kind, rx in self._patterns.items():

            def repl(match: re.Match[str], kind: str = kind) -> str:
                return new_token(kind, match.group(0))

            out = rx.sub(repl, out)
        return out, mapping

    def unmask(self, text: str, mapping: dict[str, str]) -> str:
        if not mapping or not text:
            return text
        # Порядок — обратный созданию: RAW-токены разворачиваем последними, чтобы
        # восстановленные из них «чужие» токены не подменялись повторно.
        for token in reversed(list(mapping)):
            text = text.replace(token, mapping[token])
        return text

    def scan(self, text: str) -> dict[str, int]:
        """Что и в каком количестве уйдёт наружу — для алертов/аудита."""
        found: dict[str, int] = {}
        for kind, rx in self._patterns.items():
            n = len(rx.findall(text))
            if n:
                found[kind] = n
        return found

    def redact(self, text: str) -> str:
        """Однонаправленное вырезание — для логов и публичных мест, где восстанавливать не нужно."""
        out = text
        for rx in self._patterns.values():
            out = rx.sub("[redacted]", out)
        return out
