"""Флаги возможностей (F6): определение в БД, детерминированный перцентиль, оценка в журнале.

Env-флаг («включить нельзя, откатить нельзя без рестарта») не даёт трёх вещей, которые здесь
главные:

1. **процентаж без монетки.** ``bucket = sha256(f"{flag}\\x1f{actor}") % 100`` — детерминированно
   от (флаг, принципал): один и тот же человек получает один и тот же режим в любом процессе и
   после рестарта, а «random» ломал бы и A/B, и воспроизводимость (тот же вход — разный режим);
2. **запись оценки в журнал.** Каждый ход получает ``params.flags``: «в каком режиме отвечал этот
   ход» пересчитывается из журнала, а не угадывается по дате деплоя;
3. **гигиену.** Флаг, который живёт на 100% дольше N дней, — не флаг, а легаси-ветвление: CI-тест
   и ``aegis flags stale`` падают, пока его не удалили. У каждого флага — golden-кейс в обоих
   состояниях (``evals/flags_v1.jsonl``), иначе он не проверяется, а «работает на веру».

``FLAG_CATALOG`` — каталог *известных* флагов приложения: по нему строятся дефолты (env-мост),
он же — источник для тестов гигиены. Флаг, которого нет в каталоге, живёт в БД и работает, но
doctor на него посмотрит косо.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy import text

from aegis.platform.db import SessionFactory, session

__all__ = [
    "EnvFlagSource",
    "FlagDecision",
    "FlagEngine",
    "FLAG_CATALOG",
    "SqlFlagSource",
    "bucket_for",
]

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FlagSpec:
    """Что флаг делает, кто за него отвечает и чем он проверяется в golden-наборе."""

    key: str
    description: str
    owner: str = "owner"
    #: имена кейсов evals/flags_v1.jsonl, покрывающих оба состояния: без них флаг протухает
    golden: tuple[str, str] = ("", "")
    #: env-мост: из какой настройки брать дефолт, пока флага нет в БД
    env_default: str | None = None


#: каталог заведений. Каждое заведение здесь обязано иметь пару golden-кейсов (CI-тест
#: test_flags_hygiene это проверяет) — иначе добавление флага не станет бесплатным
#: переключателем «на всякий случай»
FLAG_CATALOG: dict[str, FlagSpec] = {
    spec.key: spec
    for spec in (
        FlagSpec(
            "telegram.stream_replies",
            "досылать ответ правкой сообщения (ADR-0011)",
            golden=("flag-stream-off", "flag-stream-on"),
            env_default="stream_replies",
        ),
        FlagSpec(
            "events.outbox_relay",
            "публиковать outbox в NATS (ADR-0013)",
            golden=("flag-relay-off", "flag-relay-on"),
            env_default="outbox_relay_enabled",
        ),
        FlagSpec(
            "agent.verify_answers",
            "сверять ответ с источниками до показа владельцу (ADR-0009)",
            golden=("flag-verify-off", "flag-verify-on"),
            env_default="verify_enabled",
        ),
        FlagSpec(
            "notes.hybrid_search",
            "гибридный поиск BM25/trgm + вектор через RRF + rerank (ADR-0015)",
            golden=("flag-hybrid-off", "flag-hybrid-on"),
            env_default=None,
        ),
        FlagSpec(
            "policy.rules_active",
            "оценивать write через декларативные правила вместо кода (F5)",
            golden=("flag-policy-rules-off", "flag-policy-rules-on"),
            env_default=None,
        ),
        FlagSpec(
            "slo.degrade_enabled",
            "автоматическая деградация при провале SLO (F7)",
            golden=("flag-slo-degrade-off", "flag-slo-degrade-on"),
            env_default="slo_enforce_degradation",
        ),
    )
}


@dataclass(frozen=True, slots=True)
class FlagDecision:
    """Итог оценки: включён ли флаг для этого актора и ПОЧЕМУ. 'почему' — ради журнала."""

    key: str
    on: bool
    basis: str  # 'allowlist' | 'denylist' | 'bucket' | 'full' | 'off' | 'default'
    bucket: int = -1
    percent: int = 0
    source: str = "default"  # 'db' | 'env'

    def journal_value(self) -> dict[str, Any]:
        """Форма для ``params.flags``: compact, без objects-repr; годна для пересчёта A/B."""
        out: dict[str, Any] = {"on": self.on, "basis": self.basis}
        if self.basis == "bucket":
            out["bucket"] = self.bucket
            out["pct"] = self.percent
        if self.source == "env":
            out["src"] = "env"
        return out


class FlagSource(Protocol):
    async def load(self) -> Mapping[str, dict[str, Any]]: ...


def bucket_for(key: str, actor_id: int) -> int:
    """Детерминированный процентиль: хэш от (flag, actor) — не от случайности процесса."""
    digest = hashlib.sha256(f"{key}\x1f{int(actor_id)}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


class EnvFlagSource:
    """Фолбэк без БД: настройки env как дефолты каталога. Изменить на лету нельзя — это и есть
    причина существования SqlFlagSource; источник существует, чтобы «нет БД» ≠ «нет бота»."""

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg

    async def load(self) -> Mapping[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, spec in FLAG_CATALOG.items():
            if spec.env_default is None:
                continue
            value = bool(getattr(self._cfg, spec.env_default, False))
            out[key] = {"percent": 100 if value else 0, "allow": [], "deny": []}
        return out


class SqlFlagSource:
    """Флаги из ``platform.feature_flags`` с коротким кешом.

    Кеш — не «состояние, которое жалко потерять»: это TTL источника истины в БД, и провал
    чтения означает прошлую snapshot-оценку, а не иной режим. Откат флага без рестарта =
    UPDATE строки; следующий процесс (или TTL) увидит новый режим, журнал обоих периодов
    читается пересчётом.
    """

    def __init__(
        self,
        cfg: Any | None = None,
        *,
        session_factory: SessionFactory | None = None,
        fallback: FlagSource | None = None,
    ) -> None:
        self._sm = session_factory
        self._ttl = float(getattr(cfg, "flags_cache_s", 30.0) if cfg else 30.0)
        self._fallback = fallback
        self._cache: dict[str, dict[str, Any]] = {}
        self._loaded_at = 0.0
        self._db_loaded_once = False

    async def load(self) -> Mapping[str, dict[str, Any]]:
        now = time.monotonic()
        if self._cache and now - self._loaded_at < self._ttl:
            return self._cache
        try:
            sm = self._sm or session
            async with sm() as s:
                rows = (
                    (
                        await s.execute(
                            text(
                                "SELECT key, percent, allow_principals, deny_principals, stage"
                                " FROM platform.feature_flags WHERE stage <> 'retired'"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
        except Exception as exc:  # noqa: BLE001 — флаги не должны ронять ход: фолбэк на env
            log.warning("flags.load_failed", err=repr(exc)[:200])
            if self._fallback is not None and not self._db_loaded_once:
                return await self._fallback.load()
            return self._cache
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            out[str(row["key"])] = {
                "percent": int(row["percent"]),
                "allow": [int(v) for v in (row["allow_principals"] or [])],
                "deny": [int(v) for v in (row["deny_principals"] or [])],
                "stage": str(row["stage"]),
            }
        self._db_loaded_once = True
        # env-дефолты дописываем ПОД db-значения: отсутствие строки в БД ≠ отсутствие флага,
        # и «переезд с env на таблицу» не должен быть big-bang
        if self._fallback is not None:
            merged = dict(await self._fallback.load())
            merged.update(out)
            out = merged
        self._cache = out
        self._loaded_at = now
        return out


class FlagEngine:
    """Оценка флага для принципала. Спорить с `bucket_for` бесполезно — она и есть контракт."""

    def __init__(self, source: FlagSource | None = None) -> None:
        self._source = source or EnvFlagSource(None)

    async def evaluate(self, key: str, actor_id: int) -> FlagDecision:
        defs = await self._source.load()
        row = defs.get(key)
        if row is None:
            spec = FLAG_CATALOG.get(key)
            if spec is not None and spec.env_default is not None:
                return FlagDecision(key, False, "default")
            return FlagDecision(key, False, "default")
        allow = {int(v) for v in row.get("allow") or []}
        deny = {int(v) for v in row.get("deny") or []}
        actor = int(actor_id)
        percent = int(row.get("percent") or 0)
        source = "db" if "stage" in row else "env"
        if actor in deny:
            return FlagDecision(key, False, "denylist", source=source, percent=percent)
        if actor in allow:
            return FlagDecision(key, True, "allowlist", source=source, percent=percent)
        if percent <= 0:
            return FlagDecision(key, False, "off", source=source, percent=percent)
        if percent >= 100:
            return FlagDecision(key, True, "full", source=source, percent=percent)
        bucket = bucket_for(key, actor)
        on = bucket < percent
        return FlagDecision(key, on, "bucket", bucket=bucket, percent=percent, source=source)

    async def snapshot(
        self, actor_id: int, keys: Iterable[str] | None = None
    ) -> dict[str, FlagDecision]:
        wanted = list(keys) if keys is not None else sorted(FLAG_CATALOG)
        return {key: await self.evaluate(key, actor_id) for key in wanted}
