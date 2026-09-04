"""ModelGateway — все LLM-вызовы только через него (ADR-002).

Ответственность одного вызова ``chat``:

1. бюджетный gate (до запроса) и учёт стоимости (после);
2. деградация по уровню затрат (thinking off → fast-модель);
3. DLP-маскирование строкового содержимого сообщений + размаскирование ответа;
4. retry с экспоненциальным backoff на retryable-ошибки и переход на fallback-провайдер;
5. запись каждого attempt (успех/провал, токены, латентность) в аудит — «всё воспроизводимо».

Бизнес-решений здесь нет: какие инструменты доступны и что делать с tool_calls — дело
:mod:`aegis.agents.supervisor`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import openai
import structlog
from openai import AsyncOpenAI
from pydantic import SecretStr

from aegis.platform.config import Settings
from aegis.platform.gateway.cost import CostGovernor
from aegis.platform.gateway.dlp import DLP
from aegis.platform.gateway.models import (
    ChatRole,
    ModelSpec,
    resolve_spec,
    spec_for_name,
)

__all__ = ["ChatResult", "LLMCallRecord", "ModelGateway", "ModelUnavailable", "ToolCall"]

log = structlog.get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class ModelUnavailable(RuntimeError):
    """Ни primary, ни fallback не ответили. Верхний слой обязан деградировать, а не падать.

    ``cause`` хранит последнюю сырую ошибку транспорта. Она не идёт наружу как есть (там
    бывают URL и заголовки) — её классифицирует :func:`aegis.platform.gateway.diagnose`.
    """

    def __init__(self, message: str, *, cause: str = "") -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall]
    model: str
    role: ChatRole
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    raw_message: dict[str, Any]
    dlp_map: dict[str, str] = field(default_factory=dict)
    truncated: bool = False


@dataclass(slots=True)
class LLMCallRecord:
    call_id: str
    role: str
    model: str
    trace_id: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    ok: bool
    error: str | None = None
    provider: str = "primary"
    attempt: int = 0


Recorder = Callable[[LLMCallRecord], Awaitable[None]]


async def _null_recorder(record: LLMCallRecord) -> None:
    return None


class ModelGateway:
    def __init__(
        self,
        cfg: Settings,
        cost: CostGovernor,
        recorder: Recorder | None = None,
        dlp: DLP | None = None,
    ) -> None:
        self.cfg = cfg
        self.cost = cost
        self.recorder = recorder or _null_recorder
        self.dlp = dlp or DLP()
        self.primary = self._client(cfg.glm_api_key, cfg.glm_base_url)
        #: Резерв включается и одним FALLBACK_MODEL: ключ и адрес по умолчанию основные.
        #  Просить дублировать секрет в .env ради «платной запасной модели» — значит собирать
        #  лишнюю копию ключа там, где он не нужен.
        fallback_on = bool(cfg.fallback_model) or bool(
            cfg.fallback_api_key and cfg.fallback_base_url
        )
        self.fallback: AsyncOpenAI | None = (
            self._client(
                cfg.fallback_api_key or cfg.glm_api_key,
                cfg.fallback_base_url or cfg.glm_base_url,
            )
            if fallback_on
            else None
        )
        #: Отдельный клиент для эмбеддингов, когда чат-роутер их не проксирует. None = основной
        #: эндпоинт: у роутеров embeddings есть не везде, а за векторами иногда нужно ходить
        #: напрямую к провайдеру — для этого EMBED_BASE_URL/EMBED_API_KEY.
        self.embed_client: AsyncOpenAI | None = (
            self._client(
                cfg.embed_api_key or cfg.glm_api_key,
                cfg.embed_base_url or cfg.glm_base_url,
            )
            if cfg.embed_base_url or cfg.embed_api_key
            else None
        )

    # ---------------- public ----------------

    async def chat(
        self,
        role: ChatRole,
        messages: Sequence[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        thinking: bool = False,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        trace_id: str | None = None,
    ) -> ChatResult:
        trace = trace_id or str(uuid.uuid4())
        spec = resolve_spec(role, self.cfg)
        masked, dlp_map = self._mask_messages([dict(m) for m in messages])
        await self.cost.check(self._estimate(masked, spec, max_tokens))

        level = self.cost.degradation_level(await self.cost.spent())
        thinking = thinking and level < 1
        if level >= 2 and role == "brain":
            spec = resolve_spec("fast", self.cfg)
            log.info("gateway.degraded_to_fast", budget_level=level, trace_id=trace)

        base_kwargs: dict[str, Any] = {"messages": masked, "temperature": temperature}
        if tools:
            base_kwargs["tools"] = tools
            base_kwargs["tool_choice"] = "auto"
        if response_format:
            base_kwargs["response_format"] = response_format
        if max_tokens:
            base_kwargs["max_tokens"] = max_tokens

        attempt = 0
        last_error: str | None = None
        for provider, client in self._providers():
            # у fallback своё имя модели — и свой ценник: primary может быть бесплатным, а
            # резерв платным, и учёт «по цене роли» занижал бы расходы до нуля (SLO по
            # стоимости при этом выглядел бы соблюдённым)
            spec_used = spec
            model_name = spec.name
            if provider == "fallback" and self.cfg.fallback_model:
                model_name = self.cfg.fallback_model
                spec_used = spec_for_name(model_name, spec)
            kwargs: dict[str, Any] = {**base_kwargs, "model": model_name}
            # нестандартные параметры (thinking) провайдер fallback может не понимать
            if spec.supports_thinking and provider == "primary" and self.cfg.llm_thinking_param:
                # у thinking-always-on моделей «disabled» — не настройка, а ошибка 400:
                # экономия на деградации не должна ломать сам запрос
                mode = "enabled" if (thinking or spec.thinking_always_on) else "disabled"
                kwargs["extra_body"] = {"thinking": {"type": mode}}
            for retry in range(self.cfg.llm_retries_per_client + 1):
                attempt += 1
                started = time.perf_counter()
                try:
                    resp = await client.chat.completions.create(**kwargs)
                except Exception as exc:  # noqa: BLE001 - классифицируем ниже, не глотаем
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    last_error = f"{type(exc).__name__}: {exc}"
                    await self.recorder(
                        LLMCallRecord(
                            call_id=str(uuid.uuid4()),
                            role=role,
                            model=kwargs["model"],
                            trace_id=trace,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cost_usd=0.0,
                            latency_ms=latency_ms,
                            ok=False,
                            error=last_error[:2000],
                            provider=provider,
                            attempt=attempt,
                        )
                    )
                    if not _is_retryable(exc):
                        log.warning(
                            "gateway.permanent_error", provider=provider, err=last_error[:300]
                        )
                        break
                    if retry < self.cfg.llm_retries_per_client:
                        await asyncio.sleep(self.cfg.llm_backoff_s * 2**retry)
                    continue
                return await self._on_success(
                    resp,
                    role=role,
                    spec=spec_used,
                    provider=provider,
                    attempt=attempt,
                    trace=trace,
                    dlp_map=dlp_map,
                    started=started,
                )
        raise ModelUnavailable(
            f"все провайдеры недоступны; последняя ошибка: {last_error}",
            cause=last_error or "",
        )

    async def embed(
        self, texts: Sequence[str], *, trace_id: str | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        spec = resolve_spec("embed", self.cfg)
        trace = trace_id or str(uuid.uuid4())
        joined = "".join(texts)
        await self.cost.check(len(joined) / 3.5 * spec.in_usd_per_m / 1_000_000 + 1e-6)
        started = time.perf_counter()
        try:
            client = self.embed_client or self.primary
            resp = await client.embeddings.create(model=spec.name, input=list(texts))
        except openai.OpenAIError as exc:
            await self.recorder(
                LLMCallRecord(
                    call_id=str(uuid.uuid4()),
                    role="embed",
                    model=spec.name,
                    trace_id=trace,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    ok=False,
                    error=repr(exc)[:2000],
                )
            )
            raise ModelUnavailable(
                f"эмбеддинги недоступны: {exc!r}", cause=f"{type(exc).__name__}: {exc}"
            ) from exc
        tokens = int(resp.usage.total_tokens) if resp.usage else 0
        cost = tokens * spec.in_usd_per_m / 1_000_000
        await self.cost.record(cost)
        await self.recorder(
            LLMCallRecord(
                call_id=str(uuid.uuid4()),
                role="embed",
                model=spec.name,
                trace_id=trace,
                prompt_tokens=tokens,
                completion_tokens=0,
                cost_usd=round(cost, 6),
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=True,
            )
        )
        return [list(item.embedding) for item in resp.data]

    def auth_hint(self) -> str:
        """«Ключ выдан не этим сервисом» — детерминированно, без обращения в сеть.

        Пустая строка означает, что по префиксу ключа и хосту противоречия не видно: тогда
        причину ищет :func:`aegis.platform.gateway.diagnose` по ответу провайдера.
        """
        from aegis.platform.gateway.diagnose import auth_hint_for

        key = self.cfg.glm_api_key.get_secret_value() if self.cfg.glm_api_key is not None else ""
        return auth_hint_for(base_url=self.cfg.glm_base_url, api_key=key)

    def describe(self) -> dict[str, Any]:
        """Без секретов — для /status и doctor."""
        return {
            "base_url": self.cfg.glm_base_url,
            "endpoint": self.cfg.glm_base_url.split("//")[-1].split("/")[0],
            "auth_hint": self.auth_hint(),
            "models": {
                role: resolve_spec(role, self.cfg).name
                for role in ("brain", "vision", "fast", "embed")
            },
            "fallback_enabled": self.fallback is not None,
            "embed_endpoint": (self.cfg.embed_base_url or self.cfg.glm_base_url or "")
            .split("//")[-1]
            .split("/")[0],
            "fallback_model": self.cfg.fallback_model,
            "dlp": "on",
        }

    async def aclose(self) -> None:
        for client in (self.primary, self.fallback, self.embed_client):
            if client is not None:
                await client.close()

    # ---------------- internals ----------------

    def _client(self, api_key: SecretStr | None, base_url: str | None) -> AsyncOpenAI:
        return AsyncOpenAI(
            # ключа может не быть в dev/тестах — до первого запроса это безразлично
            api_key=(api_key.get_secret_value() if api_key is not None else "unset"),
            base_url=base_url,
            timeout=self.cfg.llm_timeout_s,
            max_retries=0,  # retry-политика наша: со сменой провайдера и учётом в бюджете
        )

    def _providers(self) -> list[tuple[str, AsyncOpenAI]]:
        out: list[tuple[str, AsyncOpenAI]] = [("primary", self.primary)]
        if self.fallback is not None:
            out.append(("fallback", self.fallback))
        return out

    async def _on_success(
        self,
        resp: Any,
        *,
        role: ChatRole,
        spec: ModelSpec,
        provider: str,
        attempt: int,
        trace: str,
        dlp_map: dict[str, str],
        started: float,
    ) -> ChatResult:
        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = resp.choices[0]
        message = choice.message
        model = str(resp.model or spec.name)
        usage = resp.usage
        prompt_tokens = int(usage.prompt_tokens) if usage else 0
        completion_tokens = int(usage.completion_tokens) if usage else 0
        cost = (
            prompt_tokens * spec.in_usd_per_m + completion_tokens * spec.out_usd_per_m
        ) / 1_000_000
        await self.cost.record(cost)
        await self.recorder(
            LLMCallRecord(
                call_id=str(uuid.uuid4()),
                role=role,
                model=model,
                trace_id=trace,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=round(cost, 6),
                latency_ms=latency_ms,
                ok=True,
                provider=provider,
                attempt=attempt,
            )
        )
        raw: dict[str, Any] = message.model_dump(exclude_none=True)
        raw.setdefault("role", "assistant")
        content = message.content
        if isinstance(content, str):
            # в историю кладём размаскированный текст: при следующем запросе DLP замащает его
            # снова и стабильно — модель не таскает «вечные» токены в прошлом диалога.
            raw["content"] = self.dlp.unmask(content, dlp_map)
        return ChatResult(
            content=raw.get("content"),
            tool_calls=[
                ToolCall(tc.id, tc.function.name, tc.function.arguments)
                for tc in (message.tool_calls or [])
            ],
            model=model,
            role=role,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
            raw_message=raw,
            dlp_map=dlp_map,
            truncated=choice.finish_reason == "length",
        )

    def _mask_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        out: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                masked, mp = self.dlp.mask(content)
                mapping |= mp
                out.append({**msg, "content": masked})
            else:
                # мультимодальный content (image_url) DLP не видит: vision-запросы собираются
                # из заведомо не-PII текста, а извлечение данных с фото идёт отдельным контуром
                out.append(msg)
        return out, mapping

    def _estimate(
        self, messages: list[dict[str, Any]], spec: ModelSpec, max_tokens: int | None
    ) -> float:
        """Консервативная оценка стоимости запроса до отправки — чтобы не выйти за бюджет."""
        chars = sum(len(str(m.get("content", ""))) for m in messages)
        in_tokens = chars / 3.5
        out_tokens = float(max_tokens or 700)
        return (in_tokens * spec.in_usd_per_m + out_tokens * spec.out_usd_per_m) / 1_000_000


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _RETRYABLE_STATUS
