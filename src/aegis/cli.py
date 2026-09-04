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
from typing import Any

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aegis", description="Aegis — личный AI-менеджер (шаг 1)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bot", help="запустить Telegram-бота (polling)")

    doctor = sub.add_parser("doctor", help="проверить конфигурацию и связности")
    doctor.add_argument("--json", action="store_true", help="машинный вывод (для HEALTHCHECK)")
    doctor.add_argument("--quick", action="store_true", help="не ходить в сеть (только конфиг)")

    ask = sub.add_parser("ask", help="одиночный запрос к supervisor без Telegram")
    ask.add_argument("text", nargs="+", help="текст сообщения")
    ask.add_argument("--owner-id", type=int, default=1)

    sub.add_parser("tools", help="список зарегистрированных инструментов")
    return parser


async def _cmd_doctor(*, as_json: bool, quick: bool) -> int:
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
        from sqlalchemy import text

        from aegis.platform.db import session

        try:
            async with session() as s:
                version = await s.scalar(text("SELECT current_setting('server_version')"))
                events = await s.scalar(text("SELECT count(*) FROM platform.events"))
                report["checks"]["postgres"] = {
                    "ok": True,
                    "server_version": str(version),
                    "events": int(events),
                }
        except Exception as exc:  # noqa: BLE001 - диагностика, а не бизнес-ошибка
            report["checks"]["postgres"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }

        try:
            pong = await app.redis.ping()
            report["checks"]["redis"] = {"ok": bool(pong)}
        except Exception as exc:  # noqa: BLE001
            report["checks"]["redis"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

        if not quick:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{cfg.searxng_url.rstrip('/')}/search",
                        params={"q": "test", "format": "json"},
                    )
                    ok = resp.status_code == 200
                    report["checks"]["searxng"] = {
                        "ok": ok,
                        "status": resp.status_code,
                        "hint": None
                        if ok
                        else "включи formats: [html, json] в deploy/searxng/settings.yml",
                    }
            except Exception as exc:  # noqa: BLE001
                report["checks"]["searxng"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
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
            detail = res.get("error") or res.get("hint") or ""
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
            return asyncio.run(_cmd_doctor(as_json=args.json, quick=args.quick))
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
