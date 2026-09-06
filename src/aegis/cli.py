"""CLI-точка входа: ``aegis bot|doctor|ask|tools|repro``.

``repro`` — журнал решений (M1): ``verify`` сверяет хэш-цепочку, ``stats`` показывает объём,
``anchor`` ставит мерклов корень дня (его крутят по cron), ``replay`` повторяет ход. Те же данные,
что бот отдаёт в ``/replay``: и там, и здесь читается одна таблица.

Зачем нужен ``ask``: путь «сообщение → supervisor → ответ» должен быть проверяемым без Telegram
и без токена — это и инструмент отладки, и то, чем прогоняют смоук-тест в CI.

``doctor`` печатает состояние графа зависимостей (конфиг, БД, Redis, SearXNG, модели) и годится
как HEALTHCHECK контейнера: ``aegis doctor --json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aegis.platform.config import Settings

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aegis", description="Aegis — личный AI-менеджер (шаг 1)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bot", help="запустить Telegram-бота (polling)")

    doctor = sub.add_parser("doctor", help="проверить конфигурацию и связности")
    doctor.add_argument("--json", action="store_true", help="машинный вывод (для HEALTHCHECK)")
    doctor.add_argument("--quick", action="store_true", help="не ходить в сеть (только конфиг)")
    doctor.add_argument(
        "--models",
        action="store_true",
        help="живой прогон по всем ролям (brain/fast/vision/embed), ~по токену на роль",
    )

    ask = sub.add_parser("ask", help="одиночный запрос к supervisor без Telegram")
    ask.add_argument("text", nargs="+", help="текст сообщения")
    ask.add_argument("--owner-id", type=int, default=1)

    sub.add_parser("tools", help="список зарегистрированных инструментов")

    repro = sub.add_parser(
        "repro", help="журнал решений: сверка цепочки, объём, якорь дня, воспроизведение хода"
    )
    actions = repro.add_subparsers(dest="repro_action", required=True)
    verify = actions.add_parser("verify", help="пересчитать хэш-цепочку журнала")
    verify.add_argument("--days", type=int, default=7, help="за сколько дней (0 = всё)")
    verify.add_argument("--json", action="store_true", help="машинный вывод")
    actions.add_parser("stats", help="сколько записей, блобов, якорей")
    anchor = actions.add_parser("anchor", help="мерклов корень записей дня в governance.anchors")
    anchor.add_argument("--day", default=None, help="YYYY-MM-DD, по умолчанию сегодня")
    replay = actions.add_parser("replay", help="повторить ход с тем же входом и сравнить ответ")
    replay.add_argument("trace", nargs="?", default="", help="trace_id или его начало (8 символов)")
    replay.add_argument("--last", action="store_true", help="взять последний ход в журнале")
    replay.add_argument("--owner-id", type=int, default=1)

    remind = sub.add_parser(
        "remind", help="напоминания: поставить, показать, отменить, прогнать расписание"
    )
    remind_actions = remind.add_subparsers(dest="remind_action", required=True)
    add = remind_actions.add_parser("add", help="поставить напоминание без участия модели")
    add.add_argument("text", nargs="+", help="что напомнить")
    add.add_argument("--when", required=True, help="словами: «через 20 минут», «завтра в 9»")
    add.add_argument("--at", default=None, help="точное время ISO с поясом (обход разборщика)")
    add.add_argument("--owner-id", type=int, default=1)
    show = remind_actions.add_parser("list", help="запланированные напоминания")
    show.add_argument("--limit", type=int, default=10)
    show.add_argument("--owner-id", type=int, default=1)
    rm = remind_actions.add_parser("cancel", help="отменить по началу id или по слову из текста")
    rm.add_argument("ref")
    rm.add_argument("--owner-id", type=int, default=1)
    tick = remind_actions.add_parser(
        "tick", help="прогнать расписание: отправить всё, что пора (для systemd-таймера)"
    )
    tick.add_argument("--limit", type=int, default=None, help="сколько строк за проход")
    tick.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что ушло бы, — не отправляя и не трогая БД",
    )
    index = sub.add_parser("index", help="эмбеддинги заметок: очередь и проход индексации")
    index_actions = index.add_subparsers(dest="index_action", required=True)
    notes = index_actions.add_parser(
        "notes", help="проиндексировать заметки, у которых ещё нет эмбеддинга"
    )
    notes.add_argument("--limit", type=int, default=None, help="сколько заметок за проход")
    notes.add_argument(
        "--batch", type=int, default=None, help="текстов в одном запросе к /embeddings"
    )
    notes.add_argument(
        "--dry-run",
        action="store_true",
        help="только сказать, сколько ждёт индексации — не трогая БД и модель",
    )
    outbox = sub.add_parser("outbox", help="очередь событий и её публикация в NATS (relay)")
    outbox_actions = outbox.add_subparsers(dest="outbox_action", required=True)
    outbox_actions.add_parser(
        "stats", help="сколько событий ждёт публикации и сколько с исчерпанными попытками"
    )
    ob_tick = outbox_actions.add_parser(
        "tick", help="опубликовать непубликованное (для systemd-таймера)"
    )
    ob_tick.add_argument("--limit", type=int, default=None, help="сколько событий за проход")
    ob_tick.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что ушло бы, — не публикуя и не трогая счётчики",
    )
    export = sub.add_parser("export", help="выгрузить журнал решений во внешнюю витрину")
    export_actions = export.add_subparsers(dest="export_action", required=True)
    lf = export_actions.add_parser("langfuse", help="отправить окно журнала в Langfuse (OTLP/HTTP)")
    lf.add_argument("--limit", type=int, default=None, help="сколько записей журнала за прогон")
    lf.add_argument(
        "--since", default=None, help="ISO-дата начала окна (по умолчанию: LANGFUSE_WINDOW_HOURS)"
    )
    lf.add_argument("--trace", default=None, help="выгрузить один ход по trace_id")
    lf.add_argument(
        "--dry-run",
        action="store_true",
        help="собрать и посчитать, но ничего не отправлять; ключи при этом не нужны",
    )
    # --- права, флаги, политика, хранение, события, схема (шаги 2.5+) ---
    pr = sub.add_parser("principals", help="RBAC: кто есть, что можно, kill-switch, бюджет (F2)")
    pr_actions = pr.add_subparsers(dest="principals_action", required=True)
    pr_actions.add_parser("list", help="принципалы с грантами, лимитами и паузой")
    pk = pr_actions.add_parser("kind", help="сменить роль (owner/member/guest/service)")
    pk.add_argument("id", type=int)
    pk.add_argument("kind", choices=("owner", "member", "guest", "service"))
    pg = pr_actions.add_parser("grant", help="выдать действие (например tool:pay)")
    pg.add_argument("id", type=int)
    pg.add_argument("action")
    pv = pr_actions.add_parser("revoke", help="забрать действие")
    pv.add_argument("id", type=int)
    pv.add_argument("action")
    px = pr_actions.add_parser("kill", help="персональный kill-switch: ответы на паузе")
    px.add_argument("id", type=int)
    px.add_argument("--off", action="store_true", help="снять паузу")
    px.add_argument("--reason", default="вручную через CLI")
    pb = pr_actions.add_parser("budget", help="дневной бюджет принципала в USD")
    pb.add_argument("id", type=int)
    pb.add_argument("--usd", type=float, default=None)
    pb.add_argument("--clear", action="store_true", help="снять личный лимит (общий остаётся)")

    fl = sub.add_parser("flags", help="feature flags: состояние, выдача, протухшие (F6)")
    fl_actions = fl.add_subparsers(dest="flags_action", required=True)
    fl_list = fl_actions.add_parser("list", help="каталог × БД: процент, стадии, покрытие golden")
    fl_list.add_argument("--actor", type=int, default=None, help="оценить для конкретного id")
    fset = fl_actions.add_parser("set", help="создать/обновить определение флага")
    fset.add_argument("key")
    fset.add_argument("--percent", type=int, default=None)
    fset.add_argument("--allow", default="", help="список id через запятую")
    fset.add_argument("--deny", default="", help="список id через запятую")
    fset.add_argument("--stage", default="canary", choices=("shadow", "canary", "full", "retired"))
    fset.add_argument("--reason", default="")
    fl_actions.add_parser(
        "stale", help="флаги на 100% старше N дней — кандидаты на удаление (CI-гигиена)"
    )

    po = sub.add_parser("policy", help="policy-as-code: lint файла правил и shadow-прогон (F5)")
    po_actions = po.add_subparsers(dest="policy_action", required=True)
    po_actions.add_parser("lint", help="разобрать rules-файл, сверить с lock; смягчения — с golden")
    psh = po_actions.add_parser(
        "shadow", help="пересобрать последние N решений и показать дельту файла против кода"
    )
    psh.add_argument("--limit", type=int, default=200)

    rt = sub.add_parser("retention", help="retention-as-code: plan/apply/forget/holds/rewrap (F3)")
    rt_actions = rt.add_subparsers(dest="retention_action", required=True)
    rt_actions.add_parser("plan", help="что удалится сейчас — только чтение (legal hold учтён)")
    rta = rt_actions.add_parser("apply", help="прогон ротации; по умолчанию dry-run")
    rta.add_argument("--execute", action="store_true", help="собственно удаление (после plan)")
    rta.add_argument("--limit", type=int, default=500)
    rt_actions.add_parser("holds", help="активные legal holds")
    rth = rt_actions.add_parser("hold", help="поставить/снять hold: scope=value")
    rth.add_argument(
        "spec",
        help="scope=value: principal=777 или class=journal (других scope схема не знает)",
    )
    rth.add_argument("--reason", default="")
    rth.add_argument("--release", action="store_true", help="снять вместо установки")
    rtf = rt_actions.add_parser("forget", help="crypto-shredding по запросу «забудь меня»")
    rtf.add_argument("owner_id", type=int)
    rtf.add_argument("--execute", action="store_true", help="иначе — план без разрушения")
    rt_actions.add_parser("rewrap", help="одна пачка re-wrap DEK на активный KEK (lease-батч)")
    rt_actions.add_parser("shreds", help="журнал уничтожений (манифесты)")
    rt_actions.add_parser("keyring", help="версии ключей и сколько блобов на каждой")

    ev = sub.add_parser("events", help="шина событий: DLQ и переигрывание (F4)")
    ev_actions = ev.add_subparsers(dest="events_action", required=True)
    ev_actions.add_parser("dlq", help="сколько отбраковано, последние причины")
    evr = ev_actions.add_parser(
        "replay", help="вернуть в очередь события с seq >= N (идемпотентно по event_id)"
    )
    evr.add_argument("from_seq", type=int)
    evr.add_argument("--to-seq", type=int, default=None)
    evr.add_argument("--type", dest="etype", default=None, help="фильтр по event_type")
    evr.add_argument("--execute", action="store_true", help="иначе — только посчитать")

    mi = sub.add_parser("migrate", help="схема: версия, незакрытые бэкфиллы, подозрения (F10)")
    mi_actions = mi.add_subparsers(dest="migrate_action", required=True)
    mi_actions.add_parser(
        "status", help="alembic head против файлов, pending-backfill, dangling NOT NULL"
    )

    bf = sub.add_parser("backfill", help="пакетные доводчики данных: бег с паузой (F10)")
    bf_actions = bf.add_subparsers(dest="backfill_action", required=True)
    bfr = bf_actions.add_parser("run", help="брать задания по lease, батчами; Ctrl-C = пауза")
    bfr.add_argument("--name", default=None, help="только одно задание")
    bfr.add_argument("--rounds", type=int, default=4, help="сколько батчей за запуск")
    bfr.add_argument("--lease-secs", type=int, default=900)
    bf_actions.add_parser("status", help="прогресс всех заданий")
    bfp = bf_actions.add_parser(
        "pause", help="поставить задание на паузу (следующий раунд не возьмёт)"
    )
    bfp.add_argument("name")

    sl = sub.add_parser("slo", help="SLO: оценка окон, алерты, тик деградации (F7)")
    sl_actions = sl.add_subparsers(dest="slo_action", required=True)
    sl_actions.add_parser("status", help="окна и burn-rate: что сейчас горит")
    sla = sl_actions.add_parser("alerts", help="сгенерировать алерт-правила из slo.yml")
    sla.add_argument("--write", default=None, help="файл для записи (deploy/slo.alerts.yml)")
    slt = sl_actions.add_parser(
        "tick", help="применить декларируемую деградацию к runtime_overrides (для таймера)"
    )
    slt.add_argument("--dry-run", action="store_true", help="показать, что включилось бы")

    tn = sub.add_parser(
        "turns", help="ходы (F1): кто держит аренду, залипшие claimed, ручная расчистка"
    )
    tn_actions = tn.add_subparsers(dest="turns_action", required=True)
    tn_actions.add_parser("status", help="активные заявки и осиротевшие элементы очереди")
    tnr = tn_actions.add_parser("release", help="закрыть ход без токена — путь CLI-расчистки")
    tnr.add_argument("trace", help="trace_id (uuid) активного хода")
    tnd = tn_actions.add_parser(
        "drain", help="вернуть осиротевшие claimed-элементы очереди владельцу"
    )
    tnd.add_argument("owner_id", type=int, help="владелец, чью очередь проверяем")
    tnd.add_argument(
        "--execute", action="store_true", help="применить (по умолчанию — только план)"
    )
    return parser


def _write_text_file(path: str, body: str) -> str:
    from pathlib import Path as _Path

    target = _Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return str(target)


async def _scalar(sql: str, **params: Any) -> Any:
    """Один запрос — одна сессия. Иначе упавший запрос отравляет остаток проверки.

    `params` — именованные bind'ы: порог («сколько попыток уже исчерпано») приезжает параметром, а
    не вклеивается в SQL. Иначе каждый такой текст приходится держать в голове как «точно число?».
    """
    from sqlalchemy import text

    from aegis.platform.db import session

    async with session() as s:
        return await s.scalar(text(sql), params or None)


async def _turns_report() -> dict[str, Any]:
    """Аренды ходов (F1): «зависший ход» и «сообщение, застрявшее в claimed».

    ok=False — ровно у мёртвых claimed (attempts исчерпан: их никто не вернёт без руки
    оператора, и сообщение не дойдёт никогда). Просроченная аренда заявки — не беда: begin
    чистит их сам (_EXPIRE_CLAIM), владельцу об этом сказать — note. Таблица отсутствует —
    миграция 0005 не выкатана, и это тоже note: бот без очереди ходов живёт по-старому.
    """
    out: dict[str, Any] = {"ok": True}
    try:
        ready = await _scalar("SELECT to_regclass('governance.turn_claims') IS NOT NULL")
    except Exception as exc:  # noqa: BLE001 - настоящая ошибка БД уже в проверке postgres
        out["note"] = f"не проверялось ({type(exc).__name__})"
        out["hint"] = "детали — в проверке postgres"
        return out
    if not ready:
        out["state"] = "нет таблиц ходов"
        out["hint"] = (
            "docker compose ... alembic upgrade head (миграция 0005)"
            " — или ignore, если актив-актив не нужен"
        )
        return out
    expired = int(
        await _scalar(
            "SELECT count(*)::int FROM governance.turn_claims"
            " WHERE status = 'active' AND lease_until < now()"
        )
        or 0
    )
    dead = int(
        await _scalar(
            "SELECT count(*)::int FROM governance.turn_queue"
            " WHERE status = 'claimed' AND attempts >= 3"
            "   AND claimed_at < now() - interval '60 seconds'"
        )
        or 0
    )
    recovering = int(
        await _scalar(
            "SELECT count(*)::int FROM governance.turn_queue"
            " WHERE status = 'claimed' AND attempts < 3"
            "   AND claimed_at < now() - interval '60 seconds'"
        )
        or 0
    )
    out["active_expired"] = expired
    out["queue_dead"] = dead
    if dead:
        out["ok"] = False
        out["hint"] = f"aegis turns drain <owner> --execute вернёт {dead} в очередь"
    elif recovering:
        out["note"] = f"{recovering} claimed переберутся сами через 60s"
    elif expired:
        out["note"] = f"{expired} просроченных аренд — освободятся на первом begin"
    else:
        out["note"] = "ни зависших ходов, ни осиротевших элементов"
    return out


async def _reminders_report(cfg: Any) -> dict[str, Any]:
    """Расписание напоминаний: таблица, настроен ли инструмент, догоняет ли тик.

    Проба всегда `ok=True` — и это не мягкость. Здоровье контейнера означает «бот может отвечать»;
    невыкатанная миграция 0004 или выключенные напоминания не делают бота больным, а HEALTHCHECK,
    который орёт из-за неиспользуемой функции, владелец через неделю начнёт игнорировать. Всё, что
    здесь не так, — `note`/`hint`, которые doctor печатает строкой.
    """
    out: dict[str, Any] = {"ok": True}
    try:
        ready = await _scalar("SELECT to_regclass('planning.reminders') IS NOT NULL")
    except Exception as exc:  # noqa: BLE001 - настоящая ошибка БД уже в проверке postgres
        out["note"] = f"не проверялось ({type(exc).__name__})"
        out["hint"] = "детали — в проверке postgres"
        return out
    if not ready:
        out["state"] = "нет таблицы"
        out["hint"] = (
            "docker compose -f deploy/docker-compose.yml run --rm bot alembic upgrade head"
            " (миграция 0004)"
        )
        return out
    if not cfg.reminders_enabled:
        out["state"] = "выключено"
        out["note"] = "REMINDERS_ENABLED=false: инструмент отказывает явно"
        return out
    live = await _scalar(
        "SELECT count(*) FROM planning.reminders WHERE status IN ('scheduled', 'sending')"
    )
    overdue = await _scalar(
        "SELECT count(*) FROM planning.reminders WHERE status = 'scheduled' AND due_at <= now()"
    )
    out["live"] = int(live or 0)
    out["overdue"] = int(overdue or 0)
    out["note"] = (
        f"в расписании {out['live']}, просрочено {out['overdue']}"
        f" · тик ≤ {cfg.reminders_batch} за проход"
    )
    if out["overdue"]:
        out["hint"] = "ждут тика: systemctl status aegis-reminders.timer"
    return out


async def _notes_index_report(cfg: Any) -> dict[str, Any]:
    """Очередь эмбеддингов: сколько заметок ждут индексации.

    Проба всегда `ok=True` — по той же причине, что и напоминания: заметки без эмбеддинга не делают
    бота больным, поиск просто деградирует до text-match. Здесь важно не «жив ли», а «успевает ли
    индексация за потоком сохранённых страниц» — поэтому это `note`/`hint`, а не `ok=false`.
    """
    out: dict[str, Any] = {"ok": True}
    try:
        ready = await _scalar("SELECT to_regclass('knowledge.notes') IS NOT NULL")
    except Exception as exc:  # noqa: BLE001 - настоящая ошибка БД уже в проверке postgres
        out["note"] = f"не проверялось ({type(exc).__name__})"
        out["hint"] = "детали — в проверке postgres"
        return out
    if not ready:
        out["state"] = "нет таблицы"
        out["hint"] = (
            "docker compose -f deploy/docker-compose.yml run --rm bot alembic upgrade head"
            " (миграция 0001)"
        )
        return out
    pending = int(
        await _scalar("SELECT count(*) FROM knowledge.notes WHERE embedding IS NULL") or 0
    )
    total = int(await _scalar("SELECT count(*) FROM knowledge.notes") or 0)
    out["pending"] = pending
    out["total"] = total
    try:
        from aegis.platform.gateway.models import resolve_spec

        out["model"] = resolve_spec("embed", cfg).name
    except Exception:  # noqa: BLE001 - имя модели здесь справка, а не предмет проверки
        out["model"] = cfg.model_embed or "по роли embed"
    if not pending:
        out["note"] = f"проиндексировано всё ({total} замет.)"
        return out
    out["note"] = f"{pending} из {total} ждут эмбеддинга · проход ≤ {cfg.embed_index_limit}"
    out["hint"] = (
        "поиску это не мешает (он уходит в text-match); индексация: aegis index notes "
        "или aegis-index.timer"
    )
    return out


async def _langfuse_report(cfg: Any) -> dict[str, Any]:
    """Langfuse: настроено ли, и что бы уехало из журнала за окно. Справочная проба."""
    out: dict[str, Any] = {"ok": True, "enabled": bool(cfg.langfuse_enabled)}
    if not cfg.langfuse_enabled:
        out["note"] = "выключен: журнал остаётся источником истины, терять нечего"
        return out
    from aegis.governance.langfuse import ExportUnavailable, LangfuseTarget

    try:
        target = LangfuseTarget.from_settings(cfg)
    except ExportUnavailable as exc:
        out["ok"] = False
        out["error"] = str(exc)[:200]
        out["hint"] = "нужны LANGFUSE_HOST и пара ключей проекта"
        return out
    out["host"] = target.describe()
    try:
        rows = await _scalar(
            "SELECT count(*) FROM governance.decision_records "
            "WHERE created_at >= now() - make_interval(hours => :hours)",
            hours=int(cfg.langfuse_window_hours),
        )
    except Exception as exc:  # noqa: BLE001 - состояние БД уже показывает проба postgres
        out["note"] = f"журнал не прочитан: {type(exc).__name__}"
        return out
    out["records"] = int(rows or 0)
    out["window_h"] = int(cfg.langfuse_window_hours)
    out["note"] = (
        "готово к выгрузке: aegis export langfuse"
        if out["records"]
        else "в окне записей нет: выгружать нечего"
    )
    return out


async def _outbox_report(cfg: Any) -> dict[str, Any]:
    """Очередь outbox: сколько событий ждёт публикации и сколько застряло.

    Проба справочная (`ok=true` всегда) — по той же причине, что и две предыдущие: без NATS бот
    работает, события лежат в таблице и никуда не деваются. А вот «attempts исчерпаны» — это уже
    «relay крутится впустую», и это `hint`, который надо увидеть в выводе, а не в статусе
    контейнера.
    """
    out: dict[str, Any] = {"ok": True}
    try:
        ready = await _scalar("SELECT to_regclass('platform.outbox') IS NOT NULL")
    except Exception as exc:  # noqa: BLE001 - настоящая ошибка БД уже в проверке postgres
        out["note"] = f"не проверялось ({type(exc).__name__})"
        out["hint"] = "детали — в проверке postgres"
        return out
    if not ready:
        out["state"] = "нет таблицы"
        out["hint"] = (
            "docker compose -f deploy/docker-compose.yml run --rm bot alembic upgrade head"
        )
        return out
    pending = int(
        await _scalar("SELECT count(*) FROM platform.outbox WHERE published_at IS NULL") or 0
    )
    threshold = int(cfg.outbox_max_attempts)
    stuck = int(
        await _scalar(
            "SELECT count(*) FROM platform.outbox WHERE published_at IS NULL AND attempts >= :n",
            n=threshold,
        )
        or 0
    )
    out["pending"] = pending
    out["stuck"] = stuck
    out["relay"] = "включён" if cfg.outbox_relay_enabled else "выключен"
    out["stream"] = cfg.nats_stream or "core NATS (без ack)"
    if not pending:
        out["note"] = "очередь пуста"
        return out
    out["note"] = f"{pending} ждут публикации · тик ≤ {cfg.outbox_batch} · attempts ≥ {threshold}"
    if stuck:
        out["hint"] = (
            "события не принимаются транспортом: aegis outbox tick покажет причину; "
            "вернуть в очередь — UPDATE platform.outbox SET attempts = 0"
        )
    elif not cfg.outbox_relay_enabled:
        out["hint"] = "копятся, пока relay выключен: OUTBOX_RELAY_ENABLED=true + aegis-outbox.timer"
    return out


async def _postgres_report() -> dict[str, Any]:
    """Связность И состояние схемы. Раньше «порт открыт, таблиц нет» печаталось как «БД недоступна».

    Версия alembic нужна, чтобы отвечать на «миграции накатаны?» не заглядывая в контейнер:
    именно этот вопрос стоил владельцу вечера.
    """
    out: dict[str, Any] = {"ok": False}
    try:
        out["server_version"] = str(await _scalar("SELECT current_setting('server_version')"))
    except Exception as exc:  # noqa: BLE001 - диагностика, а не бизнес-ошибка
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        out["hint"] = (
            "postgres не отвечает: docker compose -f deploy/docker-compose.yml ps postgres"
        )
        return out

    counts = (
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND n.nspname IN "
        "('platform', 'governance', 'memory', 'knowledge', 'planning')"
    )
    for key, sql in (
        ("schema_ready", "SELECT to_regclass('platform.events') IS NOT NULL"),
        ("tables", counts),
        ("alembic_version", "SELECT version_num FROM public.alembic_version"),
    ):
        try:
            out[key] = await _scalar(sql)
        except Exception as exc:  # noqa: BLE001
            out[key] = None
            out[f"{key}_error"] = type(exc).__name__

    out["tables"] = int(out.get("tables") or 0)
    if not out["schema_ready"]:
        out["hint"] = (
            "миграции не накатаны: docker compose -f deploy/docker-compose.yml "
            "run --rm bot alembic upgrade head"
        )
        return out
    try:
        out["events"] = int(await _scalar("SELECT count(*) FROM platform.events"))
    except Exception:  # noqa: BLE001, S110 - счётчик не важнее самого факта ok
        out["events"] = 0
    out["ok"] = True
    out["note"] = (
        f"таблиц {out['tables']} · миграции {out['alembic_version'] or 'не записаны'} "
        f"· событий {out['events']}"
    )
    return out


#: Живые пробы doctor: жёсткий потолок на запрос. Висящее соединение провайдера не имеет права
#: превращать `aegis doctor` в «ничего не выводится» — и уж тем более висящий HEALTHCHECK.
_PROBE_TIMEOUT_S = 20.0


def _probe_timeout_entry(label: str) -> dict[str, Any]:
    return {
        "ok": False,
        "role": label,
        "error": f"TimeoutError: проба «{label}» не завершилась за {_PROBE_TIMEOUT_S:.0f} c",
        "hint": (
            "провайдер держит соединение: проверь VPN/прокси/таймаут LLM_TIMEOUT_S; "
            "HEALTHCHECK контейнера ходит с --quick и от сети не зависит"
        ),
    }


async def _model_report(app: Any, cfg: Any) -> dict[str, Any]:
    """Живой запрос на 8 токенов: только так видно, примет ли провайдер ключ и модель.

    Только вне --quick: HEALTHCHECK контейнера ходит именно с --quick, чтобы не тратить бюджет.
    """
    from aegis.platform.gateway.diagnose import diagnose, gateway_auth_hint, redact_secrets

    out: dict[str, Any] = {"ok": False, "role": "fast"}
    try:
        res = await asyncio.wait_for(
            app.gateway.chat("fast", [{"role": "user", "content": "ping"}], max_tokens=8),
            timeout=_PROBE_TIMEOUT_S,
        )
    except TimeoutError:
        return _probe_timeout_entry("fast")
    except Exception as exc:  # noqa: BLE001
        # деталь транспорта живёт в cause (у ModelUnavailable), str() — только «нет провайдеров»
        cause = str(getattr(exc, "cause", "") or "")
        raw = f"{type(exc).__name__}: {cause or exc}"
        out["error"] = redact_secrets(raw)[:160]
        out["hint"] = diagnose(
            raw, timeout_s=cfg.llm_timeout_s, context=gateway_auth_hint(app.gateway)
        )
        return out
    out.update(
        ok=True,
        model=res.model,
        latency_ms=res.latency_ms,
        cost_usd=round(float(res.cost_usd), 6),
        note=f"{res.model} · {res.latency_ms} мс · ${float(res.cost_usd):.6f}",
    )
    return out


async def _models_report(app: Any, cfg: Any) -> dict[str, Any]:
    """По одному крошечному запросу на роль: ловит и «ключа нет», и «такой модели нет»."""
    from aegis.platform.gateway.diagnose import diagnose, gateway_auth_hint, redact_secrets

    out: dict[str, Any] = {"ok": True, "roles": {}}
    for role in ("brain", "fast", "vision"):
        try:
            res = await asyncio.wait_for(
                app.gateway.chat(
                    role, [{"role": "user", "content": "ping"}], max_tokens=8, thinking=False
                ),
                timeout=_PROBE_TIMEOUT_S,
            )
        except TimeoutError:
            out["roles"][role] = _probe_timeout_entry(role)
            out["ok"] = False
            continue
        except Exception as exc:  # noqa: BLE001
            raw = f"{type(exc).__name__}: {getattr(exc, 'cause', '') or exc}"
            out["roles"][role] = {
                "ok": False,
                "error": redact_secrets(raw)[:160],
                "hint": diagnose(
                    raw, timeout_s=cfg.llm_timeout_s, context=gateway_auth_hint(app.gateway)
                ),
            }
            out["ok"] = False
            continue
        out["roles"][role] = {"ok": True, "model": res.model, "latency_ms": res.latency_ms}
    try:
        vecs = await asyncio.wait_for(
            app.gateway.embed(["ping"]),
            timeout=_PROBE_TIMEOUT_S,  # эмбеддинги тоже в сети
        )
    except TimeoutError:
        out["roles"]["embed"] = _probe_timeout_entry("embed")
        out["ok"] = False
    except Exception as exc:  # noqa: BLE001
        raw = f"{type(exc).__name__}: {exc}"
        out["roles"]["embed"] = {"ok": False, "error": redact_secrets(raw)[:160]}
        out["ok"] = False
    else:
        dims = len(vecs[0]) if vecs else 0
        out["roles"]["embed"] = {"ok": dims > 0, "dims": dims}
        if dims and dims != cfg.embedding_dims:
            out["roles"]["embed"]["hint"] = (
                f"провайдер отдаёт {dims} измерений, а колонка на {cfg.embedding_dims} — "
                "нужна миграция либо dimensions=... в запросе"
            )
            out["ok"] = False
    out["note"] = " · ".join(
        f"{role}:{'ok' if r.get('ok') else 'FAIL'}" for role, r in out["roles"].items()
    )
    first_hint = next((r.get("hint") for r in out["roles"].values() if r.get("hint")), None)
    first_error = next((r.get("error") for r in out["roles"].values() if r.get("error")), None)
    if not out["ok"]:
        # человек читает плоский вывод, а не JSON: первая подсказка идёт наружу целиком
        out["error"] = str(first_error or "часть ролей недоступна")
        out["hint"] = str(first_hint or "смотри --json")
    return out


async def _bounded_probe(
    label: str, make: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """Одна живая проба с жёстким потолком: висящий внешний сервис не должен вешать doctor."""
    try:
        return await asyncio.wait_for(make(), timeout=_PROBE_TIMEOUT_S)
    except TimeoutError:
        return {
            "ok": False,
            "error": f"TimeoutError: проба «{label}» не завершилась за {_PROBE_TIMEOUT_S:.0f} c",
            "hint": "внешний сервис держит соединение: проверь VPN/прокси контейнера",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


async def _search_report(cfg: Settings) -> dict[str, Any]:
    """Поиск: вердикт и причины по каждому движку — ровно то, что иначе тонет в логах контейнера."""
    from aegis.web.search import WebSearch

    outcome = await WebSearch(cfg=cfg).outcome("курс евро к доллару", 3)
    return {
        "ok": outcome.verdict != "unavailable",
        "verdict": outcome.verdict,
        "hits": len(outcome.hits),
        "engines": [report.as_text() for report in outcome.engines],
        "note": f"движки: {cfg.search_engines}",
        "hint": None
        if outcome.verdict == "ok"
        else (
            "SearXNG отвечает, но пусто — смотри unresponsive_engines в ответе движка; "
            "второй движок (zai) включается списком SEARCH_ENGINES"
            if outcome.verdict == "empty"
            else "адрес SearXNG из контейнера — http://searxng:8080, а не localhost"
        ),
    }


async def _rates_report(cfg: Settings) -> dict[str, Any]:
    """Курсы: детерминированный путь обязан работать и при выключенных моделях — проверяем его."""
    from aegis.web.rates import RateQuestion, fetch_rates

    question = RateQuestion(base="USD", quote=cfg.rate_home_currency, mode="pair", raw="doctor")
    answer = await fetch_rates(question, cfg=cfg)
    ok = answer.verdict != "unavailable"
    return {
        "ok": ok,
        "verdict": answer.verdict,
        "sources": [quote.render_line() for quote in answer.quotes][:4],
        "note": (
            f"расхождение {answer.deviation_pct:.2f} % при допуске {cfg.rate_tolerance_pct:.2f} %"
        ),
        "hint": None if ok else "; ".join(answer.causes[:2]) or "источники курсов не ответили",
    }


async def _cmd_doctor(*, as_json: bool, quick: bool, models: bool = False) -> int:
    from aegis.agents.tools import load_builtin_tools
    from aegis.agents.tools.registry import registry

    load_builtin_tools()
    from aegis.platform.config import settings
    from aegis.runtime import build_app

    cfg = settings()
    report: dict[str, Any] = {
        "env": cfg.env,
        "config_missing": cfg.missing_runtime_keys(),
        "timezone": cfg.timezone,
        "base_currency": cfg.base_currency,
        "daily_budget_usd": cfg.daily_budget_usd,
        "tools": registry.names(),
        "checks": {},
    }
    # логи включаем всегда: setup_logging пишет в stderr, поэтому stdout при --json — это
    # ровно один объект, который можно отдать jq/HEALTHCHECK
    app = build_app(registry=registry, cfg=cfg, configure_logging=True)
    try:
        report["checks"]["postgres"] = await _postgres_report()
        report["checks"]["reminders"] = await _reminders_report(cfg)
        report["checks"]["notes_index"] = await _notes_index_report(cfg)
        report["checks"]["outbox"] = await _outbox_report(cfg)
        report["checks"]["turns"] = await _turns_report()
        report["checks"]["langfuse"] = await _langfuse_report(cfg)

        try:
            if cfg.kv_backend == "memory":
                report["checks"]["redis"] = {
                    "ok": True,
                    "backend": "memory",
                    "note": "не персистентно: история/pending/бюджет живут только в этом процессе",
                }
            else:
                pong = await app.redis.ping()
                report["checks"]["redis"] = {"ok": bool(pong), "backend": "redis"}
        except Exception as exc:  # noqa: BLE001
            report["checks"]["redis"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

        if not quick:
            # поиск и курсы проверяются так же, как они устроены: по движкам и по источникам.
            # «ok» здесь = «хотя бы один путь даёт данные», а не «контейнер SearXNG отвечает».
            report["checks"]["search"] = await _bounded_probe("search", lambda: _search_report(cfg))
            report["checks"]["rates"] = await _bounded_probe("rates", lambda: _rates_report(cfg))
            if models:
                report["checks"]["models"] = await _models_report(app, cfg)
            else:
                report["checks"]["model"] = await _model_report(app, cfg)
    finally:
        await app.aclose()

    checks = report["checks"]
    ok = all(bool(c.get("ok")) for c in checks.values())
    if as_json:
        print(json.dumps({**report, "ok": ok}, ensure_ascii=False))
    else:
        head = f"aegis {report['env']} · инструментов: {len(report['tools'])}"
        print(f"{head} · бюджет в день: ${report['daily_budget_usd']}")
        if report["config_missing"]:
            print("  ! не хватает в конфиге: " + ", ".join(report["config_missing"]))
        for name, res in checks.items():
            mark = "ok " if res.get("ok") else "!! "
            detail = " · ".join(
                str(x) for x in (res.get("error"), res.get("hint"), res.get("note")) if x
            )
            print(f"  {mark}{name:<9} {str(detail)[:120]}")
    return 0 if ok else 1


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение числительного — да, ради одной строки вывода.

    «Ушло бы 1 напоминаний» читается как «код писал тот, кому не до деталей», а доверие к боту
    складывается именно из них.
    """
    if count % 100 in range(11, 15):
        return f"{count} {many}"
    last = count % 10
    if last == 1:
        return f"{count} {one}"
    if last in range(2, 5):
        return f"{count} {few}"
    return f"{count} {many}"


async def _cmd_remind(action: str, args: argparse.Namespace) -> int:
    """Напоминания из консоли — без модели: момент считает парсер, а не угадывает LLM.

    Тот же путь, что проходит инструмент ``set_reminder``, — поэтому «в CLI работает, а в боте нет»
    невозможно по конструкции: расходиться может только разбор аргументов.
    """
    from datetime import UTC, datetime

    from aegis.planning.reminders import SqlReminderStore, deliver
    from aegis.planning.schedule import WhenNotParsed, humanize, parse_when
    from aegis.platform.config import ConfigError, settings

    try:
        cfg = settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    store = SqlReminderStore()

    if action == "add":
        body = " ".join(args.text)
        try:
            if args.at:
                due = datetime.fromisoformat(args.at)
                if due.tzinfo is None:
                    due = due.replace(tzinfo=cfg.tz)
                note, matched = "", args.at
            else:
                when = parse_when(args.when, now=datetime.now(cfg.tz), timezone=cfg.timezone)
                due, note, matched = when.at, when.note, when.matched
        except (WhenNotParsed, ValueError) as exc:
            print(f"! {exc}", file=sys.stderr)
            return 2
        if due < datetime.now(UTC):
            print("! в прошлом напоминания не ставлю", file=sys.stderr)
            return 2
        reminder_id = await store.add(owner_id=args.owner_id, body=body, due_at=due.astimezone(UTC))
        # та же форма, что у инструмента: абсолютный момент + «через сколько», — чтобы «через 4 мин»
        # не выглядело расхождением с «поставленными через 5»
        local = due.astimezone(cfg.tz)
        moment = humanize(due, now=datetime.now(cfg.tz), timezone=cfg.timezone)
        print(f"Поставлено на {local:%d.%m %H:%M} ({moment}) — {body}. id={reminder_id[:8]}")
        print(f"  разбор: {matched!r}")
        if note:
            print(f"  ! {note}", file=sys.stderr)
        return 0

    if action == "list":
        items = await store.list_scheduled(owner_id=args.owner_id, limit=args.limit)
        if not items:
            print("Запланированных напоминаний нет.")
            return 0
        now = datetime.now(cfg.tz)
        for item in items:
            moment = humanize(item.due_at, now=now, timezone=cfg.timezone)
            print(f"- {item.short_id} · {moment} · {item.text}")
        counts = await store.counts()
        if counts:
            print("  всего в расписании: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        return 0

    if action == "cancel":
        removed = await store.cancel(owner_id=args.owner_id, ref=args.ref)
        if removed is None:
            print(
                f"! ничего не отменено: {args.ref!r} не похоже на id или слово из текста",
                file=sys.stderr,
            )
            return 2
        print(f"Отменено (id={removed.short_id}): {removed.text}")
        return 0

    if action == "tick":
        limit = args.limit or cfg.reminders_batch
        if args.dry_run:
            pending = await store.peek_due(limit=limit)
            if not pending:
                print("Пусто: ничего не пора отправлять.")
                return 0
            kind = _plural(len(pending), "напоминание", "напоминания", "напоминаний")
            print(f"{kind} ушло бы (ничего не отправлено, БД не тронута):")
            for item in pending:
                print(f"  - {item.label(cfg.timezone)}")
            return 0
        from aegis.interaction.telegram.notify import TelegramNotifier

        try:
            notifier = TelegramNotifier.from_settings(cfg)
        except RuntimeError as exc:
            print(f"! {exc}", file=sys.stderr)
            return 2
        try:
            await notifier.start()
            report = await deliver(store, send=notifier.send, limit=limit)
        finally:
            await notifier.aclose()
        print(report.summary())
        for short in report.sent:
            print(f"  ok {short}")
        for short in report.failed:
            print(f"  !! {short}", file=sys.stderr)
        for short in report.exhausted:
            print(f"  !! {short}: попытки кончились", file=sys.stderr)
        # ненулевой выход нужен, чтобы systemd видел failed у юнита, а не «тихо и чисто»
        return 1 if report.failed else 0

    return 2


async def _cmd_index(action: str, args: argparse.Namespace) -> int:
    """Индексация заметок: очередь эмбеддингов, которую ведёт фоновый проход (ADR-0012).

    Отдельная команда, а не «встроить при сохранении»: путь записи принадлежит владельцу, а
    `EMBED_*`-настройки имеют смысл только тогда, когда их можно применить к пачке сразу. При этом
    `--dry-run` не трогает ни модель, ни БД — «посмотреть, сколько накопилось» не должно стоить
    запроса к провайдеру.
    """
    from aegis.knowledge.index import index_pending
    from aegis.knowledge.notes import NotesRepo
    from aegis.platform.config import ConfigError, settings

    if action != "notes":
        return 2
    try:
        cfg = settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    repo = NotesRepo()

    if args.dry_run:
        try:
            pending = await repo.count_pending()
        except Exception as exc:  # noqa: BLE001 - CLI обязан объяснить, а не показать стек
            print(f"! база не отвечает: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            return 1
        print(
            f"{pending} замет. ждут эмбеддинга; за один проход берём {cfg.embed_index_limit}, "
            f"пакетами по {cfg.embed_batch_size} (ничего не изменено)"
        )
        return 0

    from aegis.agents.tools.registry import ToolRegistry
    from aegis.runtime import build_app

    app = build_app(registry=ToolRegistry(), cfg=cfg, configure_logging=False)
    try:
        report = await index_pending(
            repo,
            app.services.gateway.embed,
            limit=args.limit or cfg.embed_index_limit,
            batch=args.batch or cfg.embed_batch_size,
            max_chars=cfg.embed_max_chars,
        )
    finally:
        await app.aclose()
    print(report.summary())
    # ненулевой выход нужен systemd: «провайдер лёг, ничего не проиндексировано» и «всё чисто»
    # должны различаться в `systemctl status`, а не в чтении логов
    return 0 if report.ok else 1


async def _cmd_export(action: str, args: argparse.Namespace) -> int:
    """Выгрузка журнала в Langfuse. Коды возврата — те же, что у `outbox tick`."""
    if action != "langfuse":
        return 2
    from aegis.governance.langfuse import (
        ExportFailed,
        ExportUnavailable,
        LangfuseTarget,
        collect,
        export_window,
    )
    from aegis.platform.config import ConfigError, settings

    try:
        cfg = settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2

    since = None
    if args.since:
        from datetime import datetime

        try:
            since = datetime.fromisoformat(str(args.since))
        except ValueError:
            print(f"! --since не читается как ISO-дата: {args.since!r}", file=sys.stderr)
            return 2

    if args.dry_run:
        # проба не требует ключей: «сколько накопилось» хотят узнать до того, как заведут витрину
        try:
            traces = await collect(
                since=since,
                limit=args.limit or cfg.langfuse_limit,
                trace_id=args.trace or "",
                max_chars=int(cfg.langfuse_max_chars),
            )
        except Exception as exc:  # noqa: BLE001 - без стека в выводе команды
            print(f"! база не отвечает: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            return 1
        spans = sum(len(trace) for trace in traces)
        print(
            f"к выгрузке — записей: {spans - len(traces)}, ходов: {len(traces)} · "
            f"витрина: {cfg.langfuse_host or 'не задана'} · ничего не отправлено"
        )
        return 0

    try:
        target = LangfuseTarget.from_settings(cfg)
    except ExportUnavailable as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    try:
        report = await export_window(
            target=target,
            cfg=cfg,
            since=since,
            limit=args.limit,
            trace_id=args.trace or "",
        )
    except ExportUnavailable as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    except ExportFailed as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - команда не должна ронять стек на пользователя
        print(f"! база не отвечает: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1
    print(report.summary())
    if report.stopped:
        print(f"  !! {report.stopped}", file=sys.stderr)
    return 0 if report.ok else 1


async def _cmd_outbox(action: str, args: argparse.Namespace) -> int:
    """Outbox → NATS: очередь событий, один проход relay'я, диагностика.

    Никакой другой логики здесь нет: `drain` — чистая функция над магазином и транспортом, и тот
    же путь проходит systemd-тик. Поэтому «в CLI публикует, а по таймеру нет» невозможно по
    конструкции, как и с напоминаниями.
    """
    from sqlalchemy import text

    from aegis.platform.config import ConfigError, settings
    from aegis.platform.db import session
    from aegis.platform.events.relay import RelayUnavailable, drain
    from aegis.platform.events.store import EventStore

    try:
        cfg = settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2

    if action == "stats":
        try:
            async with session() as s:
                counts = await EventStore(s).counts(max_attempts=cfg.outbox_max_attempts)
                total = int(await s.scalar(text("SELECT count(*) FROM platform.outbox")) or 0)
        except Exception as exc:  # noqa: BLE001 - CLI обязан объяснить, а не показать стек
            print(f"! база не отвечает: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            return 1
        print(
            f"ждут публикации {counts['pending']} из {total}, "
            f"с исчерпанными попытками: {counts['stuck']}"
        )
        print(
            f"  relay: {'включён' if cfg.outbox_relay_enabled else 'выключен'} · "
            f"тик ≤ {cfg.outbox_batch} · транспорт: {cfg.nats_url} · стрим: {cfg.nats_stream}"
        )
        # застрявшее — это «нужна реакция», и systemd должен видеть failed у юнита, а не «тихо»
        return 1 if counts["stuck"] else 0

    if action == "tick":
        from aegis.platform.events.nats import NatsPublisher
        from aegis.platform.events.relay import NullTransport

        # проба очереди не требует поднятого брокера: «сколько накопилось» хотят узнать и без
        # NATS. Требовать транспорт, чтобы посмотреть очередь, — значит обесценить пробу
        transport: Any = NullTransport() if args.dry_run else None
        if transport is None:
            try:
                transport = NatsPublisher.from_settings(cfg)
                await transport.start()
            except RelayUnavailable as exc:
                print(f"! {exc}", file=sys.stderr)
                return 2
            except Exception as exc:  # noqa: BLE001 - транспорт не обязан знать наши классы
                print(
                    f"! транспорт недоступен: {type(exc).__name__}: {str(exc)[:200]}",
                    file=sys.stderr,
                )
                return 2
        try:
            async with session() as s:
                report = await drain(
                    EventStore(s),
                    transport,
                    limit=args.limit or cfg.outbox_batch,
                    max_attempts=cfg.outbox_max_attempts,
                    dry_run=args.dry_run,
                    prefix=cfg.nats_subject_prefix,
                )
        except Exception as exc:  # noqa: BLE001 - тот же договор: без стека в выводе тика
            print(f"! база не отвечает: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            return 1
        finally:
            await transport.aclose()
        print(report.summary())
        return 0 if report.ok else 1

    return 2


async def _cmd_ask(text: str, owner_id: int) -> int:
    from aegis.agents.supervisor import Inbound
    from aegis.agents.tools import load_builtin_tools
    from aegis.agents.tools.registry import registry

    load_builtin_tools()
    from aegis.platform.config import ConfigError
    from aegis.runtime import build_app

    app = build_app(registry=registry)
    try:
        try:
            app.cfg.require_runtime()
        except ConfigError as exc:
            print(f"! {exc}", file=sys.stderr)
            return 2
        reply = await app.supervisor.handle(Inbound(text=text, owner_id=owner_id))
        print(reply.text)
        print(
            f"\n--- trace={reply.trace_id[:8]} model={reply.model} cost=${reply.cost_usd:.5f} "
            f"iter={reply.iterations}{' degraded' if reply.degraded else ''}",
            file=sys.stderr,
        )
        return 0
    finally:
        await app.aclose()


async def _cmd_bot() -> int:
    from aegis.interaction.telegram.bot import main as bot_main

    await bot_main()
    return 0


async def _cmd_repro(action: str, args: argparse.Namespace) -> int:
    """Команды журнала. Без ``build_app`` там, где хватает рекордера: сверка не должна падать
    из-за недоступного Telegram-токена — ровно для этого ``verify`` и заводится в cron.
    """
    from datetime import date, timedelta

    from aegis.governance.recorder import SqlDecisionRecorder
    from aegis.platform.config import ConfigError, settings

    try:
        cfg = settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    if not cfg.repro_enabled:
        print("! REPRO_ENABLED=false: журнал не ведётся, сверять нечего", file=sys.stderr)
        return 2
    recorder = SqlDecisionRecorder(cfg)

    if action == "stats":
        print(json.dumps(await recorder.stats(), indent=2, ensure_ascii=False, default=str))
        return 0

    if action == "verify":
        since = (date.today() - timedelta(days=args.days)).isoformat() if args.days > 0 else None
        chain = await recorder.verify(since=since)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": chain.ok,
                        "checked": chain.checked,
                        "gaps": chain.gaps,
                        "missing_blobs": chain.missing_blobs,
                        "truncated": chain.truncated,
                        "problems": chain.problems[:20],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"журнал с {since or 'начала'}: {chain.summary()}")
            if recorder.failures:
                print(f"! журнал писался с ошибками: {recorder.failures}", file=sys.stderr)
        return 0 if chain.ok else 1

    if action == "anchor":
        anchored = await recorder.anchor(args.day)
        if not anchored.ok:
            print(f"! якорь не поставлен: {anchored.note or 'нет записей'}", file=sys.stderr)
            return 1
        print(f"якорь {anchored.day}: {anchored.records} записей, root={anchored.merkle_root}")
        print("публикуй root туда, где его не изменить задним числом (git tag, почта, запись в БД)")
        return 0

    if action == "replay":
        from aegis.agents.tools import load_builtin_tools
        from aegis.agents.tools.registry import registry

        load_builtin_tools()
        from aegis.governance.replay import replay_trace
        from aegis.runtime import build_app

        ref = str(getattr(args, "trace", "") or "").strip()
        if not ref or args.last:
            ref = await recorder.latest_trace(owner_id=args.owner_id) or ""
        if not ref:
            print("! в журнале нет ни одного хода: воспроизводить нечего", file=sys.stderr)
            return 1
        matches = await recorder.matching_traces(ref)
        if not matches:
            print(f"! ход {ref} не найден (нужен UUID целиком или его начало от 6 символов)")
            return 1
        if len(matches) > 1:
            print("! началу идентификатора соответствует несколько ходов: " + ", ".join(matches))
            return 1
        app = build_app(registry=registry)
        try:
            report = await replay_trace(
                matches[0],
                recorder=recorder,
                gateway=app.gateway,
                judge_role="fast",
            )
            print(report.as_text())
            if report.judged:
                print(f"\n— было:\n{report.original[:1200]}")
                print(f"\n— стало:\n{report.replayed[:1200]}")
            return 0 if report.ok else 1
        finally:
            await app.aclose()

    print(f"! неизвестное действие {action}", file=sys.stderr)
    return 2


def _cmd_tools() -> int:
    from aegis.agents.tools import load_builtin_tools
    from aegis.agents.tools.registry import registry

    load_builtin_tools()

    for spec in registry.all():
        schema = spec.args.model_json_schema()
        props = ", ".join(schema.get("properties", {}).keys()) or "—"
        print(f"{spec.name:<16} writes={spec.writes!s:<5} risk={spec.risk:<6} args: {props}")
        print(f"{'':<16} {spec.description}")
    return 0


def _flag_golden_ids() -> set[str]:
    """Чтение evals/flags_v1.jsonl — вне event loop (блокирующий pathlib в async — мусор)."""
    import json as _json
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    out: set[str] = set()
    try:
        for line in (root / "evals" / "flags_v1.jsonl").read_text().splitlines():
            if line.strip():
                out.add(str(_json.loads(line).get("id")))
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ права (F2)


async def _db_guard(coro_factory: Callable[[], Awaitable[int]]) -> int:
    """Общая рамка db-команд: база не отвечает = объяснение, а не стек."""
    from aegis.platform.config import ConfigError, settings

    try:
        settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    try:
        return await coro_factory()
    except Exception as exc:  # noqa: BLE001 - CLI обязан объяснить, а не показать стек
        print(f"! база не отвечает: {type(exc).__name__}: {str(exc)[:220]}", file=sys.stderr)
        return 1


async def _cmd_principals(action: str, args: argparse.Namespace) -> int:
    from aegis.governance.principals import SqlPrincipalStore

    async def go() -> int:
        store = SqlPrincipalStore()
        if action == "list":
            rows = await store.list_principals()
            if not rows:
                print("принципалов пока нет: реестр наполняется при первом ходе")
                return 0
            for row in rows:
                grants = ",".join(sorted(row.get("grants") or [])) or "—"
                mark = "⏸" if row.get("killed") else " "
                budget = row.get("daily_budget_usd")
                spent = row.get("spent_today")
                print(
                    f"{mark} {row['principal_id']:>12} {row['kind']:<7} "
                    f"бюджет {'∞' if budget is None else f'${float(budget):.2f}'} "
                    f"(сегодня {'?' if spent is None else '$' + format(float(spent), '.2f')}) "
                    f"[{grants}]"
                )
            return 0
        if action == "kind":
            await store.ensure(args.id, kind=args.kind)
            await store.set_kind(args.id, args.kind)
            print(f"принципал {args.id}: роль = {args.kind}")
            return 0
        if action in ("grant", "revoke"):
            from aegis.governance.principals import KNOWN_ACTIONS

            if args.action not in KNOWN_ACTIONS:
                print(
                    f"! такого действия нет: {args.action}. Известные: "
                    + ", ".join(sorted(KNOWN_ACTIONS)),
                    file=sys.stderr,
                )
                return 2
            if action == "grant":
                await store.grant(args.id, args.action, by=1)
                print(f"{args.action} выдано принципалу {args.id}")
            else:
                await store.revoke(args.id, args.action)
                print(f"{args.action} отозвано у {args.id}")
            return 0
        if action == "kill":
            active = not args.off
            await store.set_kill_switch(args.id, active, args.reason)
            print(f"принципал {args.id}: {'на паузе' if active else 'снова работает'}")
            return 0
        if action == "budget":
            limit = None if args.clear or args.usd is None else float(args.usd)
            await store.set_daily_budget(args.id, limit)
            print(
                f"бюджет {args.id}: "
                + ("снят (действует общий)" if limit is None else f"${limit:.2f}/день")
            )
            return 0
        return 2

    return await _db_guard(go)


# ------------------------------------------------------------------ флаги (F6)


def _id_list(raw: str) -> list[int]:
    out: list[int] = []
    for piece in raw.replace(" ", "").split(","):
        if piece:
            try:
                out.append(int(piece))
            except ValueError:
                continue
    return out


async def _cmd_flags(action: str, args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from aegis.platform.config import settings
    from aegis.platform.db import session
    from aegis.platform.flags import FLAG_CATALOG, EnvFlagSource, FlagEngine, SqlFlagSource

    async def go() -> int:
        cfg = settings()
        source = SqlFlagSource(cfg, fallback=EnvFlagSource(cfg))
        engine = FlagEngine(source)
        rows = await source.load()
        if action == "list":
            golden_ids = await asyncio.to_thread(_flag_golden_ids)
            for key, spec in FLAG_CATALOG.items():
                row = rows.get(key) or {}
                pct = int(row.get("percent", 0))
                allow = row.get("allow") or row.get("allow_principals") or []
                deny = row.get("deny") or row.get("deny_principals") or []
                stage = row.get("stage", "env" if cfg else "?")
                covered = all(cid in golden_ids for cid in spec.golden if cid)
                line = (
                    f"  {key:<28} pct={pct:>3} stage={stage:<7} "
                    f"allow={','.join(map(str, allow)) or '—'} "
                    f"deny={','.join(map(str, deny)) or '—'}"
                )
                if spec.env_default:
                    line += f" env:{spec.env_default}={getattr(cfg, spec.env_default, None)}"
                line += "" if covered else "  ! без golden-пары"
                print(line)
                if args.actor is not None:
                    decision = await engine.evaluate(key, int(args.actor))
                    value = "ВКЛ" if decision.on else "выкл"
                    extra = (
                        f" bucket={decision.bucket}/{decision.percent}%"
                        if decision.basis == "bucket"
                        else ""
                    )
                    print(f"      actor {args.actor}: {value} ({decision.basis}{extra})")
            return 0
        if action == "set":
            if args.key not in FLAG_CATALOG:
                print(
                    "! флаг вне каталога — определи его в FLAG_CATALOG (и заведи golden-пару) "
                    "прежде чем писать в таблицу",
                    file=sys.stderr,
                )
                return 2
            async with session() as s:
                current = await s.scalar(
                    text("SELECT percent FROM platform.feature_flags WHERE key = :k").bindparams(
                        k=args.key
                    )
                )
                percent = args.percent if args.percent is not None else int(current or 0)
                await s.execute(
                    text(
                        """
                        INSERT INTO platform.feature_flags
                            (key, percent, allow_principals, deny_principals, stage, description)
                        VALUES
                            (:k, :p, CAST(:allow AS bigint[]), CAST(:deny AS bigint[]), :st, :d)
                        ON CONFLICT (key) DO UPDATE
                        SET percent = EXCLUDED.percent,
                            allow_principals = EXCLUDED.allow_principals,
                            deny_principals = EXCLUDED.deny_principals,
                            stage = EXCLUDED.stage,
                            description = CASE WHEN EXCLUDED.description = ''
                                               THEN platform.feature_flags.description
                                               ELSE EXCLUDED.description END,
                            updated_at = now()
                        """
                    ).bindparams(
                        k=args.key,
                        p=max(0, min(100, percent)),
                        allow=_id_list(args.allow),
                        deny=_id_list(args.deny),
                        st=args.stage,
                        d=args.reason,
                    )
                )
            print(f"флаг {args.key}: percent={percent} stage={args.stage} — применён")
            return 0
        if action == "stale":
            async with session() as s:
                stale_rows = (
                    await s.execute(
                        text(
                            "SELECT key, percent, (now() - created_at)::bigint AS age_s"
                            " FROM platform.feature_flags"
                            " WHERE percent = 100 AND stage <> 'retired'"
                            "   AND created_at < now() - make_interval(days => :days)"
                            " ORDER BY created_at"
                        ).bindparams(days=int(getattr(args, "days", None) or cfg.flag_stale_days))
                    )
                ).mappings()
                stale: list[dict[str, Any]] = [dict(r) for r in stale_rows]
            if not stale:
                print("протухших флагов нет")
                return 0
            for row in stale:
                print(
                    f"  {row['key']} живёт на 100% {int(row['age_s']) // 86400} дн —"
                    " удали вместе с кодом ветвления"
                )
            print("! флаг, откатывать который уже некого, — мусор; см. правило гигиены F6")
            return 1
        return 2

    return await _db_guard(go)


# ------------------------------------------------------------------ политика как код (F5)


async def _cmd_policy(action: str, args: argparse.Namespace) -> int:
    from aegis.platform.config import ConfigError, settings

    try:
        cfg = settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    if action == "lint":
        from aegis.governance.policy_rules import (
            default_ruleset,
            load_ruleset,
            lock_payload,
            plan_change_vs_lock,
        )

        try:
            file_rules = load_ruleset(cfg.policy_rules_path)
        except Exception as exc:  # noqa: BLE001 - показать владельцу, что файл сломан
            print(f"! {exc}", file=sys.stderr)
            return 1
        if file_rules is None:
            print(f"файл правил не задан ({cfg.policy_rules_path}) — работает вшитый набор")
            return 0
        lock = lock_payload(cfg.policy_lock_path)
        changes = plan_change_vs_lock(file_rules, lock, golden_rule_ids=_rule_golden_ids())
        print(f"{file_rules.name}: {len(file_rules.rules)} правил, sha {file_rules.sha()}")
        if not changes:
            print("против lock-файла изменений нет")
            return 0
        for change in changes:
            mark = "⚠️" if change.relaxed else "·"
            print(f"  {mark} {change.id}: {change.kind} {change.old} → {change.new}")
        bad = [c for c in changes if c.relaxed]
        if bad:
            print(
                "! смягчение вердикта требует golden-кейса с этим id в evals/golden_v1.jsonl "
                f"(не хватает: {', '.join(sorted({c.id for c in bad if True}))})",
                file=sys.stderr,
            )
            return 1
        return 0
    if action == "shadow":
        from sqlalchemy import text

        from aegis.agents.tools import load_builtin_tools
        from aegis.agents.tools.registry import UnknownTool, registry
        from aegis.governance.policy import PolicyEngine
        from aegis.governance.policy_rules import default_ruleset, load_ruleset
        from aegis.platform.db import session

        load_builtin_tools()
        file_rules = load_ruleset(cfg.policy_rules_path)
        if file_rules is None:
            print("теневой прогон бессмыслен без файла правил: задай AEGIS_POLICY_RULES_PATH")
            return 2
        builtin = PolicyEngine(
            auto_allow_low_risk=cfg.auto_allow_low_risk, ruleset=default_ruleset()
        )
        loaded = PolicyEngine(auto_allow_low_risk=cfg.auto_allow_low_risk, ruleset=file_rules)
        async with session() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT tool FROM governance.tool_runs"
                        " GROUP BY tool ORDER BY max(id) DESC LIMIT :lim"
                    ).bindparams(lim=max(1, min(500, int(getattr(args, "limit", 200) or 200))))
                )
            ).scalars()
            tools = [t for t in rows if t]
        diffs = 0
        for tool in tools:
            try:
                spec = registry.get(tool)
            except UnknownTool:
                continue
            ctx = {
                "tool": tool,
                "risk": str(spec.risk),
                "writes": bool(spec.writes),
                "source_trust": "owner",
                "confidence": 1.0,
                "kill_switch": False,
                "idempotent": False,
                "permission_missing": False,
                "non_owner": False,
                "budget_ratio": 0.0,
                "auto_allow_low_risk": cfg.auto_allow_low_risk,
            }
            left, _ = builtin.ruleset.evaluate(ctx)
            right, _ = loaded.ruleset.evaluate(ctx)
            if (left.verdict if left else "deny") != (right.verdict if right else "deny"):
                diffs += 1
                print(
                    f"  Δ {tool}: код → {left.verdict if left else 'deny'};"
                    f" файл → {right.verdict if right else 'deny'}"
                )
        if diffs == 0:
            print(
                f"дельта с вшитым поведением: пусто на {len(tools)} инструментах — "
                "файл можно включать"
            )
            return 0
        print(f"дельта: {diffs} — сначала golden'ы на спорные переходы, потом включение")
        return 1
    return 2


def _rule_golden_ids() -> list[str]:
    """id правил, покрытые golden-кейсами смены политики (evals/golden_v1.jsonl kind=policy)."""
    import json as _json
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    out: list[str] = []
    try:
        lines = (root / "evals" / "golden_v1.jsonl").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            case = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if case.get("kind") == "policy-change":
            out.append(str(case.get("rule") or case.get("id") or ""))
    return out


# ------------------------------------------------------------------ хранение (F3)


async def _cmd_retention(action: str, args: argparse.Namespace) -> int:
    from aegis.platform.config import settings

    async def go() -> int:
        cfg = settings()
        from aegis.governance.retention import (
            SqlHoldRepository,
            active_holds,
            apply_forget,
            apply_retention,
            keyring_status,
            list_shreds,
            load_retention_file,
            plan_forget,
            plan_retention,
            rewrap_batch,
        )

        if action == "plan":
            policy = load_retention_file(cfg.retention_file)
            plan = await plan_retention(policy=policy)
            if not plan:
                days = ", ".join(f"{k}={v}д" for k, v in sorted(policy.days.items()))
                print(f"удалять нечего; политики: {days or '—'}")
                return 0
            print("к удалению (план, ничего не тронуто):")
            for row in plan:
                blocked = row.get("blocked_by_holds") or []
                line = (
                    f"  {row.get('class')}: кандидатов {row.get('candidates')},"
                    f" берём до {row.get('limited_to')}, cutoff {str(row.get('cutoff'))[:19]}"
                )
                if blocked:
                    line += f" · hold'ы: {', '.join(map(str, blocked))}"
                print(line)
            print("apply без --execute — тот же план; удаление только с --execute")
            return 0
        if action == "apply":
            policy = load_retention_file(cfg.retention_file)
            result = await apply_retention(
                policy=policy, dry_run=not args.execute, limit=int(args.limit)
            )
            print(result.summary() if hasattr(result, "summary") else result)
            return 0
        if action == "holds":
            rows = await active_holds()
            if not rows:
                print("legal holds нет")
                return 0
            for row in rows:
                print(f"  {row.get('scope')}={row.get('value')}: {row.get('reason')}")
            return 0
        if action == "hold":
            scope, _, value = args.spec.partition("=")
            if not value:
                print("! формат: scope=value (principal=777 / class=journal)", file=sys.stderr)
                return 2
            if scope.strip() not in ("principal", "class"):
                print(
                    f"! scope {scope.strip()!r} nonexistent: legal_holds принимает"
                    " principal и class — и это не придирка CLI, это CHECK в схеме",
                    file=sys.stderr,
                )
                return 2
            repo = SqlHoldRepository()
            if args.release:
                await repo.release(scope.strip(), value.strip(), by=1)
                print(f"hold снят: {scope}={value}")
            else:
                await repo.place(scope.strip(), value.strip(), args.reason or "без причины", by=1)
                print(f"hold выставлен: {scope}={value} — ротация эти строки не тронет")
            return 0
        if action == "forget":
            fplan = await plan_forget(int(args.owner_id))
            print(fplan.summary())
            if not args.execute:
                print("это план; --execute шредит DEK и пишет манифесты")
                return 0
            from aegis.governance.recorder import SqlDecisionRecorder

            recorder = SqlDecisionRecorder(cfg)
            result = await apply_forget(fplan, recorder=recorder, dry_run=False)
            print(result.summary() if hasattr(result, "summary") else result)
            return 0
        if action == "rewrap":
            from aegis.platform.crypto import BlobCipher, load_keks

            keks = load_keks(cfg)
            if not keks:
                print("! AEGIS_KEK не задан — перекручивать нечем", file=sys.stderr)
                return 2
            cipher = BlobCipher(keks, active_version=cfg.crypto_key_version)
            done = await rewrap_batch(cipher, limit=int(cfg.rewrap_batch))
            print(
                f"re-wrap: {done.get('rewrapped', 0)} завернуто заново, "
                f"осталось {done.get('remaining', '?')} (пачки по {cfg.rewrap_batch}, SKIP LOCKED)"
            )
            return 0
        if action == "shreds":
            rows = await list_shreds()
            if not rows:
                print("манифестов уничтожения нет")
                return 0
            for row in rows:
                print(
                    f"  {str(row['blob_sha'])[:12]} v{row['key_version']} {row['reason']} "
                    f"({row['shredded_at']})"
                )
            return 0
        if action == "keyring":
            status = await keyring_status()
            for row in status.get("keys", []):
                print(
                    f"  KEK v{row['version']}: блобов {row['blobs']}, "
                    + ("активен" if row.get("active") else "на покое")
                )
            if not status.get("keys"):
                print("ключей в keyring нет: шифрование выключено (CRYPTO_MODE=off/auto без KEK)")
            return 0
        return 2

    return await _db_guard(go)


# ------------------------------------------------------------------ события (F4)


async def _cmd_events(action: str, args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from aegis.platform.db import session
    from aegis.platform.events.store import dlq_stats, replay_from_seq

    async def go() -> int:
        if action == "dlq":
            stats = await dlq_stats()
            print(
                f"DLQ: всего {stats.get('total', 0)}, не разобрано {stats.get('new', 0)}; "
                f"последняя причина: {stats.get('last_reason') or '—'}"
            )
            async with session() as s:
                rows = (
                    await s.execute(
                        text(
                            "SELECT reason, count(*)::int AS n FROM platform.event_dlq"
                            " GROUP BY reason ORDER BY n DESC LIMIT 5"
                        )
                    )
                ).mappings()
                for row in rows:
                    print(f"  {row['n']:>4} × {row['reason'][:110]}")
            return 1 if stats.get("new") else 0
        if action == "replay":
            async with session() as s:
                count = int(
                    await s.scalar(
                        text(
                            """
                            SELECT count(*)::int
                            FROM platform.outbox o
                            JOIN platform.events e ON e.id = o.event_id
                            WHERE e.id >= :from_seq
                              AND (CAST(:to_seq AS bigint) IS NULL OR e.id <= :to_seq)
                              AND (CAST(:etype AS text) IS NULL OR e.event_type = :etype)
                            """
                        ).bindparams(
                            from_seq=int(args.from_seq),
                            to_seq=args.to_seq if args.to_seq is not None else None,
                            etype=args.etype or None,
                        )
                    )
                    or 0
                )
            print(f"под переигрывание подходит {count} событий с seq >= {args.from_seq}")
            if not args.execute:
                print("это план; --execute сбросит published_at (повторы режет UNIQUE event_id)")
                return 0
            reset = await replay_from_seq(
                int(args.from_seq), to_seq=args.to_seq, event_type=args.etype
            )
            print(
                f"возвращено в очередь: {reset} — доставка at-least-once, потребитель идемпотентен"
            )
            return 0
        return 2

    return await _db_guard(go)


# ------------------------------------------------------------------ схема (F10)


def _file_revisions() -> list[tuple[str, str | None]]:
    import re as _re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1] / "migrations" / "versions"
    out: list[tuple[str, str | None]] = []
    for path in sorted(root.glob("0*.py")):
        text_data = path.read_text(encoding="utf-8")
        rev = _re.search(r'^revision(?:: str)? = "([^"]+)"', text_data, _re.M)
        down = _re.search(
            r'^down_revision(?:: str \| None)? = (?:"([^"]+)"|None)', text_data, _re.M
        )
        if rev:
            out.append((rev.group(1), down.group(1) if down and down.group(1) else None))
    return out


async def _cmd_migrate(action: str, args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from aegis.platform.db import session

    async def go() -> int:
        revs = _file_revisions()
        head = revs[-1][0] if revs else "?"
        try:
            async with session() as s:
                try:
                    stamped = await s.scalar(text("SELECT version_num FROM alembic_version"))
                except Exception:
                    stamped = None
                    await s.rollback()
                pending: list[dict[str, Any]] = []
                try:
                    rows = (
                        await s.execute(
                            text(
                                "SELECT name, status, rows_done, cursor->>'last_id' AS cursor"
                                " FROM platform.backfills WHERE status <> 'done' ORDER BY name"
                            )
                        )
                    ).mappings()
                    pending = [dict(r) for r in rows]
                except Exception:
                    await s.rollback()
                dangling: list[dict[str, Any]] = []
                try:
                    dangling_rows = (
                        await s.execute(
                            text(
                                "SELECT table_schema||'.'||table_name||'.'||column_name AS col"
                                " FROM information_schema.columns"
                                " WHERE table_schema IN"
                                "   ('platform','governance','knowledge','memory','planning')"
                                "   AND is_nullable = 'NO' AND column_default IS NULL"
                                "   AND table_name NOT LIKE '%_archive'"
                            )
                        )
                    ).scalars()
                    dangling = [{"col": c} for c in dangling_rows]
                except Exception:
                    await s.rollback()
        except Exception as exc:  # noqa: BLE001 - единый ответ «база не отвечает»
            print(f"! база не отвечает: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            return 1
        print(f"миграции: файлов {len(revs)}, head = {head}, в базе = {stamped or 'не проставлен'}")
        if stamped and stamped != head:
            print(f"  ! база отстаёт/не совпадает: {stamped} ≠ {head} — нагони alembic upgrade")
        if pending:
            print("незакрытые бэкфиллы:")
            for row in pending:
                print(
                    f"  {row['name']} [{row['status']}] строк {row['rows_done']}"
                    f", курсор {row.get('cursor') or '—'}"
                )
        else:
            print("бэкфиллов в работе нет")
        if dangling:
            print(
                f"NOT NULL без default: {len(dangling)} — при пере-накатке с нуля это ок,"
                " на живой таблице = замок; список:"
            )
            for row in dangling[:12]:
                print(f"  {row['col']}")
        return 0

    return await _db_guard(go)


# ------------------------------------------------------------------ backfill (F10)


async def _cmd_backfill(action: str, args: argparse.Namespace) -> int:
    import os
    import socket

    from sqlalchemy import text

    from aegis.platform.backfill import BACKFILL_SPECS, SqlBackfillStore
    from aegis.platform.db import session

    async def go() -> int:
        store = SqlBackfillStore()
        if action == "status":
            jobs = await store.status()
            if not jobs:
                print("заданий нет: их регистрирует мигратор при expand-релизе")
                return 0
            for bj in jobs:
                spec = BACKFILL_SPECS.get(bj.name)
                remaining = None
                if spec is not None:
                    remaining = await store.count_remaining(spec)
                print(
                    f"  {bj.name:<18} {bj.status:<8} строк {bj.rows_done:>8}"
                    + (f", осталось {remaining}" if remaining is not None else "")
                    + f" — {getattr(bj, 'target', '')}"
                )
            return 0
        if action == "pause":
            store = SqlBackfillStore()
            jobs = await store.status()
            wanted = next((j for j in jobs if j.name == args.name), None)
            if wanted is None:
                print(f"! задания {args.name} нет в работе", file=sys.stderr)
                return 2
            await store.pause(wanted)
            print(f"{args.name}: пауза — следующий claim его не возьмёт, прогресс сохранён")
            return 0
        if action == "run":
            owner = f"cli:{socket.gethostname()}:{os.getpid()}"
            moved_total = 0
            for _ in range(max(1, int(args.rounds))):
                job = await store.claim(
                    owner=owner, lease_secs=int(args.lease_secs), name=args.name
                )
                if job is None:
                    break
                spec = BACKFILL_SPECS.get(job.name)
                if spec is None:
                    await store.fail(job, f"нет спека {job.name} в BACKFILL_SPECS")
                    print(f"! {job.name}: спек пропал — помечено как fail", file=sys.stderr)
                    continue
                try:
                    async with session() as s:
                        result = await s.execute(
                            text(spec.sql).bindparams(cursor=job.cursor or {}, limit=job.batch_size)
                        )
                        ids = [row[0] for row in result]
                        rows_done = len(ids)
                        new_cursor = {"last_id": ids[-1]} if ids else job.cursor
                    await store.finish_batch(
                        job, rows_done, new_cursor, more=rows_done >= job.batch_size
                    )
                    moved_total += rows_done
                    print(f"  {job.name}: +{rows_done} (курсор {new_cursor.get('last_id', '—')})")
                except Exception as exc:  # noqa: BLE001 - батч не роняет задание целиком
                    await store.fail(job, repr(exc)[:300])
                    print(f"! {job.name}: {repr(exc)[:200]} — лизинг освобождён", file=sys.stderr)
            if moved_total == 0:
                print("брать нечего: очередь пуста или всё на паузе")
            return 0
        return 2

    return await _db_guard(go)


# ------------------------------------------------------------------ SLO (F7)


async def _cmd_slo(action: str, args: argparse.Namespace) -> int:
    from aegis.platform.config import ConfigError, settings

    try:
        cfg = settings()
    except ConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    if action == "alerts":
        from aegis.platform.slo import load_slo_file, render_alerts

        try:
            slo_set = load_slo_file(cfg.slo_path)
        except Exception as exc:  # noqa: BLE001 - показать, что файл кривой
            print(f"! {exc}", file=sys.stderr)
            return 1
        body = render_alerts(slo_set)
        if args.write:
            written = await asyncio.to_thread(_write_text_file, args.write, body)
            print(f"алерты записаны: {written}")
        else:
            print(body, end="")
        return 0

    async def go() -> int:
        from aegis.platform.metrics import SqlMetricsStore
        from aegis.platform.slo import evaluate_set, load_slo_file
        from aegis.platform.slo_sources import collect_observations

        try:
            slo_set = load_slo_file(cfg.slo_path)
        except FileNotFoundError:
            print(f"SLO не заведены: {cfg.slo_path} отсутствует")
            return 0
        observations = await collect_observations(
            slo_set,
            window_minutes=cfg.slo_window_minutes,
            metrics=SqlMetricsStore() if action == "tick" else None,
        )
        reports = evaluate_set(
            slo_set,
            {name: obs.window for name, obs in observations.items()},
            short={name: obs.short for name, obs in observations.items() if obs.short},
            long={name: obs.long for name, obs in observations.items() if obs.long},
        )
        from aegis.platform.slo import summarize

        print(summarize(reports))
        if action == "status":
            return 1 if any(r.failing and r.page for r in reports) else 0
        # tick: право переключать поведение — за env, не за нами
        if not cfg.slo_enforce_degradation:
            print("SLO_ENFORCE_DEGRADATION=false — деградацию не трогаю")
            return 0
        from aegis.governance.degradation import DegradationController, SqlOverrides

        overrides = SqlOverrides()
        if args.dry_run:
            from aegis.platform.slo import desired_switches

            wanted = desired_switches(reports)
            print(
                "включилось бы: "
                + (", ".join(f"{k}=on" for k, v in wanted.items() if v) or "ничего")
            )
            return 0
        from aegis.governance.recorder import SqlDecisionRecorder

        controller = DegradationController(
            overrides,
            journal=SqlDecisionRecorder(cfg),
            ttl_s=cfg.degradation_ttl_seconds,
        )
        changes = await controller.apply(reports)
        for note in changes:
            print(f"  {note}")
        if not changes:
            print("деградация не менялась: всё в бюджете или уже переключено")
        return 1 if any(r.failing and r.page for r in reports) else 0

    return await _db_guard(go)


async def _cmd_turns(action: str, args: argparse.Namespace) -> int:
    """Ходы (F1) глазами оператора: аренда, залипшие claimed, ручная расчистка.

    ``release`` использует незащищённый финал специально: оператор, снимающий зависший ход,
    не может знать текущий fencing-токен — это путь, помеченный в turns.py как «CLI-расчистка».
    """
    from sqlalchemy import text

    from aegis.platform.db import session

    async def go() -> int:
        import uuid as _uuid  # noqa: PLC0415

        from aegis.governance.turns import SqlTurnLedger  # noqa: PLC0415

        ledger = SqlTurnLedger()
        if action == "status":
            st = await ledger.status()
            print("сводка: " + " · ".join(f"{k} {v}" for k, v in sorted(st.items())))
            async with session() as s:
                claims = (
                    (
                        await s.execute(
                            text(
                                "SELECT owner_id, trace_id::text AS trace, step,"
                                " GREATEST(0, EXTRACT(EPOCH FROM (lease_until - now()))::int)"
                                " AS lease_s"
                                " FROM governance.turn_claims WHERE status = 'active'"
                                " ORDER BY lease_until"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                stranded = (
                    (
                        await s.execute(
                            text(
                                "SELECT id, owner_id, trace_id::text AS trace, attempts,"
                                " EXTRACT(EPOCH FROM (now() - claimed_at))::int AS idle_s"
                                " FROM governance.turn_queue"
                                " WHERE status = 'claimed' AND claimed_at < now()"
                                "   - make_interval(secs => 60) ORDER BY id"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                await s.commit()
            for row in claims:
                print(
                    f"  активный ход: владелец {row['owner_id']} · trace {str(row['trace'])[:8]}"
                    f" · шаг {row['step']} · аренда ещё {row['lease_s']}s"
                )
            for row in stranded:
                print(
                    f"  осиротевший claim очереди: id {row['id']} · владелец {row['owner_id']}"
                    f" · попыток {row['attempts']} · висит {row['idle_s']}s — aegis turns drain"
                )
            if not claims and not stranded:
                print("ни активных заявок, ни осиротевших элементов — ходы идут как надо")
            return 1 if stranded else 0
        if action == "release":
            raw = str(args.trace).strip()
            try:
                trace = str(_uuid.UUID(raw))
            except ValueError:
                print("! trace_id должен быть uuid", file=sys.stderr)
                return 2
            closed = await ledger.finish(trace)
            if closed:
                print(f"ход {trace[:8]} закрыт — владелец свободен, очередь разберёт бот")
                return 0
            print(f"активного хода {trace[:8]} нет — освобождать нечего", file=sys.stderr)
            return 1
        if action == "drain":
            owner = int(args.owner_id)
            async with session() as s:
                ids = list(
                    (
                        await s.execute(
                            text(
                                "SELECT id FROM governance.turn_queue WHERE owner_id = :o"
                                " AND status = 'claimed'"
                                " AND claimed_at < now() - make_interval(secs => 60)"
                                " ORDER BY id"
                            ).bindparams(o=owner)
                        )
                    ).scalars()
                )
                if not ids:
                    await s.commit()
                    print(
                        f"владелец {owner}: осиротевших элементов нет — очередь"
                        " разберёт бот на следующем ходе"
                    )
                    return 0
                if not args.execute:
                    await s.commit()
                    print(
                        f"владелец {owner}: {len(ids)} осиротевших claim'ов (id "
                        + ", ".join(str(x) for x in ids[:10])
                        + "); apply без --execute — тот же план"
                    )
                    return 0
                moved: Any = await s.execute(
                    text(
                        "UPDATE governance.turn_queue SET status = 'queued', claimed_at = NULL"
                        " WHERE id = ANY(CAST(:ids AS bigint[])) AND status = 'claimed'"
                    ).bindparams(ids=ids)
                )
                await s.commit()
                print(f"владелец {owner}: возвращено в очередь {moved.rowcount} — бот подхватит")
                return 0
        return 2

    return await _db_guard(go)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "bot":
            return asyncio.run(_cmd_bot())
        if args.command == "doctor":
            return asyncio.run(_cmd_doctor(as_json=args.json, quick=args.quick, models=args.models))
        if args.command == "ask":
            return asyncio.run(_cmd_ask(" ".join(args.text), args.owner_id))
        if args.command == "tools":
            return _cmd_tools()
        if args.command == "repro":
            return asyncio.run(_cmd_repro(args.repro_action, args))
        if args.command == "remind":
            return asyncio.run(_cmd_remind(args.remind_action, args))
        if args.command == "index":
            return asyncio.run(_cmd_index(args.index_action, args))
        if args.command == "outbox":
            return asyncio.run(_cmd_outbox(args.outbox_action, args))
        if args.command == "export":
            return asyncio.run(_cmd_export(args.export_action, args))
        if args.command == "principals":
            return asyncio.run(_cmd_principals(args.principals_action, args))
        if args.command == "flags":
            return asyncio.run(_cmd_flags(args.flags_action, args))
        if args.command == "policy":
            return asyncio.run(_cmd_policy(args.policy_action, args))
        if args.command == "retention":
            return asyncio.run(_cmd_retention(args.retention_action, args))
        if args.command == "events":
            return asyncio.run(_cmd_events(args.events_action, args))
        if args.command == "migrate":
            return asyncio.run(_cmd_migrate(args.migrate_action, args))
        if args.command == "backfill":
            return asyncio.run(_cmd_backfill(args.backfill_action, args))
        if args.command == "slo":
            return asyncio.run(_cmd_slo(args.slo_action, args))
        if args.command == "turns":
            return asyncio.run(_cmd_turns(args.turns_action, args))
    except KeyboardInterrupt:
        print("остановлено", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
