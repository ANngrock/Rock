"""Сборка приложения: один граф зависимостей для ``aegis bot``, worker'ов и тестов.

Зачем отдельный модуль: бот, CLI и (позже) Temporal-worker должны собирать одинаковые объекты,
но по-разному их закрывать. Разбор «кто кому что передаёт» в одном месте — единственный способ
не получить три слегка отличающихся окружения (то, что живёт в dev, никогда не совпадает с продом).

Здесь же решение «есть ли БД»: если Postgres недоступен, supervisor получает NullEventSink/NullAudit
и домены с реальными репозиториями — бот продолжает отвечать, а записи падают с внятной ошибкой.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from aegis.platform.logging import setup_logging

__all__ = ["App", "build_app"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class App:
    cfg: Settings
    #: настоящий клиент redis-py (шире порта KV): нужен для aclose/ping в CLI и shutdown
    redis: Any
    cost: CostGovernor
    gateway: ModelGateway
    services: Services
    policy: PolicyEngine
    kill_switch: KillSwitch
    supervisor: Supervisor
    events: EventSink
    audit: AuditLog
    registry: ToolRegistry
    db_ready: bool

    async def aclose(self) -> None:
        await self.gateway.aclose()
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

    raw_kv = (
        redis if redis is not None else aioredis.from_url(cfg.redis_url, decode_responses=False)
    )
    # redis-py шире нашего порта (десятки методов); сужаем осознанно — приложение видит только KV
    kv: KV = cast(KV, raw_kv)
    cost = CostGovernor(kv, cfg.daily_budget_usd, timezone=cfg.timezone)
    db_ready = _probe_db()
    events: EventSink = BestEffortEventSink(OutboxEventSink()) if db_ready else NullEventSink()
    audit: AuditLog = SqlAuditLog() if db_ready else NullAudit()
    gateway = ModelGateway(cfg, cost, recorder=_make_recorder(audit), dlp=DLP())
    services = Services.build(gateway)
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
    )
    log.info(
        "app.built",
        db_ready=db_ready,
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
        registry=registry,
        db_ready=db_ready,
    )


def _make_recorder(audit: AuditLog) -> Any:
    async def record(record: Any) -> None:
        await audit.llm_call(record)

    return record
