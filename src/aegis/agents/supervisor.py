"""Supervisor v1: роутинг запроса, цикл tool calling, подтверждения через policy engine.

Что здесь происходит и почему именно так:

* **tier-роутинг** — болтовня не должна стоить brain-модели; тяжёлые запросы получают thinking.
  Решение детерминированное (эвристика + уровень бюджета), а не «на глаз модели».
* **цикл** ≤ ``max_iterations``: модель → tool_calls → policy → исполнение → обратно в модель.
  Каждое tool-сообщение получает ответ всегда, включая confirm/deny: иначе следующий запрос к
  OpenAI-совместимому API падает на несогласованном ``tool_call_id`` (реальный баг первой версии).
* **подтверждения** — снимок сообщений уходит в KV с TTL, а inline-кнопка возвращает управление в
  :meth:`Supervisor.resume`. Действие не исполняется «в фоне»: без явного «Да» его нет.
* **события** — каждый ход пишется в event store (trace_id, версия промпта, модель, стоимость),
  поэтому любой ответ воспроизводим по данным, а не по памяти модели.

Состояния «в контексте» нет: история — рабочий кэш в KV, факты/заметки — в БД.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import orjson
import structlog

from aegis.agents.intents import try_answer
from aegis.agents.prompts.system import PROMPT_VERSION, build_system_prompt
from aegis.agents.quarantine import quarantine_payload
from aegis.agents.services import Services
from aegis.agents.tools.registry import (
    Attachment,
    ToolContext,
    ToolRegistry,
    ToolResult,
    UnknownTool,
)
from aegis.agents.verify import Verifier
from aegis.governance.audit import AuditLog, NullAudit
from aegis.governance.degradation import Overrides
from aegis.governance.killswitch import KillSwitch
from aegis.governance.policy import ActionContext, Decision, PolicyEngine
from aegis.governance.principals import (
    PrincipalGovernance,
    PrincipalSnapshot,
    parse_roster,
    permission_for_tool,
    resolve_principal,
    snapshot_for,
)
from aegis.governance.recorder import DecisionRecorder, NullDecisionRecorder
from aegis.governance.turns import NullTurnLedger, TurnBusy, TurnHandle, TurnLedger
from aegis.platform.canonical import sha256_hex
from aegis.platform.config import Settings, settings
from aegis.platform.events.sink import EventSink, NullEventSink
from aegis.platform.flags import FlagEngine
from aegis.platform.gateway.client import ChatResult, ModelUnavailable
from aegis.platform.gateway.cost import BudgetExceeded
from aegis.platform.gateway.diagnose import diagnose, gateway_auth_hint
from aegis.platform.gateway.models import ChatRole
from aegis.platform.gateway.streaming import TextSink
from aegis.platform.kv import KV, history_key, pending_key
from aegis.platform.logging import bind_contextvars
from aegis.platform.rls import PrincipalScope, principal_scope, reset_scope, set_scope
from aegis.web.search import wrap_untrusted

__all__ = ["Inbound", "PendingAction", "Reply", "Route", "Supervisor"]

log = structlog.get_logger(__name__)

#: Потолок длины ответа: Telegram-лимит с запасом на разметку (см. interaction.telegram.render).
MAX_MESSAGE_CHARS = 4000

CONFIRM_PLACEHOLDER = "[ожидает подтверждения владельца]"

# Запросы, где thinking действительно окупается (вывод, план, сравнение, причины).
THINK_HINT = re.compile(
    r"(проанализ|спланир|сравн|оцени|стратеги|почему|как лучше|что лучше|рассчитай|"
    r"план на|причин|следстви)",
    re.I,
)
# Болтовня: реплика без полезной нагрузки, отвечать которую можно без инструментов.
SMALLTALK = re.compile(
    r"^(привет|здравствуй|спасибо|благодарю|ок|окей|пока|хорошо|ясно|да|нет|го|ага)[\s!.,?]*$", re.I
)
# Явная просьба не расходовать бюджет на разбор.
CHEAP_HINT = re.compile(r"(коротко|в одно слово|без анализа)", re.I)


@dataclass(slots=True)
class Inbound:
    """Сообщение владельца (+ вложения). ``source_trust`` — кто является инициатором текста.

    ``actor_id`` (F2) — кто СПРОСИЛ; ``owner_id`` — чьи данные обрабатываются. Пока бот
    одновладелец, они совпадают; с появлением членов семьи расхождение обязано различаться
    и в журнале, и в правах (actor не получает доступ к чужому «потому что бот так умеет»).
    """

    text: str
    owner_id: int
    attachments: list[Attachment] = field(default_factory=list)
    source_trust: Literal["owner", "untrusted", "system"] = "owner"
    actor_id: int | None = None

    @property
    def actor(self) -> int:
        return int(self.actor_id if self.actor_id is not None else self.owner_id)


@dataclass(slots=True)
class PendingAction:
    tool: str
    args: dict[str, Any]
    call_id: str
    reason: str
    risk: str = "none"
    #: сработавшее правило (F5): «rule@version» уезжает в снимок и возвращается в журнал
    #  после подтверждения — иначе подтверждённый шаг выглядит безымянным
    rule: str = ""

    def render(self) -> str:
        args_json = orjson.dumps(self.args, option=orjson.OPT_INDENT_2).decode()
        return (
            f"<b>{self.tool}</b> (<i>{self.risk}</i>)\n<code>{args_json}</code>"
            f"\nпричина: {self.reason}"
        )


@dataclass(slots=True)
class Reply:
    """То, что интерфейс показывает владельцу."""

    text: str
    pending: list[PendingAction] = field(default_factory=list)
    pending_id: str | None = None
    trace_id: str = ""
    model: str | None = None
    cost_usd: float = 0.0
    iterations: int = 0
    degraded: bool = False
    #: ход поставлен в очередь «один на владельца» (F1) — не потерян, а ждёт своего прохода
    queued: bool = False
    #: отказ, который имеет смысл обжаловать владельцу (F5): бот добавит кнопку апелляции
    appealable: bool = False
    #: ход исполнялся через воркфлоу-раннер (F8): видно в журнале и в /status
    workflow: str = ""
    #: id staged-апелляции (F5): бот вешает кнопку «обжаловать», владелец резолвит тем же
    # ok/no-механизмом, что и обычные подтверждения
    appeal_id: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.pending) and self.pending_id is not None


@dataclass(frozen=True, slots=True)
class Route:
    """Решение «какой моделью и с чем отвечать»."""

    role: ChatRole
    tools: bool
    thinking: bool
    reason: str


class Supervisor:
    def __init__(
        self,
        *,
        services: Services,
        registry: ToolRegistry,
        policy: PolicyEngine,
        kv: KV,
        cfg: Settings | None = None,
        events: EventSink | None = None,
        audit: AuditLog | None = None,
        kill_switch: KillSwitch | None = None,
        recorder: DecisionRecorder | None = None,
        turns: TurnLedger | None = None,
        flags: FlagEngine | None = None,
        overrides: Overrides | None = None,
        principals: Any = None,
        governance: PrincipalGovernance | None = None,
        metrics: Any = None,
        activity_ledger: Any = None,
    ) -> None:
        self.services = services
        self.registry = registry
        self.policy = policy
        self.kv = kv
        self.cfg = cfg or settings()
        self.events: EventSink = events or NullEventSink()
        self.audit: AuditLog = audit or NullAudit()
        self.kill_switch = kill_switch
        #: сверка ответа с источниками (шаг 2). Отдельный слой, потому что у отказа верификатора
        #  допустим ровно один исход: сказать «не проверено», но не отменить ответ
        self.verifier = Verifier(gateway=services.gateway, cfg=self.cfg)
        #: журнал решений (M1): точный вход/выход хода и хэш-цепочка. Null — когда БД нет, и это
        #  видно в /status: «воспроизводимости нет» должно быть фактом, а не сюрпризом на инцидент
        self.repro: DecisionRecorder = recorder or NullDecisionRecorder()
        #: KV упал в этом процессе? Честно показываем в /status, а не молча «забываем» историю
        self.kv_degraded = False
        # --- шаг 2.5: параллельность, права, флаги, деградация, идемпотентность ---
        #: тот же ledger, что у рекордера: «открытый ход» и очередь — одна правда на оба глаза
        self.turns: TurnLedger = turns or getattr(self.repro, "turns", None) or _memory_turns()
        self.flags = flags
        self.overrides = overrides
        self.principals = principals
        self.governance = governance
        self.metrics = metrics
        #: реестр исполненных активностей (F8): «платёж не уйдёт дважды» после краха процесса
        self.activity_ledger = activity_ledger
        #: кеш runtime-overrides на время хода: 40 запросов на tool_call — не «свежесть любой ценой»
        self._overrides_cache: dict[str, Any] = {}
        self._overrides_at = 0.0
        self._degrade_notes: set[str] = set()

    # ------------------------------------------------ public

    async def handle(self, msg: Inbound, *, on_delta: TextSink | None = None) -> Reply:
        # F2/RLS: связываем контекст до первой транзакции хода — «кто спросил» (actor), «чьё
        # это» (house=owner) и роль. Дальше хук БД сам ставит GUC в каждой транзакции; у
        # репозиториев нет шанса «забыть». kind уточнится после snapshot — до этого считаем гостя
        # гостем: fail-closed на чтение, запись журнала это не блокирует
        with principal_scope(msg.actor, "guest", msg.owner_id):
            return await self._handle_bound(msg, on_delta=on_delta)

    async def _handle_bound(self, msg: Inbound, *, on_delta: TextSink | None) -> Reply:
        trace_id = str(uuid.uuid4())
        bind_contextvars(trace_id=trace_id, owner_id=msg.owner_id, actor_id=msg.actor)
        # F1: «один ход на владельца» — заявка в БД, а не «мы же один процесс». Пока активен
        # чужой ход, это сообщение СТАВИТСЯ В ОЧЕРЕДЬ и будет разобрано drain'ом: апдейт не
        # теряется ни при каком исходе, включая падение процесса на середине чужого хода
        handle: TurnHandle | None = None
        try:
            handle = await self.turns.begin(
                trace_id,
                owner_id=msg.owner_id,
                prompt_ids=[],
                tools_schema_sha=None,
            )
        except TurnBusy:
            await self.turns.enqueue(
                msg.owner_id,
                trace_id=trace_id,
                kind="message",
                payload={
                    "text": (msg.text or "")[:8000],
                    "actor_id": msg.actor,
                    "source_trust": msg.source_trust,
                },
            )
            backlog = await self.turns.queue_count(msg.owner_id)
            return Reply(
                text=f"Текущий ещё исполняется — добавил это в очередь (позиция {backlog}).",
                trace_id=trace_id,
                queued=True,
            )
        # F2: кто спросил и что ему можно; F7: что приложение деградировало прямо сейчас
        snapshot = await self._principal_snapshot(msg)
        if not snapshot.principal.usable or snapshot.kill_active:
            # персональный kill: ход не исполняется вовсе. Отказ текстом, а не тихое молчание:
            # «бот не отвечает гостю» без объяснения — это инцидент, который никто не найдёт
            await self.turns.finish(trace_id, handle.fencing_token if handle else None)
            reason = str(snapshot.killed.get("reason") or "приостановлено владельцем")
            return Reply(
                text=f"Мои ответы для тебя на паузе: {reason}. Снятие — только у владельца.",
                trace_id=trace_id,
                degraded=True,
            )
        if snapshot.budget_exhausted and not snapshot.principal.is_owner:
            await self.turns.finish(trace_id, handle.fencing_token if handle else None)
            limit = float(snapshot.budget_limit or 0.0)
            return Reply(
                text=(
                    f"Личный лимит на день (${limit:.2f}) исчерпан — умные ответы "
                    "на паузе до завтра. Чтение, заметки и команды работают."
                ),
                trace_id=trace_id,
                degraded=True,
            )
        # роль известна — пересвязываем контекст (member получает household-чтение, guest — нет)
        _scope_token = _scope_for(msg, snapshot)
        flags = await self._flag_snapshot(msg.actor)
        overrides = await self._runtime_overrides()
        level = await self._degradation_level()
        route = self._route(msg, level=level, overrides=overrides)
        # level передаётся явно: route() и системный промпт должны видеть один и тот же бюджет
        log.info("supervisor.route", role=route.role, thinking=route.thinking, reason=route.reason)
        system = await self._system_message(level=level, overrides=overrides)
        messages: list[dict[str, Any]] = [
            system,
            *await self._history(msg.owner_id),
            {"role": "user", "content": self._user_content(msg)},
        ]
        prompt_ids = self._prompt_ids(str(system.get("content") or ""))
        ctx = ToolContext(
            trace_id=trace_id,
            owner_id=msg.owner_id,
            services=self.services,
            source_trust=msg.source_trust,
            attachments=list(msg.attachments),
            # инструменты ходят в тот же KV за кэшем курсов: свой ответ на «повтори» дешевле,
            # чем новый запрос в банк, но кэш не должен пережить смену вопроса
            extras={
                "kv": self.kv,
                "notices": [],
                # шаги хода для журнала: номер шага и «чем этот ход был вооружён»
                "turn_no": 0,
                "prompt_ids": prompt_ids,
                "started_at": time.perf_counter(),
                # «улучшайзеры» (сверка, карантин) отключаем первыми: сначала ответ, потом polish
                "degraded": level > 0,
                # сырьё для верификатора: что реально вернули внешние инструменты
                "sources": [],
            },
        )
        await self.repro.begin_turn(
            trace_id,
            owner_id=msg.owner_id,
            prompt_ids=prompt_ids,
            tools_schema_sha=self.registry.schema_sha(),
        )
        # номер шага и «чем вооружён ход» теперь на строке заявки; токен — в extras: ему
        # сверяются advance/finish (fencing: украденная аренда не двигает чужой счётчик)
        ctx.extras["fencing"] = handle.fencing_token if handle else None
        ctx.extras["flags"] = flags
        # actor в extras обязателен обоим путям (handle и resume): журнал, апелляция и бюджеты
        # читают его оттуда, и «resume знает, а handle нет» = молча потерянный автор хода
        ctx.extras["actor_id"] = msg.actor
        ctx.extras["principal"] = snapshot.principal
        # _decide читает СНИМОК (kill/budget живут на нём), а не только principal: без этой
        # строки RBAC-вход policy был бы вечно пустым — «права проверяются» только на словах
        ctx.extras["principal_snapshot"] = snapshot
        ctx.extras["budget_ratio"] = snapshot.ratio()
        await self._event(
            owner_id=msg.owner_id,
            event_type="conversation.turn_received",
            payload={
                "text": msg.text[:4000],
                "attachments": len(msg.attachments),
                "role": route.role,
                "thinking": route.thinking,
                "prompt_version": PROMPT_VERSION,
                "source_trust": msg.source_trust,
            },
        )
        # Детерминированный путь раньше модели: там, где есть первоисточник, LLM не нужен —
        # ответ не зависит ни от дневного бюджета, ни от живости провайдера (принцип 5).
        try:
            fast = await self._try_intent(msg, ctx)
            if fast is not None:
                await self._save_history(msg.owner_id, messages, final_text=fast.text)
                fast.trace_id = trace_id
                await self._record_turn(ctx, messages, fast, route=route)
                return fast
            reply = await self._guarded_loop(messages, ctx, route=route, on_delta=on_delta)
            # F5: отказ не-владельцу — право подать апелляцию; снимок отказанных действий
            # стейджится в KV ровно как pending-подтверждение, чтобы резолвил его тот же
            # механизм (кнопки ok/no владельца), а не второй, «почти такой же»
            if (
                ctx.extras.get("appealable")
                and not reply.needs_confirmation
                and ctx.extras.get("denied")
            ):
                reply.appealable = True
                reply.appeal_id = await self._stage_appeal(
                    messages, ctx, list(ctx.extras["denied"])
                )
            await self._verify_reply(reply, ctx, question=msg.text)
            notes = _notes_of(ctx)
            _append_notices(reply, ctx)
            await self._save_history(msg.owner_id, messages, final_text=reply.text)
            reply.trace_id = trace_id
            await self._record_turn(ctx, messages, reply, route=route, notes=notes)
            self._observe_turn(ctx, reply)
            return reply
        finally:
            if _scope_token is not None:
                reset_scope(_scope_token)
            await self._end_turn(trace_id, handle)

    async def resume(self, pending_id: str, approved: bool, owner_id: int) -> Reply:
        """Продолжение прерванного хода после решения владельца (ADR-006)."""
        raw = await self._kv("pending.getdel", self.kv.getdel(pending_key(pending_id)), None)
        if raw is None and self.kv_degraded:
            return Reply(
                "Память сессий сейчас недоступна (Redis), поэтому я не могу ни продолжить, ни "
                "отменить действие. Попробуй через минуту — или повтори запрос после её починки.",
                degraded=True,
            )
        if raw is None:
            return Reply("Это подтверждение устарело или уже обработано. Запроси действие заново.")
        snapshot: dict[str, Any] = orjson.loads(raw)
        if int(snapshot.get("owner_id", -1)) != owner_id:
            return Reply("Это подтверждение относится к другому диалогу.")
        messages: list[dict[str, Any]] = list(snapshot["messages"])
        actions: list[dict[str, Any]] = list(snapshot["actions"])
        trace_id = str(snapshot["trace_id"])
        ctx = ToolContext(
            trace_id=trace_id,
            owner_id=owner_id,
            services=self.services,
            extras={
                "kv": self.kv,
                "notices": [],
                "turn_no": 0,
                "started_at": time.perf_counter(),
                "degraded": False,
                "sources": [],
            },
        )
        # ход продолжает уже начатую трассу: без этого «до» и «после подтверждения» выглядели бы
        # как два несвязанных ответа, а это ровно тот случай, где владельца интересует причина.
        # F1: та же trace_id переоткрывает заявку (status finished→active) с НОВЫМ токеном:
        # старый процесс, если он чудом жив, больше не сможет закрыть этот ход «своим» токеном
        handle = await self.repro.begin_turn(trace_id, owner_id=owner_id, tools_schema_sha=None)
        ctx.extras["fencing"] = handle.fencing_token if handle else None
        # журнал продолжения помнит, ЧЕЙ запрос исполняется (actor — не всегда владелец: гость
        # попросил, владелец подтвердил; у replay/бюджета это разные строки)
        ctx.extras["actor_id"] = snapshot.get("actor_id", owner_id)

        for action in actions:
            tool = str(action["tool"])
            args = dict(action["args"])
            call_id = str(action["call_id"])
            if approved:
                result = await self._execute(tool=tool, args=args, ctx=ctx, decision="confirmed")
                content = _tool_text(tool, result)
            else:
                content = "Отменено владельцем (действие не выполнено)."
                await self._audit_tool(ctx, tool, args, "rejected", content, ok=True)
            _patch_tool_message(messages, call_id, content)
            await self._event(
                owner_id=owner_id,
                event_type="action.confirmation_resolved",
                payload={"tool": tool, "approved": approved, "trace_id": trace_id},
            )

        if not approved:
            await self._save_history(owner_id, messages, final_text="Отменено владельцем.")
            return Reply("Отменено — ничего не записано.", trace_id=trace_id)

        route = Route(
            role="brain", tools=True, thinking=False, reason="продолжение после подтверждения"
        )
        try:
            reply = await self._guarded_loop(messages, ctx, route=route)
            await self._verify_reply(reply, ctx, question=_last_user_text(messages))
            notes = _notes_of(ctx)
            _append_notices(reply, ctx)
            await self._save_history(owner_id, messages, final_text=reply.text)
            reply.trace_id = trace_id
            await self._record_turn(ctx, messages, reply, route=route, notes=notes)
            self._observe_turn(ctx, reply)
            return reply
        finally:
            await self._end_turn(trace_id, handle)

    async def reset(self, owner_id: int) -> None:
        await self._kv("history.delete", self.kv.delete(history_key(owner_id)), 0)
        await self._event(owner_id=owner_id, event_type="conversation.reset", payload={})

    @property
    def _tracing_failures(self) -> int:
        return (
            int(getattr(self.events, "failures", 0))
            + int(getattr(self.audit, "failures", 0))
            + int(getattr(self.repro, "failures", 0))
        )

    @property
    def _tracing_degraded(self) -> bool:
        return self._tracing_failures > 0

    async def status(self, owner_id: int) -> dict[str, Any]:
        """Для ``/status``: режим, бюджет, инструменты. Секретов здесь нет по построению."""
        return {
            "notes": await self._notes_index_state(),
            "prompt_version": PROMPT_VERSION,
            "gateway": self.services.gateway.describe(),
            "cost": await self.services.gateway.cost.snapshot(),
            "kill_switch": await self._kill_switch_state(),
            "tools": self.registry.names(),
            "history_messages": len(await self._history(owner_id)),
            "kv_degraded": self.kv_degraded,
            "max_iterations": self.cfg.max_iterations,
            # «подключено» != «пишется»: без этих полей `/status` врал при мёртвой трассе
            "tracing_degraded": self._tracing_degraded,
            "tracing_failures": self._tracing_failures,
            # «сверка и карантин включены» — отдельный факт: без него «почему мне не сказали, что
            #  число не из источника» превращается в чтение кода
            "answer_polish": {
                "verify": self.cfg.verify_enabled,
                "quarantine": self.cfg.quarantine_enabled,
                "always": self.cfg.verify_always,
            },
            # «пишем журнал или нет» — свой факт: без него /status врёт о воспроизводимости
            "repro_enabled": bool(self.repro.enabled),
            "repro_failures": int(getattr(self.repro, "failures", 0)),
            # расписание: «3 запланировано, 1 просрочено» — это диагноз тика. Без него «напоминание
            # не пришло» означает «иди читай таблицу»
            "reminders": await _reminders_status(self.services.reminders, self.cfg),
            # F1/F7: «два процесса дерутся за владельца» и «мы прямо сейчас деградированы» —
            # то, без чего инцидент начинается с чтения логов
            "turns": await self._turns_status(),
            "degradation": await self._degradation_status(),
            "workflow_mode": str(getattr(self.cfg, "workflow_mode", "off")),
        }

    async def _turns_status(self) -> dict[str, Any]:
        try:
            data = await self.turns.status()
        except Exception as exc:  # noqa: BLE001 - статус не имеет права падать
            return {"error": f"{type(exc).__name__}: {exc}"[:160]}
        return {"durable": bool(self.turns.durable), **data}

    async def _degradation_status(self) -> dict[str, Any]:
        if self.overrides is None:
            return {"enabled": False}
        try:
            active = await self.overrides.list_active()
        except Exception as exc:  # noqa: BLE001
            return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"[:160]}
        return {"enabled": True, "active": list(active)}

    # ------------------------------------------------ роутинг

    async def route(self, msg: Inbound) -> Route:
        """Публичная точка маршрутизации — её же дёргают evals и (позже) Temporal-activity."""
        return self._route(msg, level=await self._degradation_level())

    def _route(
        self, msg: Inbound, *, level: int, overrides: Mapping[str, Any] | None = None
    ) -> Route:
        text = (msg.text or "").strip()
        # F7: «только дешёвый путь» — решение БЕЗ LLM-классификатора (принцип 5: деградация
        # не должна сама стоить вызова модели). Классификатор остаётся фолбэком, если строка
        # всё же короткая: fast и так fast
        if overrides and overrides.get("route.force_fast"):
            return Route("fast", bool(msg.attachments), False, "degraded:fast_only")
        if msg.attachments:
            return Route("brain", True, level < 1, "есть вложение: нужен analyze_image + brain")
        if SMALLTALK.match(text):
            return Route("fast", False, False, "болтовня: дешёвая модель без инструментов")
        if level >= 2:
            return Route("fast", True, False, "бюджет > 85%: деградация на fast-модель")
        thinking = bool(THINK_HINT.search(text)) and not CHEAP_HINT.search(text)
        return Route("brain", True, thinking and level < 1, "рабочий режим")

    async def _try_intent(self, msg: Inbound, ctx: ToolContext) -> Reply | None:
        """Ответ без LLM, если вопрос покрыт парсером. None — «отвечает модель»."""
        if msg.attachments:
            return None
        try:
            answer = await try_answer(
                msg.text,
                services=self.services,
                kv=self.kv,
                cfg=self.cfg,
                notices=ctx.extras.get("notices"),
            )
        except Exception as exc:  # noqa: BLE001 - кривой парсер не имеет права ронять ход
            log.warning("supervisor.intent_failed", err=repr(exc)[:200])
            return None
        if answer is None:
            return None
        await self._event(
            owner_id=msg.owner_id,
            event_type="intent.answered",
            payload={"intent": answer.intent, "trace_id": ctx.trace_id, **answer.meta},
        )
        log.info("supervisor.intent", intent=answer.intent, meta=len(answer.meta))
        return Reply(
            text=answer.text,
            trace_id=ctx.trace_id,
            model=f"deterministic:{answer.intent}",
            cost_usd=0.0,
            iterations=0,
        )

    def _prompt_ids(self, system_text: str) -> list[dict[str, str]]:
        """Версия промпта + хэш именно того текста, что ушёл в модель.

        ``PROMPT_VERSION`` одна на все ходы, а в системном сообщении живут ещё и факты, список
        инструментов, заметки уровня деградации: два хода при одной версии могут требовать разного
        поведения. Хэш полного текста — это то, по чему replay обязан совпасть или честно
        расписаться в расхождении.
        """
        if not system_text:
            return []
        return [
            {
                "id": "core/system",
                "version": PROMPT_VERSION,
                "sha256": sha256_hex(system_text.encode("utf-8")),
            }
        ]

    async def _record_turn(
        self,
        ctx: ToolContext,
        messages: list[dict[str, Any]],
        reply: Reply,
        *,
        route: Route,
        notes: Sequence[str] = (),
    ) -> None:
        """Ход в журнале целиком: вход, ответ, стоимость, инструменты, замечания."""
        started = float(ctx.extras.get("started_at") or 0.0)
        prompt_ids = list(ctx.extras.get("prompt_ids") or [])
        if not prompt_ids and messages:
            prompt_ids = self._prompt_ids(str(messages[0].get("content") or ""))
        await self.repro.turn_summary(
            trace_id=ctx.trace_id,
            turn_no=int(ctx.extras.get("turn_no", 0)),
            owner_id=ctx.owner_id,
            messages=messages,
            answer=reply.text,
            model=reply.model,
            cost_usd=reply.cost_usd,
            latency_ms=int((time.perf_counter() - started) * 1000) if started else 0,
            iterations=reply.iterations,
            prompt_ids=prompt_ids,
            tools_schema_sha=self.registry.schema_sha() if route.tools else None,
            route=f"{route.role}:{route.reason}",
            notes=notes,
            flags=dict(ctx.extras.get("flags") or {}),
            actor_id=ctx.extras.get("actor_id"),
            degraded=reply.degraded or bool(ctx.extras.get("degraded")),
        )

    async def _degradation_level(self) -> int:
        cost = self.services.gateway.cost
        return cost.degradation_level(await cost.spent())

    # ------------------------------------------------ цикл агента

    async def _guarded_loop(
        self,
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        *,
        route: Route,
        on_delta: TextSink | None = None,
    ) -> Reply:
        """Цикл с гарантированной деградацией: ни бюджет, ни отказ провайдера не роняют ответ."""
        try:
            return await self._loop(messages, ctx, route=route, on_delta=on_delta)
        except BudgetExceeded as exc:
            redacted = self.services.gateway.dlp.redact(str(exc))
            reply = Reply(
                text=(
                    "Дневной бюджет LLM исчерпан — умная часть на паузе.\n"
                    f"{redacted}\n"
                    "/cost покажет расходы, команды работают."
                ),
                trace_id=ctx.trace_id,
                degraded=True,
            )
            _append_notices(reply, ctx)
            return reply
        except ModelUnavailable as exc:
            # Владелец должен увидеть ПРИЧИНУ, а не «попробуй позже»: читать логи контейнера
            # ради 401 — не тот уровень сервиса для личного инструмента.
            reason = diagnose(
                str(getattr(exc, "cause", "") or exc),
                timeout_s=self.cfg.llm_timeout_s,
                context=gateway_auth_hint(self.services.gateway),
            )
            log.warning("supervisor.model_unavailable", err=reason[:200])
            # Только переносы строк, никакой разметки: этот текст обязан дойти целиком даже
            # тогда, когда Telegram не принял HTML и слой отправки ушёл в запасной путь.
            reply = Reply(
                text=(
                    "Модели сейчас недоступны — провайдер не ответил.\n"
                    f"Причина: {reason}\n"
                    "/status и aegis doctor показывают состояние; "
                    "команды и работа с базой продолжаются."
                ),
                trace_id=ctx.trace_id,
                degraded=True,
            )
            _append_notices(reply, ctx)
            return reply

    async def _loop(
        self,
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        *,
        route: Route,
        on_delta: TextSink | None = None,
    ) -> Reply:
        total_cost = 0.0
        model: str | None = None
        iterations = 0
        for _ in range(self.cfg.max_iterations):
            iterations += 1
            # номер шага ведёт ledger: локальный счётчик расходится с журналом, если ход начался
            # без begin_turn (resume, деградация) — тогда step=0 и мы откатываемся на итерацию.
            # fencing-токен обязателен: украденная (просроченная) аренда не двигает чужой шаг
            step = await self.repro.turn_step(ctx.trace_id, ctx.extras.get("fencing")) or iterations
            ctx.extras["turn_no"] = step
            call_kwargs: dict[str, Any] = {
                "tools": self.registry.schemas() if route.tools else None,
                "thinking": route.thinking,
                "trace_id": ctx.trace_id,
            }
            # стриминг включён, только если интерфейс реально куда-то текст досылает: иначе это был
            # бы «стрим ради стрима» — та же задержка до ответа и лишний риск на оборванный поток.
            # F6/F7: право голоса теперь не только у env — флаг telegram.stream_replies (каталог)
            # и runtime-override деградации SLO могут выключить стрим на живую, без рестарта
            sink = on_delta if await self._stream_allowed(ctx) else None
            if sink is None:
                res: ChatResult = await self.services.gateway.chat(
                    route.role, messages, **call_kwargs
                )
            else:
                res = await self.services.gateway.chat_stream(
                    route.role, messages, on_text=sink, **call_kwargs
                )
            total_cost += res.cost_usd
            model = res.model
            messages.append(res.raw_message)
            if not res.tool_calls:
                return Reply(
                    text=_clean_reply(res),
                    trace_id=ctx.trace_id,
                    model=model,
                    cost_usd=round(total_cost, 6),
                    iterations=iterations,
                )

            pending: list[PendingAction] = []
            kill_active = await self._kill_switch_active()
            for call in res.tool_calls:
                try:
                    spec = self.registry.get(call.name)
                    args = spec.args.model_validate_json(call.arguments or "{}").model_dump()
                except (UnknownTool, ValueError) as exc:
                    messages.append(_tool_message(call.id, f"ERROR: инструмент недоступен: {exc}"))
                    continue
                decision, reason, rule_tag = self._decide(
                    ctx,
                    spec,
                    args,
                    kill_active=kill_active,
                    snapshot=ctx.extras.get("principal_snapshot"),
                )
                await self._event(
                    owner_id=ctx.owner_id,
                    event_type="policy.decision",
                    payload={
                        "tool": spec.name,
                        "decision": str(decision),
                        "reason": reason,
                        "rule": rule_tag,
                    },
                )
                await self.repro.policy(
                    trace_id=ctx.trace_id,
                    turn_no=step,
                    owner_id=ctx.owner_id,
                    tool=spec.name,
                    decision=str(decision),
                    reason=reason,
                    risk=str(spec.risk),
                    rule=rule_tag,
                    actor_id=ctx.extras.get("actor_id"),
                )
                if decision is Decision.DENY:
                    messages.append(_tool_message(call.id, f"DENIED: {reason}"))
                    await self._audit_tool(ctx, spec.name, args, "deny", reason, ok=True)
                    # F5: отказ — не приговор без права голоса. Не-владелец может обжаловать
                    # владельцу: отказ записывается в extras, кнопка появится на ответе
                    if int(ctx.extras.get("actor_id") or ctx.owner_id) != int(ctx.owner_id):
                        ctx.extras["appealable"] = True
                        ctx.extras.setdefault("denied", []).append(
                            {
                                "tool": spec.name,
                                "args": args,
                                "call_id": call.id,
                                "reason": reason,
                                "rule": rule_tag,
                            }
                        )
                    continue
                if decision is Decision.CONFIRM:
                    # placeholder обязателен: у каждого tool_call должен быть свой tool-ответ
                    pending.append(
                        PendingAction(
                            tool=spec.name,
                            args=args,
                            call_id=call.id,
                            reason=reason,
                            risk=str(spec.risk),
                            rule=rule_tag,
                        )
                    )
                    messages.append(_tool_message(call.id, CONFIRM_PLACEHOLDER))
                    continue
                out = await self._execute(
                    tool=spec.name, args=args, ctx=ctx, decision=str(decision)
                )
                messages.append(_tool_message(call.id, _tool_text(call.name, out)))

            if pending:
                reply = await self._request_confirmation(messages, ctx, pending)
                if ctx.extras.get("appealable") and reply.pending:
                    reply.appealable = True
                return reply
        return Reply(
            text=(
                f"Не уложился в {self.cfg.max_iterations} шагов: задача больше, чем один проход. "
                "Уточни, с чего начать."
            ),
            trace_id=ctx.trace_id,
            model=model,
            cost_usd=round(total_cost, 6),
            iterations=iterations,
            degraded=True,
        )

    async def _stage_appeal(
        self,
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        denied: list[dict[str, Any]],
    ) -> str | None:
        """Зарегистрировать апеллию: снимок отказанных действий живёт там же, где подтверждения.

        Возвращает id для кнопки или None, если KV лежит — «обжаловать нельзя» честнее кнопки,
        которая обещает и не может (тот же принцип, что у подтверждения без снимка).
        """
        appeal_id = uuid.uuid4().hex[:12]
        payload = {
            "pid": appeal_id,
            "owner_id": ctx.owner_id,
            "actor_id": ctx.extras.get("actor_id"),
            "trace_id": ctx.trace_id,
            "appeal": True,
            "actions": denied,
            "messages": orjson.loads(orjson.dumps(messages)),
        }
        stored = await self._kv_ok(
            "appeal.set",
            self.kv.set(
                pending_key(appeal_id), orjson.dumps(payload), ex=self.cfg.pending_ttl_seconds
            ),
        )
        if not stored:
            return None
        return appeal_id

    async def appeal_snapshot(self, appeal_id: str) -> dict[str, Any] | None:
        """Снимок апелляции для бота: «сколько действий под кнопкой» и жива ли ещё она.

        Чтение через тот же KV и тот же ключ, что у подтверждений: с TTL-истечением кнопка
        должна отвечать «истекло», а не тихо срабатывать на пустоте.
        """
        raw = await self._kv("appeal.get", self.kv.get(pending_key(appeal_id)), None)
        if not raw:
            return None
        try:
            data = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not data.get("appeal"):
            return None
        return data

    async def _request_confirmation(
        self,
        messages: list[dict[str, Any]],
        ctx: ToolContext,
        pending: list[PendingAction],
    ) -> Reply:
        pending_id = uuid.uuid4().hex[:12]
        payload = {
            "pid": pending_id,
            "owner_id": ctx.owner_id,
            # F2: чей запрос ждёт подтверждения — резолвер (кнопка) принадлежит владельцу,
            # но «кто попросил» не должно стираться кнопкой
            "actor_id": ctx.extras.get("actor_id"),
            "trace_id": ctx.trace_id,
            "actions": [
                {
                    "tool": p.tool,
                    "args": p.args,
                    "call_id": p.call_id,
                    "reason": p.reason,
                    "rule": p.rule,
                }
                for p in pending
            ],
            # снимок пройдёт через orjson: только JSON-совместимые значения
            "messages": orjson.loads(orjson.dumps(messages)),
        }
        stored = await self._kv_ok(
            "pending.set",
            self.kv.set(
                pending_key(pending_id), orjson.dumps(payload), ex=self.cfg.pending_ttl_seconds
            ),
        )
        if not stored:
            # кнопка без снимка состояния — это обещание, которое мы не можем выполнить
            note = (
                "Я не могу поставить действие на подтверждение: память сессий недоступна, "
                "а без неё решение владельца не дойдёт до инструмента. Действие НЕ выполнено."
            )
            await self._event(
                owner_id=ctx.owner_id,
                event_type="action.confirmation_unavailable",
                payload={"tools": [p.tool for p in pending]},
            )
            return Reply(text=note, degraded=True, trace_id=ctx.trace_id)
        await self._event(
            owner_id=ctx.owner_id,
            event_type="action.confirmation_requested",
            payload={"pending_id": pending_id, "tools": [p.tool for p in pending]},
        )
        body = "\n\n".join(p.render() for p in pending)
        return Reply(
            text=f"Нужно твоё подтверждение:\n\n{body}",
            pending=pending,
            pending_id=pending_id,
            trace_id=ctx.trace_id,
            iterations=len(messages),
        )

    async def _execute(
        self, *, tool: str, args: dict[str, Any], ctx: ToolContext, decision: str
    ) -> ToolResult:
        """Один вызов инструмента: policy уже сказал «можно», здесь — исполнение и запись.

        Результат возвращается размеченным (:class:`ToolResult`), потому что «что модель увидела»
        и «насколько этому можно верить» — разные вещи, и вторая без первой бесполезна.
        """
        spec = self.registry.get(tool)
        # F7: «web tools → не найдено» — объявленная деградация исполняется ЗДЕСЬ, до handler:
        # выключенный инструмент не должен ниNetworking, ни платить за транспорт; ответ —
        # честная строка для модели, а не исключение (модель продолжит без веба)
        overrides = ctx.extras.get("overrides") or await self._runtime_overrides()
        if overrides.get("tools.web.enabled") is False and _is_web_tool(spec):
            self._note_degraded("tools.web.enabled", ctx)
            return ToolResult(
                content=(
                    "Веб-инструменты деградированы (расход SLO-бюджета): ничего не найдено. "
                    "Это не ошибка запроса — попробуй заметки/память или ответь без веба."
                ),
                trust="system",
            )
        started = time.perf_counter()

        async def _run() -> ToolResult:
            return ToolResult.coerce(await spec.handler(spec.args.model_validate(args), ctx))

        try:
            out = await self._run_once(ctx, spec, args, _run)
        except Exception as exc:  # noqa: BLE001 - ошибка инструмента уходит модели как данные, не как 500
            log.exception("tool.failed", tool=tool)
            await self._audit_tool(ctx, tool, args, decision, repr(exc), ok=False)
            await self._event(
                owner_id=ctx.owner_id,
                event_type="tool.failed",
                payload={"tool": tool, "err": repr(exc)[:500]},
            )
            safe = self.services.gateway.dlp.redact(str(exc))
            return ToolResult(content=f"ERROR: {type(exc).__name__}: {safe[:800]}")
        await self._audit_tool(ctx, tool, args, decision, out.content, ok=True)
        await self._event(
            owner_id=ctx.owner_id,
            event_type="tool.executed",
            payload={
                "tool": tool,
                "decision": decision,
                "trust": out.trust,
                "result_len": len(out.content),
                "result": out.content[:1000],
            },
        )
        await self.repro.tool_run(
            trace_id=ctx.trace_id,
            turn_no=int(ctx.extras.get("turn_no", 0)),
            owner_id=ctx.owner_id,
            tool=tool,
            args=args,
            result=out.content,
            trust=out.trust,
            decision=decision,
            ok=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
            actor_id=ctx.extras.get("actor_id"),
        )
        if out.is_untrusted:
            # Верификатору нужно СЫРЬЁ, а не остаток после разметки: сверяем ответ с тем, что вернул
            # инструмент, иначе «не подтверждено» означало бы «не совпало с чужим пересказом»
            ctx.extras.setdefault("sources", []).append(out.content)
            out = await self._quarantine(tool, out, ctx)
        return out

    # ---------------- ----------- параллельность / права / флаги / деградация / идемпотентность

    async def _principal_snapshot(self, msg: Inbound) -> PrincipalSnapshot:
        """Кто спросил, что ему можно и сколько он уже потратил. Одна сборка на ход.

        Офлайн (без principals-магазина) — синтетический владелец: resolve_principal без БД
        строит Principal из конфига-росера, а snapshot_for без governance даёт только fallback-
        лимит. Смысл: путь через права проходит ВСЕГДА, «тесты без БД» не должны исполнять
        другой код (иначе RLS-логика проверялась бы только на проде).
        """
        roster: dict[int, Any] = {}
        raw_roster = str(getattr(self.cfg, "principal_roster", "") or "")
        if raw_roster:
            try:
                roster = parse_roster(raw_roster)
            except Exception as exc:  # noqa: BLE001 - кривой конфиг не должен валить ход
                log.warning("principals.roster_invalid", err=repr(exc)[:160])
        principal = await resolve_principal(
            self.principals, msg.actor, owner_id=msg.owner_id, roster=roster
        )
        snapshot = await snapshot_for(
            self.governance,
            principal,
            fallback_limit=float(getattr(self.cfg, "principal_daily_budget_usd", 0.0) or 0.0)
            or None,
        )
        return snapshot

    async def _flag_snapshot(self, actor_id: int) -> dict[str, Any]:
        """Снимок флагов хода (F6). Пустой — если источник не подключён: «не проверяли», не
        «выключено»."""
        if self.flags is None:
            return {}
        try:
            snap = await self.flags.snapshot(actor_id)
        except Exception as exc:  # noqa: BLE001 - флаг не имеет права валить ход; но видно в логе
            log.warning("flags.snapshot_failed", err=repr(exc)[:200])
            return {}
        return {key: dec.journal_value() for key, dec in snap.items()}

    async def _runtime_overrides(self) -> dict[str, Any]:
        """Деградационные переопределения (F7) с TTL-кэшем на 5 секунд.

        Кэш — не лень, а порядок цены: overrides читаются на каждый tool_call и каждый
        candidate-фильтр; 40 маленьких SELECT на ход превратили бы деградацию в новый источник
        лаг-метрики. 5 секунд — компромисс «включили деградацию → через 5 секунд вся ферма».
        """
        if self.overrides is None:
            return {}
        now = time.monotonic()
        if now - self._overrides_at < 5.0 and self._overrides_at > 0:
            return self._overrides_cache
        try:
            loaded = dict(await self.overrides.load())
        except Exception as exc:  # noqa: BLE001 — override-читалка не может уронить ответ
            log.warning("overrides.load_failed", err=repr(exc)[:200])
            return self._overrides_cache or {}
        self._overrides_cache = loaded
        self._overrides_at = now
        return loaded

    async def _stream_allowed(self, ctx: ToolContext) -> bool:
        """Стримить или нет: env-настройка → флаг каталога → override деградации (в таком
        приоритете).

        Флаг может и ВКЛЮЧИТЬ стрим там, где env его не просил (конверсия по перцентилю), и
        выключить живьём. Override — только выключает: «SLO горит» не может быть перебит флагом.
        """
        if not self.cfg.stream_replies:
            return False
        flags = ctx.extras.get("flags") or {}
        decision = flags.get("telegram.stream_replies")
        if isinstance(decision, Mapping) and decision.get("src") == "db":
            if not decision.get("on", True):
                return False
        overrides = ctx.extras.get("overrides")
        if overrides is None:
            overrides = await self._runtime_overrides()
            ctx.extras["overrides"] = overrides
        if overrides.get("telegram.stream_replies") is False:
            return False
        return True

    async def allow_streaming(self, owner_id: int, actor_id: int | None = None) -> bool:
        """Вопрос «открывать ли стрим-ответ» ДО входа в ход — его задаёт бот."""
        flags = await self._flag_snapshot(int(actor_id if actor_id is not None else owner_id))
        decision = flags.get("telegram.stream_replies")
        if (
            isinstance(decision, Mapping)
            and decision.get("src") == "db"
            and not decision.get("on", True)
        ):
            return False
        overrides = await self._runtime_overrides()
        return (
            bool(self.cfg.stream_replies) and overrides.get("telegram.stream_replies") is not False
        )

    def _decide(
        self,
        ctx: ToolContext,
        spec: Any,
        args: dict[str, Any],
        *,
        kill_active: bool,
        snapshot: PrincipalSnapshot | None,
    ) -> tuple[Decision, str, str]:
        """Policy-решение с RBAC-входом (F2+F5). Возвращает (решение, причина, тег правила)."""
        principal = snapshot.principal if snapshot is not None else None
        required = permission_for_tool(spec.name, writes=bool(spec.writes))
        missing = bool(principal is not None and required and not principal.can(required))
        outcome = self.policy.decide_full(
            ActionContext(
                tool=spec.name,
                risk=spec.risk,
                writes=bool(spec.writes),
                source_trust=ctx.source_trust,
                args=args,
                kill_switch=bool(kill_active) or bool(snapshot and snapshot.kill_active),
                required_action=required,
                permission_missing=missing,
                non_owner=principal is not None and not principal.is_owner,
                budget_ratio=float(ctx.extras.get("budget_ratio") or 0.0),
            )
        )
        return outcome.decision, outcome.reason, outcome.rule

    async def _run_once(
        self, ctx: ToolContext, spec: Any, args: dict[str, Any], fn: Any
    ) -> ToolResult:
        """Идемпотентность побочного эффекта (F8): activity_id = trace+шаг+инструмент.

        Реестр говорит «это уже исполнено с результатом R» — handler НЕ зовётся повторно:
        платёж не уйдёт дважды после краха на середине хода. Реестра нет (Null/durable=False) —
        просто исполняем; идемпотентность не может быть условием работы, только гарантией.
        """
        ledger = self.activity_ledger
        mode = str(getattr(self.cfg, "workflow_mode", "off"))
        if ledger is None or mode == "off":
            return await fn()
        from aegis.workflows.ledger import run_once  # noqa: PLC0415

        step_no = int(ctx.extras.get("turn_no", 0))
        activity_id = f"{ctx.trace_id}:{step_no}:tool:{spec.name}"

        async def _boxed() -> dict[str, Any]:
            return _result_json(await fn())

        outcome = await run_once(
            ledger,
            activity_id,
            _boxed,
            trace_id=ctx.trace_id,
            fencing_token=int(ctx.extras.get("fencing") or 0),
        )
        if outcome.state == "running":
            return ToolResult(
                content=(
                    "Этот шаг уже исполняется другим воркером; повторный эффект не запущен "
                    "(дедупликация по activity id — как задумано в durable-режиме)."
                ),
                trust="system",
            )
        return _result_from_json(outcome.result)

    def _note_degraded(self, switch: str, ctx: ToolContext) -> None:
        """Одно упоминание деградации на ход — в notices и в extras['degraded'] (в журнал)."""
        ctx.extras["degraded"] = True
        if switch in self._degrade_notes:
            return
        self._degrade_notes.add(switch)
        ctx.extras.setdefault("notices", []).append(f"деградация по SLO: {switch}")

    async def _end_turn(self, trace_id: str, handle: TurnHandle | None) -> None:
        """Финал хода: закрыть заявку (токеном) — очередь разбирает бот, не этот процесс."""
        try:
            await self.repro.end_turn(trace_id, handle.fencing_token if handle else None)
        except Exception as exc:  # noqa: BLE001 - финал не имеет права затмить ответ
            log.warning("turn.finish_failed", err=repr(exc)[:200])

    async def drain_next(self, owner_id: int) -> Inbound | None:
        """Очередь «один ход на владельца»: следующий сообщивший (F1). Пусто — None, без лжи."""
        item = await self.turns.pop_next(owner_id)
        if item is None:
            return None
        await self.turns.close_item(item.queue_id)
        payload = dict(item.payload or {})
        return Inbound(
            text=str(payload.get("text") or ""),
            owner_id=owner_id,
            source_trust=str(payload.get("source_trust") or "owner"),  # type: ignore[arg-type]
            actor_id=int(payload["actor_id"]) if payload.get("actor_id") is not None else None,
        )

    def _observe_turn(self, ctx: ToolContext, reply: Reply) -> None:
        """Метрики хода (F7): длительность + счётчик. Без реестра — no-op, не исключение."""
        if self.metrics is None:
            return
        try:
            started = float(ctx.extras.get("started_at") or 0.0)
            if started:
                self.metrics.histogram(
                    "aegis.turn.seconds",
                    help="время хода от входа до ответа",
                ).observe(max(time.perf_counter() - started, 0.0))
            self.metrics.counter("aegis.turn.total").inc(
                1.0,
                **{
                    "queued": str(bool(reply.queued)).lower(),
                    "degraded": str(bool(reply.degraded)).lower(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - метрика не роняет ответ
            log.warning("metrics.observe_failed", err=repr(exc)[:160])

    # ------------------------------------------------ промпт и память

    async def _verify_reply(self, reply: Reply, ctx: ToolContext, *, question: str) -> None:
        """Сверить ответ с источниками и сказать владельцу, если не сошлось.

        Проверка — постфактум и вне пути ответа: ни её собственный отказ, ни её находка не имеют
        права превратить «вот ответ» в «ошибка». Находка обязана дойти текстом, который добавили мы:
        модель не обязана признавать, что ошиблась числами.
        """
        sources = [str(item) for item in (ctx.extras.get("sources") or []) if str(item).strip()]
        if ctx.extras.get("degraded") or reply.degraded or not sources or not reply.text:
            return
        # F7: «verify» — из объявленных переключателей деградации: сверка стоит отдельный
        # LLM-вызов, и сжигающий бюджет сервис вправе его отключить (не «украсить», а отключить)
        if (ctx.extras.get("overrides") or {}).get("answer.verify.enabled") is False:
            self._note_degraded("answer.verify.enabled", ctx)
            return
        if not self.verifier.should_verify(answer=reply.text, sources=sources):
            return
        verdict = await self.verifier.verify(
            question=question,
            answer=reply.text,
            sources=sources,
            trace_id=ctx.trace_id,
            turn_no=int(ctx.extras.get("turn_no", 0)),
            owner_id=ctx.owner_id,
        )
        await self._event(
            owner_id=ctx.owner_id,
            event_type="answer.verified",
            payload={
                "ok": verdict.ok,
                "severity": verdict.severity,
                "mode": verdict.mode,
                "problems": list(verdict.problems)[:6],
            },
        )
        await self.repro.verdict(
            trace_id=ctx.trace_id,
            turn_no=int(ctx.extras.get("turn_no", 0)),
            owner_id=ctx.owner_id,
            ok=verdict.ok,
            severity=verdict.severity,
            checked=list(verdict.checked),
            problems=list(verdict.problems),
            model=reply.model,
            cost_usd=verdict.cost_usd,
            latency_ms=verdict.latency_ms,
            prompt_ids=[dict(item) for item in verdict.prompt_ids],
            payload=verdict.payload,
        )
        notice = verdict.notice()
        if notice:
            reply.cost_usd = round(reply.cost_usd + verdict.cost_usd, 6)
            ctx.extras.setdefault("notices", []).append(notice)

    async def _quarantine(self, tool: str, out: ToolResult, ctx: ToolContext) -> ToolResult:
        """Внешний текст попадает к планировщику только через карантинную модель (dual-LLM).

        Порядок записи важен: ``tool_run`` уже содержит сырой ответ инструмента, а в messages уйдёт
        размеченный. Так журнал отвечает на оба вопроса — «что вернул мир» и «что увидела модель» —
        и не приходится выбирать между безопасностью и воспроизводимостью.
        """
        if ctx.extras.get("degraded") or len(out.content) < self.cfg.quarantine_min_chars:
            return out
        digested = await quarantine_payload(
            self.services.gateway,
            tool=tool,
            raw=out.content,
            sources=[line for line in out.source.splitlines() if line.strip()],
            cfg=self.cfg,
            trace_id=ctx.trace_id,
        )
        if digested is None:
            return out
        if digested.has_instructions:
            # владелец узнаёт о попытке инъекции из нашего текста, а не из доброй воли модели
            ctx.extras.setdefault("notices", []).append(
                "в источнике найдена попытка инструкции ассистенту (не выполнена): "
                + "; ".join(digested.instructions)[:200]
            )
            await self._event(
                owner_id=ctx.owner_id,
                event_type="quarantine.instructions",
                payload={"tool": tool, "found": list(digested.instructions)[:4]},
            )
        return ToolResult(
            content=digested.text,
            trust=out.trust,
            source=out.source,
            media_type=out.media_type,
            ref_id=out.ref_id,
        )

    async def _system_message(
        self, *, level: int, overrides: Mapping[str, Any] | None = None
    ) -> dict[str, str]:
        notes: list[str] = []
        facts: list[str] = []
        try:
            facts = await self.services.facts.recent(50)
        except Exception as exc:  # noqa: BLE001 - нет БД — нет долговременной памяти, но бот жив
            notes.append(f"долговременная память недоступна ({type(exc).__name__})")
        if await self._kill_switch_active():
            notes.append("kill switch активен: записи запрещены, работай только на чтение")
        if level >= 1:
            notes.append("бюджет близок к лимиту: отвечай короче, инструментов — минимум")
        if overrides and overrides.get("tools.web.enabled") is False:
            notes.append(
                "веб-инструменты деградированы (SLO-бюджет): они вернут «не найдено»; "
                "не обещай актуальных данных из веба, помечай неуверенность явно"
            )
        tools = [(t.name, t.description) for t in self.registry.all() if t.enabled]
        return {
            "role": "system",
            "content": build_system_prompt(
                self.cfg.timezone, self.cfg.base_currency, facts, tools, notes=notes
            ),
        }

    def _user_content(self, msg: Inbound) -> str:
        text = msg.text or "(без текста)"
        if msg.attachments:
            kinds = ", ".join(sorted({a.kind for a in msg.attachments}))
            text += (
                f"\n[вложения: {len(msg.attachments)} ({kinds}); для изображений — analyze_image]"
            )
        return text

    async def _kv(self, op: str, coro: Any, default: Any) -> Any:
        """KV — внешний кэш, а не источник истины: сбой = деградация, не потеря сообщения.

        История диалога и pending-подтверждения живут в Redis. При его недоступности
        ход обязан состояться (модель, инструменты, БД — всё на месте), просто без
        «памяти про последние реплики». Исключение отсюда нарушило бы принцип 5.
        """
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - любое падение кэша трактуем одинаково
            log.warning("kv.unavailable", op=op, err=repr(exc)[:200])
            self.kv_degraded = True
            return default

    async def _kv_ok(self, op: str, coro: Any) -> bool:
        """Удалась ли операция KV. Значение не смотрим: redis-`SET` без flags отвечает None.

        Нужен именно этот различимый ответ, потому что «подтверждение» без снимка состояния
        невыполнимо — и честнее отказать, чем показать кнопку, которая в никуда.
        """
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 - любое падение кэша трактуем одинаково
            log.warning("kv.unavailable", op=op, err=repr(exc)[:200])
            self.kv_degraded = True
            return False
        return True

    async def _history(self, owner_id: int) -> list[dict[str, Any]]:
        raw = await self._kv("history.get", self.kv.get(history_key(owner_id)), None)
        if not raw:
            return []
        try:
            data = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return []
        return list(data) if isinstance(data, list) else []

    async def _save_history(
        self, owner_id: int, messages: Sequence[dict[str, Any]], *, final_text: str
    ) -> None:
        """В кэше остаются только реплики user/assistant без служебных tool-вставок."""
        clean: list[dict[str, str]] = []
        for msg in messages:
            if msg.get("role") not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if (
                not isinstance(content, str)
                or content == CONFIRM_PLACEHOLDER
                or not content.strip()
            ):
                continue
            clean.append({"role": str(msg["role"]), "content": content[:8000]})
        if final_text.strip():
            clean.append({"role": "assistant", "content": final_text[:8000]})
        await self._kv(
            "history.set",
            self.kv.set(
                history_key(owner_id),
                orjson.dumps(clean[-self.cfg.history_limit :]),
                ex=self.cfg.history_ttl_seconds,
            ),
            None,
        )

    # ------------------------------------------------ observability

    async def _notes_index_state(self) -> dict[str, Any]:
        """Сколько заметок ждут эмбеддинга — строка для `/status`, а не диагностика.

        Счётчик читается у магазина, если он умеет: у Null/тестовых магазинов его нет, и это честно
        означает «не видно», без попытки сходить в базу, которой может не быть.
        """
        count = getattr(self.services.notes, "count_pending", None)
        if count is None:
            return {"available": False}
        try:
            return {"available": True, "pending": int(await count())}
        except Exception as exc:  # noqa: BLE001 - статус не имеет права падать из-за отчёта
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"[:160]}

    async def _kill_switch_active(self) -> bool:
        state = await self._kill_switch_state()
        return bool(state.get("active"))

    async def _kill_switch_state(self) -> dict[str, Any]:
        if self.kill_switch is None:
            return {"active": False}
        try:
            state = await self.kill_switch.state()
        except Exception as exc:  # noqa: BLE001 - недоступный Redis не должен блокировать ответы
            log.warning("killswitch.unavailable", err=repr(exc))
            return {"active": False, "error": repr(exc)[:200]}
        return {"active": state.active, "reason": state.reason}

    async def _event(self, *, owner_id: int, event_type: str, payload: dict[str, Any]) -> None:
        await self.events.append(
            stream_type="owner",
            stream_id=f"owner:{owner_id}",
            event_type=event_type,
            payload=payload,
            metadata={"prompt_version": PROMPT_VERSION},
        )

    async def _audit_tool(
        self,
        ctx: ToolContext,
        tool: str,
        args: dict[str, Any],
        decision: str,
        result: str | None,
        *,
        ok: bool,
    ) -> None:
        await self.audit.tool_run(
            trace_id=ctx.trace_id,
            tool=tool,
            args=args,
            decision=decision,
            result=result,
            ok=ok,
            owner_id=ctx.owner_id,
        )


# --------------------------------------------------------------- helpers


def _scope_for(msg: Inbound, snapshot: PrincipalSnapshot) -> object:
    """Пересвязка contextvar на реальный kind принципала. Возвращает токен для finally.

    Функция, а не метод: состояние хода живёт в asyncio-контексте задачи, а не на Supervisor
    (один объект на N параллельных ходов — атрибут-токен бы затирался между задачами).
    """
    return set_scope(PrincipalScope(msg.actor, snapshot.principal.kind, msg.owner_id))


def _result_json(out: ToolResult) -> dict[str, Any]:
    """ToolResult → то, что переживёт рестарт процесса (jsonb в реестре активностей)."""
    return {"content": out.content, "trust": out.trust, "source": out.source}


def _result_from_json(data: Mapping[str, Any]) -> ToolResult:
    return ToolResult(
        content=str(data.get("content") or ""),
        trust=str(data.get("trust") or "system"),  # type: ignore[arg-type]
        source=str(data.get("source") or ""),
    )


_WEB_TOOL_PREFIXES = ("web", "search", "fetch", "news", "rates")


def _is_web_tool(spec: Any) -> bool:
    """«Веб-инструмент» для выключателя деградации — по имени и флагу untrusted-источника.

    Список не магический: внешний источник = сеть; локальные инструменты (notes, memory,
    reminders) при «веб лежит» обязаны работать — в этом весь смысл graceful degradation.
    """
    name = str(getattr(spec, "name", "")).lower()
    if any(part in name for part in _WEB_TOOL_PREFIXES):
        return True
    return bool(getattr(spec, "is_external", False))


def _memory_turns() -> TurnLedger:
    """Офлайн-режим: очередь и заявки живут в процессе. durable=False видно в /status."""
    return NullTurnLedger()


def _notes_of(ctx: ToolContext) -> list[str]:
    """Снимок замечаний инструментов — до того, как их приклеят к ответу и очистят."""
    return [str(note) for note in (ctx.extras.get("notices") or []) if str(note).strip()]


def _append_notices(reply: Reply, ctx: ToolContext) -> None:
    """Причины отказа инструментов доходят до владельца текстом, а не обещанием модели.

    Модель может пересказать находку как угодно; «поиск лежит» не имеет права превратиться в
    «попробуйте уточнить запрос» — для этого и есть отдельная строка, которую добавляем мы сами.
    """
    notices = [str(note) for note in (ctx.extras.get("notices") or []) if str(note).strip()]
    if not notices:
        return
    #: снимаем список: ветка деградации и общий путь зовут helper подряд — не должно быть двух
    #  одинаковых «⚠️» в конце одного сообщения
    ctx.extras["notices"] = []
    tail = "\n\n⚠️ " + "; ".join(dict.fromkeys(notices))[:400]
    reply.text = (reply.text + tail)[:MAX_MESSAGE_CHARS]


async def _reminders_status(store: Any, cfg: Settings) -> dict[str, Any]:
    """Срез расписания для ``/status``: включено ли, сколько живёт, сколько просрочено.

    Счётники берутся у самого магазина: у Null-магазина их нет, и это честно означает «расписания
    нет» — без попытки сходить в несуществующую БД.
    """
    info: dict[str, Any] = {
        "enabled": bool(getattr(store, "enabled", False)) and cfg.reminders_enabled,
        "batch": cfg.reminders_batch,
    }
    counts = getattr(store, "counts", None)
    if counts is not None:
        try:
            info.update(await counts())
        except Exception as exc:  # noqa: BLE001 - статус не имеет права падать из-за отчёта
            info["error"] = f"{type(exc).__name__}: {exc}"[:160]
    return info


def _tool_text(tool: str, result: ToolResult) -> str:
    """Что реально уйдёт в контекст модели как результат инструмента.

    Правило «внешнее помечено» исполняется здесь, а не в каждом handler'е: забыть обёртку в одном
    инструменте означает отдать веб-страницу право командовать агентом. Инструменты, которые уже
    обернули сами (``<untrusted>`` в тексте), не оборачиваем повторно — двойная рамка учит модель
    считать метку оформлением.
    """
    if not result.is_untrusted or "<untrusted" in result.content[:400]:
        return result.content
    return wrap_untrusted(tool, result.content)


def _last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    """Вопрос хода для верификатора: после подтверждения реплики владельца в списке нет."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            return str(content) if content else ""
    return ""


def _tool_message(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content[:16000]}


def _patch_tool_message(messages: list[dict[str, Any]], call_id: str, content: str) -> None:
    """Заменить placeholder подтверждения на реальный результат, сохранив формат OpenAI."""
    for msg in reversed(messages):
        if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
            msg["content"] = content[:16000]
            return


def _clean_reply(res: ChatResult) -> str:
    content: Any = res.content
    if isinstance(content, list):  # отдельные провайдеры отдают parts-списком
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    text = (content or "").strip()
    if not text:
        return "Модель вернула пустой ответ. Уточни запрос или попробуй ещё раз."
    return text
