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
from typing import Any, TypeVar

import openai
import orjson
import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel, SecretStr, ValidationError

from aegis.platform.config import Settings
from aegis.platform.gateway.cost import CostGovernor
from aegis.platform.gateway.dlp import DLP
from aegis.platform.gateway.models import (
    ChatRole,
    ModelSpec,
    host_of,
    resolve_spec,
    spec_for_name,
)
from aegis.platform.gateway.streaming import StreamCollector, Streamed, TextSink

__all__ = [
    "ChatResult",
    "LLMCallRecord",
    "ModelGateway",
    "ModelUnavailable",
    "ToolCall",
    "extract_json",
]

log = structlog.get_logger(__name__)

#: PEP 695 (`def f[T](...)`) не используем: песочница разработчика на 3.11, а runtime-минимум
#: проекта — 3.12; TypeVar работает в обеих и не требует проверять, чем запущен контейнер.
_ModelT = TypeVar("_ModelT", bound=BaseModel)

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: сколько раз просить модель переделать ответ, если он не прошёл схему. Два: один ретрай
#: лечает «модель обернула JSON в ```», второй обычно уже означает, что промпт плохой,
#: и крутиться дальше — только жечь бюджет.
_JSON_ATTEMPTS = 2

_FENCE_OPEN = ("```json", "```")


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
    #: Снимки «что именно ушло в API» и «что оттуда пришло» — для журнала решений (M1).
    #: Заполняются только при ``REPRO_RECORD_PAYLOAD``; аудит их не читает, и это намеренно:
    #: метрики и содержимое — разные по объёму вещи, и держать их в одной таблице значит раздувать
    #: запрос «сколько стоило сегодня».
    request: dict[str, Any] | None = None
    response: dict[str, Any] | None = None


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
        return await self._call(
            role,
            messages,
            tools=tools,
            thinking=thinking,
            response_format=response_format,
            temperature=temperature,
            max_tokens=max_tokens,
            trace_id=trace_id,
        )

    async def chat_stream(
        self,
        role: ChatRole,
        messages: Sequence[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        thinking: bool = False,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        trace_id: str | None = None,
        on_text: TextSink | None = None,
    ) -> ChatResult:
        """Тот же запрос, только текст отдаётся по мере прихода.

        Возврат идентичен :meth:`chat` — собранный `ChatResult` с `raw_message`, у которого есть и
        `content`, и доконцованные `tool_calls`. Это не удобство, а требование: весь остальной слой
        (история, цикл инструментов, журнал, учёт стоимости) обязан не знать, откуда взялся ответ,
        иначе стриминг станет второй реализацией агента вместо одной.

        Ретраи возможны только пока наружу не ушёл ни один кусок. «Попросить заново» после того, как
        половина ответа уже на экране, — это второй ответ в одном сообщении, и владелец увидел бы
        сшитого монстра. Обрыв после первого куска = `truncated=True` с тем, что пришло.
        """
        return await self._call(
            role,
            messages,
            tools=tools,
            thinking=thinking,
            temperature=temperature,
            max_tokens=max_tokens,
            trace_id=trace_id,
            stream=True,
            on_text=on_text,
        )

    async def _call(
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
        stream: bool = False,
        on_text: TextSink | None = None,
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
        if stream:
            base_kwargs["stream"] = True
            # без usage-чанка мы бы считали стоимость по своей оценке (см. _estimate) и врали бы в
            # /cost; кто-то из провайдеров на неизвестное поле отвечает 400 — тогда снимем его ниже
            base_kwargs["stream_options"] = {"include_usage": True}

        attempt = 0
        last_error: str | None = None
        want_usage = stream
        for provider, client in self._providers():
            # у fallback своё имя модели — и свой ценник: primary может быть бесплатным, а резерв
            # платным, и учёт «по цене роли» занижал бы расходы до нуля (SLO по стоимости при этом
            # выглядел бы соблюдённым)
            spec_used = spec
            model_name = spec.name
            if provider == "fallback" and self.cfg.fallback_model:
                model_name = self.cfg.fallback_model
                spec_used = spec_for_name(
                    model_name,
                    spec,
                    host=host_of(self.cfg.fallback_base_url or self.cfg.glm_base_url),
                )
            kwargs: dict[str, Any] = {**base_kwargs, "model": model_name}
            if not want_usage:
                kwargs.pop("stream_options", None)
            request = self._snapshot(kwargs)
            # нестандартные параметры (thinking) провайдер fallback может не понимать
            if spec.supports_thinking and provider == "primary" and self.cfg.llm_thinking_param:
                # у thinking-always-on моделей «выключить» — не настройка, а ошибка 400:
                # экономия на деградации не должна ломать сам запрос
                kwargs["extra_body"] = self._thinking_body(
                    bool(thinking) or spec.thinking_always_on
                )
            for retry in range(self.cfg.llm_retries_per_client + 1):
                attempt += 1
                started = time.perf_counter()
                try:
                    if not stream:
                        resp = await client.chat.completions.create(**kwargs)
                        return await self._on_success(
                            resp,
                            role=role,
                            spec=spec_used,
                            provider=provider,
                            attempt=attempt,
                            trace=trace,
                            dlp_map=dlp_map,
                            started=started,
                            request=request,
                        )
                    collector = StreamCollector(on_text)
                    try:
                        await collector.collect(await client.chat.completions.create(**kwargs))
                    except Exception as inner:  # noqa: BLE001 - обрыв потока: см. ниже
                        if collector.has_text:
                            # текст уже на экране у владельца: повторный запрос дал бы второй ответ,
                            # поэтому отдаём что есть и честно помечаем обрыв
                            log.warning(
                                "gateway.stream_interrupted",
                                chunks=collector.chunks,
                                err=f"{type(inner).__name__}: {inner}"[:200],
                            )
                            return await self._on_stream_success(
                                collector.result(),
                                role=role,
                                spec=spec_used,
                                provider=provider,
                                attempt=attempt,
                                trace=trace,
                                dlp_map=dlp_map,
                                started=started,
                                request=request,
                                interrupted=True,
                                masked=masked,
                            )
                        raise
                    return await self._on_stream_success(
                        collector.result(),
                        role=role,
                        spec=spec_used,
                        provider=provider,
                        attempt=attempt,
                        trace=trace,
                        dlp_map=dlp_map,
                        started=started,
                        request=request,
                        interrupted=False,
                        masked=masked,
                    )
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
                            request=request,
                        )
                    )
                    if want_usage and "stream_options" in last_error:
                        # провайдер не знает поля про usage — это не «модель недоступна», а «говорим
                        # на разных наречиях»: повторяем тот же запрос без него. Поле снимаем и с
                        # локального kwargs — следующая попытка этого же клиента читает его
                        want_usage = False
                        base_kwargs.pop("stream_options", None)
                        kwargs.pop("stream_options", None)
                        log.info("gateway.stream_options_rejected", provider=provider)
                        continue
                    if not _is_retryable(exc):
                        log.warning(
                            "gateway.permanent_error", provider=provider, err=last_error[:300]
                        )
                        break
                    if retry < self.cfg.llm_retries_per_client:
                        await asyncio.sleep(self.cfg.llm_backoff_s * 2**retry)
                    continue
        raise ModelUnavailable(
            f"все провайдеры недоступны; последняя ошибка: {last_error}",
            cause=last_error or "",
        )

    async def chat_json(
        self,
        role: ChatRole,
        messages: Sequence[dict[str, Any]],
        schema: type[_ModelT],
        *,
        thinking: bool = False,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        trace_id: str | None = None,
    ) -> _ModelT:
        """Вызов, обязанный вернуться валидным ``schema``; иначе — один ретрай с текстом ошибки.

        Почему в шлюзе, а не в каждом вызывающем: разбор ответа по месту — это N slightly
        different парсеров и N способов молча получить ``None``. Заодно схема описывает контракт
        промпта типом, а не абзацем текста.
        """
        convo: list[dict[str, Any]] = [dict(m) for m in messages]
        last_error = ""
        for attempt in range(_JSON_ATTEMPTS):
            result = await self.chat(
                role,
                convo,
                response_format={"type": "json_object"},
                thinking=thinking,
                temperature=temperature,
                max_tokens=max_tokens,
                trace_id=trace_id,
            )
            try:
                return schema.model_validate(extract_json(result.content or ""))
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:400]
                log.info(
                    "gateway.json_retry",
                    role=role,
                    attempt=attempt,
                    error=last_error[:160],
                    trace_id=trace_id,
                )
                convo = [
                    *convo,
                    {"role": "assistant", "content": result.content or ""},
                    {
                        "role": "user",
                        "content": "Ответ не проходит схему. Верни только JSON без пояснений. "
                        f"Ошибка: {last_error[:300]}",
                    },
                ]
        raise ModelUnavailable(
            f"модель не вернула валидный JSON за {_JSON_ATTEMPTS} попытки: {last_error[:200]}",
            cause=last_error,
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

    def _thinking_style(self) -> str:
        """openrouter|zai — как реально пойдёт запрос: auto читает хост GLM_BASE_URL."""
        style = self.cfg.llm_thinking_style
        if style != "auto":
            return style
        return "openrouter" if host_of(self.cfg.glm_base_url).endswith("openrouter.ai") else "zai"

    def _thinking_body(self, enabled: bool) -> dict[str, Any]:
        """Тело параметра размышления: у z.ai и OpenRouter разные имена и форма.

        `thinking.type` OpenRouter не знает, а `reasoning.enabled` не знает z.ai: неверная форма —
        это 400 на каждом запросе (или молча проигнорированный параметр), поэтому стиль
        определяется по хосту, а `LLM_THINKING_STYLE` перебивает автоопределение.
        """
        effort = self.cfg.llm_reasoning_effort
        if self._thinking_style() == "openrouter":
            reasoning: dict[str, Any] = {"enabled": enabled}
            if enabled and effort:
                reasoning["effort"] = effort
            return {"reasoning": reasoning}
        body: dict[str, Any] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if enabled and effort:
            body["reasoning_effort"] = effort
        return body

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
                for role in ("brain", "vision", "fast", "embed", "quarantine")
            },
            "fallback_enabled": self.fallback is not None,
            "embed_endpoint": (self.cfg.embed_base_url or self.cfg.glm_base_url or "")
            .split("//")[-1]
            .split("/")[0],
            "fallback_model": self.cfg.fallback_model,
            # не «auto», а то, что реально уйдёт в запрос: `/status` должен врать меньше
            "thinking_style": self._thinking_style(),
            "reasoning_effort": self.cfg.llm_reasoning_effort or None,
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
        request: dict[str, Any] | None = None,
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
                request=request,
                # не «всегда»: без REPRO_RECORD_PAYLOAD тратить сериализацию ответа незачем,
                # и падать из-за журнала в основном пути — тем более
                response=_response_snapshot(resp) if request is not None else None,
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

    async def _on_stream_success(
        self,
        got: Streamed,
        *,
        role: ChatRole,
        spec: ModelSpec,
        provider: str,
        attempt: int,
        trace: str,
        dlp_map: dict[str, str],
        started: float,
        request: dict[str, Any] | None = None,
        interrupted: bool = False,
        masked: Sequence[dict[str, Any]] = (),
    ) -> ChatResult:
        """Собранный из чанков ответ — в тот же `ChatResult`, что дал бы обычный запрос.

        Стоимость считаем по usage, если провайдер его прислал; иначе — по длине текста. «Иначе»
        тут не догадка: без usage-чанка у нас нет других чисел, а оценивать стрим по нулям значило
        бы вести бюджет по одному из двух путей и удивляться расхождению в /cost.
        """
        latency_ms = int((time.perf_counter() - started) * 1000)
        model = str(got.model or spec.name)
        content = self.dlp.unmask(got.content, dlp_map) if got.content else got.content
        # тот же консервативный пересчёт, что и в `_estimate`: символы/3.5. Смысл не в точности, а
        # в том, чтобы стриминговый путь не жил по другой арифметике, чем обычный
        prompt_tokens = got.prompt_tokens or int(
            sum(len(str(m.get("content", ""))) for m in masked) / 3.5
        )
        completion_tokens = got.completion_tokens or int(len(content or "") / 3.5)
        cost = (
            prompt_tokens * spec.in_usd_per_m + completion_tokens * spec.out_usd_per_m
        ) / 1_000_000
        await self.cost.record(cost)
        raw: dict[str, Any] = {"role": "assistant"}
        if content:
            raw["content"] = content
        if got.reasoning:
            raw["reasoning_content"] = got.reasoning
        if got.tool_calls:
            raw["tool_calls"] = [call.as_raw() for call in got.tool_calls]
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
                error="поток оборвался: ответ неполный" if interrupted else None,
                provider=provider,
                attempt=attempt,
                request=request,
                response=(
                    _stream_snapshot(raw, got, interrupted=interrupted)
                    if request is not None
                    else None
                ),
            )
        )
        return ChatResult(
            content=content,
            tool_calls=[ToolCall(call.id, call.name, call.arguments) for call in got.tool_calls],
            model=model,
            role=role,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
            raw_message=raw,
            dlp_map=dlp_map,
            truncated=interrupted or got.finish_reason == "length",
        )

    def _snapshot(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """Чем именно мы дёргали API: model, messages (после DLP), tools, параметры.

        Ключей тут быть не может — заголовок Authorization живёт в клиенте, а не в kwargs.
        Возвращаем тот же объект, что ушёл в SDK: записывать «как мы думаем, оно выглядело» —
        значит вести журнал догадок.
        """
        if not self.cfg.repro_record_payload:
            return None
        return dict(kwargs)

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


def extract_json(text: str) -> dict[str, Any]:
    """Терпеливо достать объект из ответа: код-блок, обрамляющая проза, ``{...}`` в середине.

    ``response_format=json_object`` у части провайдеров не гарантирован (GLM принимает параметр, но
    при thinking может добавить пояснение). Валидация схемы строже парсера: сначала вытаскиваем
    границы объекта, потом разбираем — иначе проза превращалась бы в «судья не ответил».
    """
    raw = (text or "").strip()
    for fence in _FENCE_OPEN:
        if raw.startswith(fence):
            raw = raw[len(fence) :].strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()
    for candidate in _candidates(raw):
        try:
            parsed = orjson.loads(candidate)
        except orjson.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return dict(parsed)
    raise ValueError(f"в ответе нет JSON-объекта: {raw[:160]!r}")


def _candidates(raw: str) -> list[str]:
    out = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        inner = raw[start : end + 1]
        if inner != raw:
            out.append(inner)
    return out


def _json_dump(obj: Any) -> dict[str, Any] | None:
    """``model_dump`` SDK'а в JSON-совместимый dict, если объект это умеет.

    Сначала с ``mode="json"`` (иначе в журнал попадут datetime/enum, которые orjson не примет),
    потом без аргументов: старый SDK и простые двойники его не принимают.
    """
    dump = getattr(obj, "model_dump", None)
    if not callable(dump):
        return None
    for kwargs in ({"mode": "json"}, {}):
        try:
            data = dump(**kwargs)
        except TypeError:
            continue
        return dict(data) if isinstance(data, dict) else None
    return None


def _response_snapshot(resp: Any) -> dict[str, Any]:
    """Ответ провайдера в том виде, в каком его видно из OpenAI-совместимого SDK."""
    dumped = _json_dump(resp)
    if dumped is not None:
        return dumped
    choices: list[dict[str, Any]] = []
    for choice in list(getattr(resp, "choices", None) or [])[:1]:
        message = getattr(choice, "message", None)
        body = _json_dump(message) or {
            "role": str(getattr(message, "role", "assistant") or "assistant"),
            "content": str(getattr(message, "content", "") or ""),
        }
        choices.append(
            {"finish_reason": str(getattr(choice, "finish_reason", "") or ""), "message": body}
        )
    return {
        "model": str(getattr(resp, "model", "") or ""),
        "id": str(getattr(resp, "id", "") or ""),
        "choices": choices,
    }


def _stream_snapshot(raw: dict[str, Any], got: Streamed, *, interrupted: bool) -> dict[str, Any]:
    """Ответ стрима в той же форме, что и ответ провайдера у `chat`.

    `choices[0].message` — не кокетство: разбор журнала и `aegis replay` должны видеть один формат
    «что увидели мы», независимо от того, пришёл ответ одним объектом или сорока чанками.
    """
    return {
        "choices": [{"finish_reason": got.finish_reason or "", "message": dict(raw)}],
        "streamed": True,
        "chunks": got.chunks,
        "interrupted": interrupted,
    }


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _RETRYABLE_STATUS
