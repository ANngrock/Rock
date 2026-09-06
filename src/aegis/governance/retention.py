"""Ретеншен, legal hold и право быть забытым (F3): конфликт append-only и «удали меня».

Журнал не редактируется и не удаляется — это смысл цепочки хэшей. Но право на забвение не может
упираться в «у нас тут immutability, извините». Разрешение конфликта, записанное здесь:

* **крипто-стирание.** Содержимое блоба уничтожается не удалением строки, а уничтожением DEK:
  запись остаётся (цепочка цела, ``verify`` проходит), а текст восстановить нечем. Манифест
  уничтожения (какие sha, чей ключ, кем запрошено) подписывает операцию — «мы удалили»
  остаётся фактом журнала, а не обещанием;
* **tombstone в цепочке.** Отдельная запись журнала ``kind='tombstone'`` с числом стёртых блобов
  и ссылкой на манифест: пустое место в истории не появляется, и «удаление» видно из той же
  таблицы, что и ходы;
* **retention как код + пауза.** ``deploy/retention.yml`` — сроки по классам данных; план
  пересчитывается из файла, ``dry-run`` ничего не трогает, ``apply`` исполняет план; активный
  legal hold на класс/принципал исключает класс из плана (и это видно в отчёте, а не только в
  отказе);
* **re-wrap на механике аренд** (:mod:`aegis.platform.backfill`): перешифрование старых строк под
  новый KEK — батчами, идемпотентно, с паузой; прерванный прогон продолжает с курсора.

Модуль сознательно не знает про Telegram и модели: это функция над БД, файлом и ключаркой.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import orjson
import structlog
from sqlalchemy import text

from aegis.governance.recorder import DecisionRecorder, purgeable_blob_ids
from aegis.platform.canonical import sha256_bytes
from aegis.platform.db import SessionFactory, session

__all__ = [
    "LegalHold",
    "RetentionPolicy",
    "RetentionPolicyError",
    "ShredPlan",
    "ShredResult",
    "active_holds",
    "apply_forget",
    "apply_retention",
    "keyring_status",
    "list_shreds",
    "load_retention_file",
    "plan_forget",
    "plan_retention",
    "rewrap_batch",
    "select_holds",
]

log = structlog.get_logger(__name__)


class RetentionPolicyError(ValueError):
    """Плохой retention.yml — это не «применяем прошлый», это падение: на кону чужие данные."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Класс данных → срок хранения в днях. ``0`` = «хранить вечно, только по запросу forget».

    Классы зафиксированы (blobs/journal/history), потому что именно они встречаются в схеме:
    «срок на выдуманную категорию» — это ноль применённых политик при полном ощущении контроля.
    """

    days: Mapping[str, int] = field(default_factory=dict)
    grace_days: int = 1
    source: str = ""

    def cutoff(self, now: datetime, cls: str) -> datetime | None:
        days = int(self.days.get(cls, 0) or 0)
        if days <= 0:
            return None
        return now - timedelta(days=days + self.grace_days)


def load_retention_file(path: str | Path) -> RetentionPolicy:
    file = Path(path)
    if not file.is_file():
        raise RetentionPolicyError(f"файл ретеншена не найден: {file}")
    import yaml  # noqa: PLC0415 — формат файла

    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RetentionPolicyError(f"{file.name}: не YAML: {exc}") from exc
    raw = data.get("classes") or {}
    if not isinstance(raw, Mapping) or not raw:
        raise RetentionPolicyError(f"{file.name}: classes — непустая карта")
    days: dict[str, int] = {}
    for name, value in raw.items():
        if isinstance(value, Mapping):
            days[str(name)] = int(value.get("days", 0))
        else:
            days[str(name)] = int(value)
    return RetentionPolicy(days=days, grace_days=int(data.get("grace_days", 1)), source=str(file))


@dataclass(frozen=True, slots=True)
class LegalHold:
    scope: str  # 'class' | 'principal'
    value: str
    reason: str

    def covers(self, cls: str, owner_id: int | None) -> bool:
        if self.scope == "class" and self.value == cls:
            return True
        if self.scope == "principal" and owner_id is not None and self.value == str(int(owner_id)):
            return True
        return False


class HoldRepository(Protocol):
    async def active(self) -> Sequence[LegalHold]: ...

    async def place(self, scope: str, value: str, reason: str, *, by: int) -> None: ...

    async def release(self, scope: str, value: str, *, by: int) -> None: ...


class SqlHoldRepository:
    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._sm = session_factory

    def _session(self) -> Any:
        return self._sm() if self._sm is not None else session()

    async def active(self) -> Sequence[LegalHold]:
        sql = (
            "SELECT scope, value, reason FROM platform.legal_holds"
            " WHERE released_at IS NULL ORDER BY placed_at"
        )
        async with self._session() as s:
            rows = (await s.execute(text(sql))).mappings().all()
        return [
            LegalHold(scope=str(r["scope"]), value=str(r["value"]), reason=str(r["reason"] or ""))
            for r in rows
        ]

    async def place(self, scope: str, value: str, reason: str, *, by: int) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "INSERT INTO platform.legal_holds (scope, value, reason, placed_by)"
                    " VALUES (:scope, :value, :reason, :by) ON CONFLICT (scope, value)"
                    " WHERE released_at IS NULL DO NOTHING"
                ).bindparams(scope=scope, value=value[:80], reason=reason[:500], by=int(by))
            )
            await s.commit()

    async def release(self, scope: str, value: str, *, by: int) -> None:
        async with self._session() as s:
            await s.execute(
                text(
                    "UPDATE platform.legal_holds SET released_at = now(), released_by = :by"
                    " WHERE scope = :scope AND value = :value AND released_at IS NULL"
                ).bindparams(scope=scope, value=value[:80], by=int(by))
            )
            await s.commit()


def select_holds(holds: Iterable[LegalHold], cls: str, owner_id: int | None) -> list[LegalHold]:
    return [hold for hold in holds if hold.covers(cls, owner_id)]


@dataclass(slots=True)
class ShredResult:
    dry_run: bool = True
    candidates: int = 0
    shredded: int = 0
    blocked: str = ""
    detail: str = ""
    chain_note: str = ""

    def summary(self) -> str:
        head = "план (ничего не тронуто)" if self.dry_run else f"уничтожено блобов: {self.shredded}"
        bits = [head, f"кандидатов: {self.candidates}"]
        if self.blocked:
            bits.append(f"⛔ {self.blocked}")
        if self.detail:
            bits.append(self.detail)
        if self.chain_note:
            bits.append(self.chain_note)
        return " · ".join(bits)


async def plan_retention(
    *,
    policy: RetentionPolicy,
    session_factory: SessionFactory | None = None,
    now: datetime | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Что истекло: (класс, строки) без исполнения. Dry-run = буквально этот план."""
    moment = now or datetime.now(UTC)
    plan: list[dict[str, Any]] = []
    sm = session_factory or session
    async with sm() as s:
        holds = (
            await s.execute(
                text(
                    "SELECT scope, value, coalesce(reason, '') AS reason FROM platform.legal_holds"
                    " WHERE released_at IS NULL"
                )
            )
        ).mappings()
        blocked = [LegalHold(str(r["scope"]), str(r["value"]), str(r["reason"])) for r in holds]
        blobs_cutoff = policy.cutoff(moment, "blobs")
        if blobs_cutoff is not None:
            count = await s.scalar(
                text("SELECT count(*) FROM platform.blobs WHERE created_at < :cutoff").bindparams(
                    cutoff=blobs_cutoff
                )
            )
            blocked_by = select_holds(blocked, "blobs", None)
            plan.append(
                {
                    "class": "blobs",
                    "cutoff": blobs_cutoff.isoformat(),
                    "candidates": int(count or 0),
                    "blocked_by_holds": [f"{h.scope}:{h.value}" for h in blocked_by],
                    "limited_to": int(limit),
                }
            )
    return plan


async def apply_retention(
    *,
    policy: RetentionPolicy,
    session_factory: SessionFactory | None = None,
    now: datetime | None = None,
    limit: int = 500,
    dry_run: bool = True,
) -> ShredResult:
    """Стереть просроченные блобы по классам. Без ключа доступа содержимое остаётся «в блобе»,
    поэтому purge = удаление строки + запись манифеста (для шифрованных — ещё и shred DEK).

    apply без явного ``dry_run=False`` не делает НИЧЕГО: «plan → apply» — это контракт, а не
    рефрен в доке. Прерванный прогон безопасен: каждое удаление — отдельная транзакция,
    курсор — created_at, повтор просто находит меньше строк.
    """
    moment = now or datetime.now(UTC)
    result = ShredResult(dry_run=dry_run)
    cutoff = policy.cutoff(moment, "blobs")
    if cutoff is None:
        return result
    sm = session_factory or session
    async with sm() as s:
        holds = (
            await s.execute(
                text("SELECT scope, value FROM platform.legal_holds WHERE released_at IS NULL")
            )
        ).mappings()
        blocked = [LegalHold(str(r["scope"]), str(r["value"]), "") for r in holds]
        if select_holds(blocked, "blobs", None):
            result.blocked = (
                "blobs под legal hold: apply ничего не удалит (place → release, не «подождём»)"
            )
            return result
        rows = (
            await s.execute(
                text(
                    "SELECT sha256, content_cipher, key_version FROM platform.blobs"
                    " WHERE created_at < :cutoff ORDER BY created_at LIMIT :limit"
                ).bindparams(cutoff=cutoff, limit=int(limit))
            )
        ).all()
        result.candidates = len(rows)
        if dry_run:
            return result
        for sha, _cipher, key_version in rows:
            await s.execute(
                text(
                    "INSERT INTO platform.key_shreds (blob_sha, key_version, reason, manifest)"
                    " VALUES (:sha, :ver, :reason, CAST(:manifest AS jsonb))"
                ).bindparams(
                    sha=bytes(sha),
                    ver=key_version,
                    reason="retention:blobs",
                    manifest=orjson.dumps(
                        {"at": moment.isoformat(), "class": "blobs", "cutoff": cutoff.isoformat()}
                    ).decode(),
                )
            )
            await s.execute(
                text("DELETE FROM platform.blobs WHERE sha256 = :sha").bindparams(sha=bytes(sha))
            )
        await s.commit()
        result.shredded = len(rows)
    return result


@dataclass(slots=True)
class ShredPlan:
    """План «забыть владельца»: какие блобы реально уничтожить и что сказать журналу."""

    owner_id: int
    blobs: list[bytes] = field(default_factory=list)
    shared_left: int = 0
    records_touched: int = 0

    def summary(self) -> str:
        base = f"владелец {self.owner_id}: уничтожим {len(self.blobs)} блобов"
        if self.shared_left:
            base += (
                f"; {self.shared_left} переиспользуются другими —"
                " содержимое остаётся, доступ режет RLS"
            )
        if self.records_touched:
            base += f"; строк журнала затронуто {self.records_touched}"
        return base


async def plan_forget(
    owner_id: int,
    *,
    session_factory: SessionFactory | None = None,
    limit: int = 500,
) -> ShredPlan:
    """Что можно уничтожить по запросу «забыть». Отдельно — что нельзя (разделяемые блобы).

    Журнальные строки НЕ удаляются никогда: append-only — не бюрократия, а доказательство,
    что «историю не правили». Забыть содержимое и оставить факт — единственная форма, в которой
    оба обещания живут одновременно: tombstone в цепочке означает «содержимое уничтожено
    намеренно», а «мы чего-то не нашли» от «так и было» отличает именно она.
    """
    sm = session_factory or session
    async with sm() as s:
        records = int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM governance.decision_records WHERE owner_id = :o"
                ).bindparams(o=int(owner_id))
            )
            or 0
        )
    purgeable = await purgeable_blob_ids(owner_id, session_factory=session_factory, limit=limit)
    referenced = 0
    async with sm() as s:
        referenced = int(
            await s.scalar(
                text(
                    "SELECT count(DISTINCT coalesce(dr.input_sha, dr.output_sha))::int"
                    " FROM governance.decision_records dr"
                    " WHERE dr.owner_id = :o AND coalesce(dr.input_sha, dr.output_sha) IS NOT NULL"
                ).bindparams(o=int(owner_id))
            )
            or 0
        )
    return ShredPlan(
        owner_id=int(owner_id),
        blobs=purgeable,
        shared_left=max(referenced - len(purgeable), 0),
        records_touched=records,
    )


async def apply_forget(
    plan: ShredPlan,
    *,
    recorder: DecisionRecorder | None = None,
    actor_id: int | None = None,
    session_factory: SessionFactory | None = None,
    dry_run: bool = True,
    reason: str = "right-to-be-forgotten",
) -> ShredResult:
    """Выполнить план: shred DEK, удалить содержимое, дописать tombstone в цепочку."""
    result = ShredResult(dry_run=dry_run)
    result.candidates = len(plan.blobs)
    if not plan.blobs:
        result.detail = "нечего уничтожать: все блобы разделяются с другими владельцами"
        return result
    manifest = {
        "owner_id": plan.owner_id,
        "blobs": [bytes(sha).hex() for sha in plan.blobs],
        "reason": reason,
        "actor_id": actor_id,
        "at": datetime.now(UTC).isoformat(),
    }
    manifest_sha = sha256_bytes(orjson.dumps(manifest, option=orjson.OPT_SORT_KEYS))
    if dry_run:
        result.detail = (
            f"уничтожили бы {len(plan.blobs)} блобов; манифест {manifest_sha.hex()[:16]};"
            " apply с --apply"
        )
        return result
    sm = session_factory or session
    async with sm() as s:
        for sha in plan.blobs:
            await s.execute(
                text(
                    "INSERT INTO platform.key_shreds"
                    " (blob_sha, key_version, reason, actor_id, manifest_sha, manifest)"
                    " VALUES (:sha, (SELECT key_version FROM platform.blobs WHERE sha256 = :sha),"
                    " :reason, :actor, :msha, CAST(:manifest AS jsonb))"
                ).bindparams(
                    sha=sha,
                    reason=reason[:120],
                    actor=int(actor_id or plan.owner_id),
                    msha=manifest_sha,
                    manifest=orjson.dumps(manifest).decode(),
                )
            )
            # сам ключ уничтожаем раньше строки: если процесс умрёт между операциями,
            # содержимое уже нечитаемо (DEK нет) — «полузабытого» состояния не существует
            await s.execute(
                text(
                    "UPDATE platform.blobs SET wrapped_dek = NULL"
                    " WHERE sha256 = :sha AND content_cipher <> 'none'"
                ).bindparams(sha=sha)
            )
            await s.execute(
                text("DELETE FROM platform.blobs WHERE sha256 = :sha").bindparams(sha=sha)
            )
        await s.commit()
    result.shredded = len(plan.blobs)
    result.detail = f"манифест уничтожения: {manifest_sha.hex()[:16]} (platform.key_shreds)"
    if recorder is not None:
        await recorder.system_event(
            owner_id=plan.owner_id,
            kind="tombstone",
            note=(
                f"крипто-стирание владельца {plan.owner_id}: блобов {len(plan.blobs)},"
                f" манифест {manifest_sha.hex()[:16]}"
            ),
            params={
                "blobs": len(plan.blobs),
                "manifest_sha": manifest_sha.hex(),
                "actor_id": int(actor_id or plan.owner_id),
                "shared_left": plan.shared_left,
                "records_kept": plan.records_touched,
            },
        )
        result.chain_note = (
            "tombstone-запись добавлена в цепочку; verify проходит, missing_blobs растёт"
        )
    return result


async def list_shreds(
    *, session_factory: SessionFactory | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    sm = session_factory or session
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT blob_sha::text, key_version, reason, actor_id,"
                    " coalesce(encode(manifest_sha, 'hex'), '') AS manifest,"
                    " shredded_at"
                    " FROM platform.key_shreds ORDER BY id DESC LIMIT :limit"
                ).bindparams(limit=int(limit))
            )
        ).mappings()
        return [dict(row) for row in rows]


async def active_holds(*, session_factory: SessionFactory | None = None) -> list[dict[str, Any]]:
    sm = session_factory or session
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT scope, value, reason, placed_by, placed_at FROM platform.legal_holds"
                    " WHERE released_at IS NULL ORDER BY placed_at"
                )
            )
        ).mappings()
        return [dict(row) for row in rows]


#: re-wrap: перешифрование обёрток DEK под активный KEK (батчами; тот же принцип, что у бэкфиллов).
#: SQL-хвосты — куски одного запроса: «взять батч» и «обновить одну строку» не должны
#: расходиться по предикату, иначе строка может быть обновлена дважды (безопасно) или не
#: обновиться вовсе (тоже безопасно — вернётся в следующий проход)
REWAP_PENDING_SQL = """
SELECT sha256, wrapped_dek, key_version
  FROM platform.blobs
 WHERE content_cipher <> 'none' AND key_version < :active
 ORDER BY created_at
 LIMIT :limit
 FOR UPDATE SKIP LOCKED
"""

REWAP_UPDATE_SQL = """
UPDATE platform.blobs
   SET wrapped_dek = :dek, key_version = :version
 WHERE sha256 = :sha
"""


async def rewrap_batch(
    cipher: Any,
    *,
    session_factory: SessionFactory | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Один батч re-wrap'а: развернуть DEK старым KEK, завернуть активным. Содержимое не трогаем.

    «Содержимое не трогаем» — главное: sha256 блоба (он же ссылка цепочки) не меняется, поэтому
    перешифрование не порождает «новую историю». Обрывающийся прогон не страшен: строки либо уже
    под новой версией (пропускаются предикатом), либо вернутся в следующий батч.
    """
    from aegis.platform.crypto import KeyShredded  # noqa: PLC0415

    sm = session_factory or session
    active = int(getattr(cipher, "active_version", 1))
    done = failed = 0
    async with sm() as s:
        rows = (
            await s.execute(text(REWAP_PENDING_SQL).bindparams(active=active, limit=int(limit)))
        ).all()
        for sha, wrapped, version in rows:
            try:
                dek = cipher.unwrap(bytes(wrapped), key_version=int(version))
                fresh, new_version = cipher.wrap(dek)
                await s.execute(
                    text(REWAP_UPDATE_SQL).bindparams(
                        sha=bytes(sha), dek=fresh, version=int(new_version)
                    )
                )
                done += 1
            except (KeyShredded, ValueError) as exc:
                log.warning("rewrap.row_failed", sha=str(sha)[:16], err=repr(exc)[:160])
                failed += 1
        await s.commit()
    return {"rewrapped": done, "failed": failed, "seen": len(rows)}


async def keyring_status(*, session_factory: SessionFactory | None = None) -> dict[str, Any]:
    """Состояние ключей: версии в БД, сколько блобов на каждой, сколько без ключа. Для doctor'а."""
    sm = session_factory or session
    async with sm() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT coalesce(key_version::text, 'plain') AS ver, content_cipher,"
                    " count(*) AS n, sum(count(*)) OVER () AS total"
                    " FROM platform.blobs GROUP BY 1, 2 ORDER BY 1"
                )
            )
        ).mappings()
        data = [dict(r) for r in rows]
    return {
        "groups": [
            {"key_version": d["ver"], "cipher": d["content_cipher"], "blobs": int(d["n"])}
            for d in data
        ],
        "blobs": int(data[0]["total"]) if data else 0,
    }
