"""CLI-точка входа: ``aegis bot|doctor|ask|tools``.

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
    return parser


async def _scalar(sql: str) -> Any:
    """Один запрос — одна сессия. Иначе упавший запрос отравляет остаток проверки."""
    from sqlalchemy import text

    from aegis.platform.db import session

    async with session() as s:
        return await s.scalar(text(sql))


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
        "WHERE c.relkind = 'r' AND n.nspname IN ('platform', 'governance', 'memory', 'knowledge')"
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
    from aegis.agents.tools import builtin  # noqa: F401  (регистрирует инструменты)
    from aegis.agents.tools.registry import registry
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


async def _cmd_ask(text: str, owner_id: int) -> int:
    from aegis.agents.supervisor import Inbound
    from aegis.agents.tools import builtin  # noqa: F401
    from aegis.agents.tools.registry import registry
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


def _cmd_tools() -> int:
    from aegis.agents.tools import builtin  # noqa: F401
    from aegis.agents.tools.registry import registry

    for spec in registry.all():
        schema = spec.args.model_json_schema()
        props = ", ".join(schema.get("properties", {}).keys()) or "—"
        print(f"{spec.name:<16} writes={spec.writes!s:<5} risk={spec.risk:<6} args: {props}")
        print(f"{'':<16} {spec.description}")
    return 0


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
    except KeyboardInterrupt:
        print("остановлено", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
