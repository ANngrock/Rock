"""Исполнение исходящего действия: рендер шаблона, SSRF-guard, HTTP, журнал без секретов.

Три правила, на которых держится доверие к «бот сам дёргает API»:
1) секретные значения подставляются в шаблон только здесь и сейчас — в ``digest`` журнала
   каждое значение вырезается на ``***``, что бы сервер ни ответил;
2) URL проходит :func:`aegis.web.net.guard_url` — эндпоинт «на внутренний сервис хоста»
   не выстрелит, даже если кто-то убедит модель его добавить;
3) нет плейсхолдера — нет запроса: ошибки не «доигрываются» пустой строкой, а возвращаются
   владельцу списком недостающих ключей (значений не показываем и так).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from aegis.automation.store import EndpointRow

__all__ = ["ActionError", "ActionResult", "mask_secrets", "render_template", "run_endpoint"]

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_:.-]{1,64})\s*\}\}")
_MASK = "***"


@dataclass(slots=True)
class ActionResult:
    ok: bool
    status: int = 0
    ms: int = 0
    digest: str = ""


class ActionError(RuntimeError):
    """Действие не выполнено (не хватает переменных, url заблокирован, транспорт упал)."""


def render_template(template: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """``{{key}}`` → значения; возвращает (текст, список недостающих ключей)."""

    missing: list[str] = []

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        if key not in missing:
            missing.append(key)
        return m.group(0)

    return _PLACEHOLDER_RE.sub(sub, template), missing


def mask_secrets(text: str, secret_values: list[str]) -> str:
    """Вырезаем из любого текста все непустые секретные значения (длинные — первые)."""
    for value in sorted((v for v in secret_values if v), key=len, reverse=True):
        text = text.replace(value, _MASK)
    return text


async def run_endpoint(
    row: EndpointRow,
    secrets_map: dict[str, str],
    *,
    variables: dict[str, str] | None = None,
    client: Any = None,
    max_body_kb: int = 256,
) -> ActionResult:
    """Один вызов эндпоинта. ``client`` — инъекция для тестов; свой закрываем сами."""
    import httpx  # noqa: PLC0415

    from aegis.web.net import guard_url  # noqa: PLC0415

    values = {str(k): str(v) for k, v in (variables or {}).items()}
    values.update({f"secret:{k}": v for k, v in secrets_map.items()})
    body, miss_body = render_template(row.body_template, values)
    headers: dict[str, str] = {}
    miss_hdr: list[str] = []
    for k, v in row.headers.items():
        hv, miss = render_template(v, values)
        headers[k] = hv
        miss_hdr.extend(miss)
    missing = sorted(set(miss_body) | set(miss_hdr))
    if missing:
        raise ActionError(
            "не хватает переменных: "
            + ", ".join(f"{{{{{k}}}}}" for k in missing)
            + " — передайте их в variables или заполните секреты через aegis action add --secret"
        )
    try:
        url = guard_url(row.url)
    except ValueError as exc:
        raise ActionError(f"url заблокирован guard'ом: {exc}") from exc

    timeout_s = min(row.timeout_ms / 1000.0, 120.0)  # noqa: PLR2004 — потолок конфига
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
    started = time.monotonic()
    try:
        resp = await client.request(
            row.method,
            url,
            content=body.encode("utf-8") if body else None,
            headers=headers or None,
        )
        raw = resp.text[: max(0, int(max_body_kb)) * 1024]
        ms = int((time.monotonic() - started) * 1000)
        ok = 200 <= int(resp.status_code) < 400  # noqa: PLR2004 — диапазон успеха HTTP
        digest = f"HTTP {resp.status_code} · {ms} мс · {_one_line(raw)}"
        digest = mask_secrets(digest, [*secrets_map.values(), *values.values()])
        return ActionResult(ok=ok, status=int(resp.status_code), ms=ms, digest=digest[:900])
    except httpx.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        raise ActionError(mask_secrets(f"транспорт: {exc}", [*secrets_map.values()])) from exc
    finally:
        if own_client:
            await client.aclose()


def _one_line(text: str) -> str:
    return " ".join(text.split())[:700] or "пустое тело"
