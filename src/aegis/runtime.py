"""Сборка приложения: один граф зависимостей для ``aegis bot``, worker'ов и тестов.

Зачем отдельный модуль: бот, CLI и (позже) Temporal-worker должны собирать одинаковые объекты,
но по-разному их закрывать. Разбор «кто кому что передаёт» в одном месте — единственный способ
не получить три слегка отличающихся окружения (то, что живёт в dev, никогда не совпадает с продом).

Здесь же решение «есть ли БД»: если Postgres недоступен, supervisor получает NullEventSink/NullAudit
и домены с реальными репозиториями — бот продолжает отвечать, а записи падают с внятной ошибкой.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self, cast

import redis.asyncio as aioredis
import structlog

from aegis.agents.services import Services
from aegis.agents.supervisor import Supervisor
from aegis.agents.tools.registry import ToolRegistry
from aegis.governance.audit import AuditLog, NullAudit, SqlAuditLog
from aegis.governance.killswitch import KillSwitch
from aegis.governance.policy import PolicyEngine
from aegis.governance.recorder import DecisionRecorder, NullDecisionRecorder, SqlDecisionRecorder
from aegis.planning.reminders import NullReminderStore, ReminderStore, SqlReminderStore
from aegis.platform.config import Settings, settings
from aegis.platform.db import get_sessionmaker
from aegis.platform.events.sink import (
    BestEffortEventSink,
    EventSink,
    NullEventSink,
    OutboxEventSink,
)
from aegis.platform.gateway.client import ModelGateway
from aegis.platform.gateway.cost import CostGovernor
from aegis.platform.gateway.dlp import DLP
from aegis.platform.kv import KV
from aegis.platform.kv_memory import MemoryKV
from aegis.platform.logging import setup_logging

__all__ = ["App", "build_app"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class App:
    cfg: Settings
    #: клиент KV (redis-py или MemoryKV): нужен для aclose/ping в CLI и на shutdown
    redis: Any
    cost: CostGovernor
    gateway: ModelGateway
    services: Services
    policy: PolicyEngine
    kill_switch: KillSwitch
    supervisor: Supervisor
    events: EventSink
    audit: AuditLog
    #: журнал решений (M1). Отдельным полем, а не «внутри audit»: у него своя ответственность —
    #  доказуемость хода, и «аудит пишется, а журнал нет» — реальное состояние, которое надо видеть
    repro: DecisionRecorder
    registry: ToolRegistry
    db_ready: bool
    #: свой ли KV-клиент: чужой (инжектированный тестом) не закрываем
    owns_kv: bool = True
    #: fallback-дедуп апдейтов без БД (см. seen_update); dict как ordered set
    _dedup_mem: dict[int, None] = field(default_factory=dict)

    async def seen_update(self, update_id: int, chat_id: int, *, kind: str = "message") -> bool:
        """Впервые ли этот апдейт (F1 «update_id дедуплицируется в Postgres»).

        True — обработать, False — дубль (молча проглотить). Два процесса на одной базе
        конкурируют на INSERT...ON CONFLICT: победитель ровно один. Без БД — in-memory-множество
        в этом процессе: «помним только про себя» должно быть свойством режима, а не тайным
        обещанием, поэтому durable-ответ здесь возвращает None и мидлвари это видно.
        """
        if self.db_ready:
            from sqlalchemy import text

            from aegis.platform.db import session

            sql = (
                "INSERT INTO platform.telegram_updates (update_id, chat_id, kind)"
                " VALUES (:uid, :chat, :kind) ON CONFLICT (update_id) DO NOTHING"
                " RETURNING update_id"
            )
            try:
                async with session() as s:
                    fresh = await s.scalar(
                        text(sql).bindparams(uid=int(update_id), chat=int(chat_id), kind=kind)
                    )
                    await s.commit()
                return fresh is not None
            except Exception as exc:  # noqa: BLE001 — сбой дедупа не право ронять ответ
                log.warning("updates.dedup_db_failed", err=repr(exc)[:200])
        # память процесса: LRU-порядок dict'а + жёсткий потолок — «когда-то видели» переживает
        # часы, но не вечность, и 10k апдейтов не раздувают RSS
        if update_id in self._dedup_mem:
            return False
        self._dedup_mem[update_id] = None
        if len(self._dedup_mem) > 10_000:
            self._dedup_mem.pop(next(iter(self._dedup_mem)))
        return True

    async def probe_schema(self) -> bool:
        """Есть ли таблицы платформы. Connect-ok не равен schema-ok: без этого шага бот
        отвечает, но трассу не пишет, и выглядит это как «всё хорошо»."""
        if not self.db_ready:
            return False
        from sqlalchemy import text

        from aegis.platform.db import session

        probe = "SELECT to_regclass('platform.events'), to_regclass('platform.llm_calls')"
        try:
            async with session() as s:
                row = (await s.execute(text(probe))).first()
        except Exception as exc:  # noqa: BLE001
            log.error("db.probe_schema_failed", err=repr(exc)[:300])
            return False
        names = ("platform.events", "platform.llm_calls")
        missing = [name for name, reg in zip(names, row or (), strict=True) if not reg]
        if missing:
            log.error(
                "db.schema_missing",
                tables=",".join(missing),
                hint="aegis bot запущен без миграций: alembic upgrade head",
            )
            return False
        log.info("db.schema_ok")
        return True

    async def aclose(self) -> None:
        await self.gateway.aclose()
        if not self.owns_kv:
            return
        try:
            await self.redis.aclose()
        except Exception as exc:  # noqa: BLE001 - закрытие не должно ронять shutdown
            log.debug("redis.close_failed", err=repr(exc))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


def _probe_db() -> bool:
    """Есть ли смысл писать события/аудит. Проверяется конфиг, а не сетевой доступ: ping потом
    скажет своё слово в BestEffort-обёртке (она и проглотит редкие отказы БД)."""
    try:
        get_sessionmaker()
        return True
    except Exception as exc:  # noqa: BLE001 - нет драйвера/URL -> работаем без трассировки
        log.warning("db.unavailable", err=repr(exc)[:300])
        return False


def build_app(
    *,
    registry: ToolRegistry,
    cfg: Settings | None = None,
    redis: Any | None = None,
    configure_logging: bool = True,
) -> App:
    cfg = cfg or settings()
    if configure_logging:
        setup_logging(cfg.log_level, json_output=cfg.log_json)

    own_kv: Any = None
    if redis is not None:
        kv = cast(KV, redis)
    elif cfg.kv_backend == "memory":
        # демо/CI: тот же узкий порт, только в памяти процесса (см. модуль kv_memory)
        own_kv = MemoryKV()
        kv = cast(KV, own_kv)
        log.warning(
            "kv.in_memory",
            note="история, pending и дневной бюджет живут только в этом процессе",
        )
    else:
        own_kv = aioredis.from_url(cfg.redis_url, decode_responses=False)
        # redis-py шире нашего порта (десятки методов); сужаем осознанно — приложение
        # видит только KV, и моки в тестах обязаны тому же
        kv = cast(KV, own_kv)
    cost = CostGovernor(kv, cfg.daily_budget_usd, timezone=cfg.timezone)
    db_ready = _probe_db()
    events: EventSink = BestEffortEventSink(OutboxEventSink()) if db_ready else NullEventSink()
    audit: AuditLog = SqlAuditLog() if db_ready else NullAudit()
    repro: DecisionRecorder = (
        SqlDecisionRecorder(cfg) if (db_ready and cfg.repro_enabled) else NullDecisionRecorder()
    )
    gateway = ModelGateway(cfg, cost, recorder=_make_recorder(audit, repro), dlp=DLP())
    # напоминания без БД бессмысленны (тик живёт отдельно от процесса бота), поэтому магазин
    # подключается ровно тогда же, когда и остальная запись: db_ready — единственный источник истины
    reminders: ReminderStore = SqlReminderStore() if db_ready else NullReminderStore()
    services = Services.build(gateway, repro=repro, reminders=reminders)
    policy = PolicyEngine.from_settings(cfg)
    kill_switch = KillSwitch(kv)
    supervisor = Supervisor(
        services=services,
        registry=registry,
        policy=policy,
        kv=kv,
        cfg=cfg,
        events=events,
        audit=audit,
        kill_switch=kill_switch,
        recorder=repro,
    )
    log.info(
        "app.built",
        db_ready=db_ready,
        repro=repro.enabled,
        owns_kv=own_kv is not None,
        budget=cfg.daily_budget_usd,
        models=gateway.describe()["models"],
    )
    return App(
        cfg=cfg,
        redis=kv,
        cost=cost,
        gateway=gateway,
        services=services,
        policy=policy,
        kill_switch=kill_switch,
        supervisor=supervisor,
        events=events,
        audit=audit,
        repro=repro,
        registry=registry,
        db_ready=db_ready,
    )


def _make_recorder(audit: AuditLog, repro: DecisionRecorder) -> Any:
    """Один хук шлюза — два получателя: метрики в аудит, содержимое в журнал решений.

    Порядок фиксирован и не влияет на правильность (каждый писатель глотает свои ошибки сам), но
    важен для цены вопроса: аудит — про бюджет и доступность, журнал — про доказуемость. Ни один из
    них не имеет права лишить владельца ответа.
    """

    async def record(record: Any) -> None:
        await audit.llm_call(record)
        await repro.on_llm_call(record)

    return record
