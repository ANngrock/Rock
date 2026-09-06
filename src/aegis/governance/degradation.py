"""Автоматическая деградация (F7): провалили SLO — переключили ветки, записали факт в журнал.

«Право на деградацию» означает три вещи одновременно:

1. приложение переключает РОВНО объявленное (``degrade:`` в slo.yml). Ни «заодно выключим и
   верификатор», ни «молча деградируем навсегда»: список исходит из файла, и тест сверяет
   переключённое с ним;
2. факт переключения — запись журнала (kind='system'), а не только счётчик метрик. Через месяц
   вопрос «почему бот тогда не стримил» решается запросом к журналу;
3. переключатель — строка в Postgres (``platform.runtime_overrides``) с TTL, а не флаг процесса:
   второй экземпляр увидит то же самое, а восстановление — это истечение аренды или явный
   сним, не «перезапусти и помолись».

Контроллер намеренно глупый: он применяет решения ``SloReport``, не придумывая своих. «Придумывать»
— дело SLO-источников и порога в slo.yml, и это видно в diff'е файла, а не в поведении процесса.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import orjson
import structlog
from sqlalchemy import text

from aegis.platform.db import SessionFactory, session
from aegis.platform.slo import SUPPORTED_SWITCHES, SloReport

__all__ = [
    "OVERRIDE_KEYS",
    "DegradationController",
    "MemoryOverrides",
    "Override",
    "Overrides",
    "SqlOverrides",
]

log = structlog.get_logger(__name__)

#: переключатель slo.yml → ключ в runtime_overrides. Значение — «что включить» (см. consume'ов)
OVERRIDE_KEYS: dict[str, tuple[str, Any]] = {
    "streaming": ("telegram.stream_replies", False),
    "web_tools": ("tools.web.enabled", False),
    "verify": ("answer.verify.enabled", False),
    "fast_route_only": ("route.force_fast", True),
}

#: TTL переключателя: дольше — «забыли снять», короче — «дёргает каждые пять минут».
#: Два часа = полный цикл до следующей ручной проверки doctor'ом
DEFAULT_TTL_S = 2 * 3600


@dataclass(frozen=True, slots=True)
class Override:
    key: str
    value: Any
    reason: str = ""
    principal_id: int = 0


class Overrides(Protocol):
    async def load(self) -> Mapping[str, Any]: ...

    async def set(self, key: str, value: Any, *, reason: str, ttl_s: int) -> None: ...

    async def clear(self, key: str, *, reason_prefix: str = "") -> int: ...

    async def list_active(self) -> list[dict[str, Any]]: ...


class MemoryOverrides:
    """Однопроцессный фолбэк (демо без БД). Волатильность объявлена, а не обнаружена посреди
    ночи."""

    durable = False

    def __init__(self) -> None:
        self._data: dict[str, Override] = {}

    async def load(self) -> Mapping[str, Any]:
        return {key: item.value for key, item in self._data.items()}

    async def set(self, key: str, value: Any, *, reason: str, ttl_s: int) -> None:
        del ttl_s  # в памяти TTL не нужен: процесс упал — состояние ушло, и это объявлено
        self._data[key] = Override(key, value, reason)

    async def clear(self, key: str, *, reason_prefix: str = "") -> int:
        item = self._data.get(key)
        if item is None or (reason_prefix and not item.reason.startswith(reason_prefix)):
            return 0
        del self._data[key]
        return 1

    async def list_active(self) -> list[dict[str, Any]]:
        return [vars(item) for item in self._data.values()]


class SqlOverrides:
    """``platform.runtime_overrides``: строка с причиной и TTL. Ссылка на SLO — в ``reason``."""

    durable = True

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def load(self) -> Mapping[str, Any]:
        sql = text(
            "SELECT key, value FROM platform.runtime_overrides"
            " WHERE expires_at IS NULL OR expires_at > now()"
        )
        try:
            async with self._session() as s:
                rows = (await s.execute(sql)).mappings().all()
        except Exception as exc:  # noqa: BLE001 — отсутствие таблицы не имеет права валить ход
            log.warning("overrides.load_failed", err=repr(exc)[:160])
            return {}
        out: dict[str, Any] = {}
        for row in rows:
            value = row["value"]
            if isinstance(value, (bytes, bytearray)):
                value = orjson.loads(value)
            out[str(row["key"])] = value
        return out

    async def set(self, key: str, value: Any, *, reason: str, ttl_s: int) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO platform.runtime_overrides"
                    " (key, value, reason, expires_at, set_at)"
                    " VALUES (:key, CAST(:value AS jsonb), :reason,"
                    " now() + make_interval(secs => :ttl), now())"
                    " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,"
                    " reason = EXCLUDED.reason, expires_at = EXCLUDED.expires_at, set_at = now()"
                ).bindparams(
                    key=key,
                    value=orjson.dumps(value).decode(),
                    reason=reason[:500],
                    ttl=int(ttl_s),
                )
            )
            await s.commit()

    async def clear(self, key: str, *, reason_prefix: str = "") -> int:
        sql = "DELETE FROM platform.runtime_overrides WHERE key = :key"
        params: dict[str, Any] = {"key": key}
        if reason_prefix:
            # снимать чужое нельзя: трогать можно только переключатели, выставленные тем же
            # механизмом (slo:*), иначе деградация научилась бы отменять ручные решения
            sql += " AND reason LIKE :prefix"
            params["prefix"] = f"{reason_prefix}%"
        async with self._session() as s:
            result = await s.execute(text(sql), params)
            await s.commit()
            return int(result.rowcount or 0)

    async def list_active(self) -> list[dict[str, Any]]:
        sql = text(
            "SELECT key, value, reason, principal_id, set_at, expires_at"
            " FROM platform.runtime_overrides"
            " WHERE expires_at IS NULL OR expires_at > now() ORDER BY key"
        )
        try:
            async with self._session() as s:
                rows = (await s.execute(sql)).mappings().all()
        except Exception as exc:  # noqa: BLE001
            log.warning("overrides.list_failed", err=repr(exc)[:160])
            return []
        return [dict(row) for row in rows]


class DegradationController:
    """Применяет :class:`SloReport` к переключателям. Держать состояние — не его работа."""

    def __init__(
        self,
        overrides: Overrides,
        *,
        journal: Any = None,  # DecisionRecorder-ish: нужен только system_event
        ttl_s: int = DEFAULT_TTL_S,
        owner_id: int = 0,
    ) -> None:
        self._overrides = overrides
        self._journal = journal
        self._ttl_s = int(ttl_s)
        self._owner_id = int(owner_id)

    async def apply(self, reports: Sequence[SloReport]) -> list[str]:
        """Вернуть список применённых изменений («streaming: off (slo:stream-abort-rate)»).

        Порядок фиксирован: сначала включаем деградацию для проваленных, потом снимаем её с
        восстановившихся. Обратный порядок на коротком окне давал бы «выключили стриминг и
        сразу включили» внутри одного прогона — дребезг, который и запрещён контрактом.
        """
        unknown = {
            switch
            for report in reports
            for switch in report.degrade
            if switch not in SUPPORTED_SWITCHES
        }
        if unknown:
            raise ValueError(f"неизвестные переключатели в отчётах: {sorted(unknown)}")
        changes: list[str] = []
        seen: set[str] = set()
        for report in reports:
            if not report.degrade:
                continue
            for switch in report.degrade:
                key, value = OVERRIDE_KEYS[switch]
                if report.failing:
                    reason = f"slo:{report.name}"
                    await self._overrides.set(key, value, reason=reason, ttl_s=self._ttl_s)
                    note = (
                        f"деградация: {switch} → {value!r} "
                        f"(SLO {report.name} провален: {report.detail})"
                    )
                    changes.append(note)
                    await self._journal_event(note)
                    seen.add(key)
                elif key not in seen:
                    cleared = await self._overrides.clear(key, reason_prefix="slo:")
                    if cleared:
                        note = f"деградация снята: {switch} (SLO {report.name} восстановлен)"
                        changes.append(note)
                        await self._journal_event(note)
        return changes

    async def _journal_event(self, note: str) -> None:
        event = getattr(self._journal, "system_event", None)
        if event is None:
            return
        try:
            await event(owner_id=self._owner_id, note=note)
        except Exception as exc:  # noqa: BLE001 — журнал не имеет права удержать деградацию
            log.warning("degradation.journal_failed", err=repr(exc)[:200])
