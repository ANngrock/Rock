"""Живой Postgres без Docker: portable-кластер + миграции + запуск команды.

Зачем: инцидент «Сбой: TypeError в каждом сообщении» родился именно на стыке «мокрый» тест
зелёный, а реальный SQL не выполнялся никогда. Этот инструмент делает путь «прогнать интеграции
на настоящей БД» однотипным и не требующим Docker:

    python tools/with_local_pg.py -- pytest -q -m integration
    python tools/with_local_pg.py --keep -- bash   # URL печатается: им можно подключиться psql

``--strip-ext`` вырезает из миграции statements, которым нужны pg_trgm/pgcrypto (в переносном
кластере их нет). На настоящем образке pgvector (``make up-core``) этот флаг не нужен.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / ".cache" / "aegis-pg"
UNAVAILABLE_EXT = ("pg_trgm", "pgcrypto", "gin_trgm_ops")


def _alembic(url: str) -> int:
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env
    ).returncode


def _migration_sql() -> str:
    """SQL всех миграций подряд (0001, 0002, …) — то, что выполнил бы ``alembic upgrade head``.

    раньше брался ровно один файл и ровно один блок: с появлением 0002 тестовая база молча
    осталась бы без таблиц журнала, и «интеграция зелёная» означала бы «мы не туда посмотрели».
    Поэтому — все тройные SQL-блоки внутри ``upgrade()`` каждой миграции, по порядку файлов.
    """
    files = sorted((ROOT / "migrations" / "versions").glob("0*.py"))
    if not files:
        raise SystemExit("миграции не найдены")
    parts: list[str] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        starts = [i for i, ln in enumerate(lines) if ln.startswith("def upgrade")]
        if not starts:
            raise SystemExit(f"в {path.name} нет upgrade() — формат миграции поменялся")
        ends = [i for i, ln in enumerate(lines) if ln.startswith("def downgrade")]
        stop = min((i for i in ends if i > starts[0]), default=len(lines))
        i = starts[0]
        while i < stop:
            if "op.execute(" not in lines[i]:
                i += 1
                continue
            fence = next((j for j in (i, i + 1) if '"""' in lines[j]), None)
            if fence is None:
                raise SystemExit(
                    f"{path.name}:{i + 1}: op.execute без тройной кавычки — перепиши харнесс"
                )
            if lines[fence].count('"""') == 2:
                inner = lines[fence].split('"""')[1]
                parts.append(inner)
                i += 1
                continue
            close = next((j for j in range(fence + 1, stop) if lines[j].strip() == '"""'), None)
            if close is None:
                raise SystemExit(f"{path.name}: не закрыт SQL-блок миграции")
            parts.append("\n".join(lines[fence + 1 : close]))
            i = close + 1
    return "\n".join(parts)


def _strip(sql: str) -> str:
    dropped = [ln for ln in sql.splitlines() if any(k in ln for k in UNAVAILABLE_EXT)]
    for ln in dropped:
        print(f"  пропущено (нет расширения): {ln.strip()[:90]}")
    return "\n".join(ln for ln in sql.splitlines() if ln not in dropped)


def _apply_manual(url: str) -> None:
    import asyncio

    import asyncpg

    plain = re.sub(r"postgresql\+asyncpg://", "postgresql://", url)
    sql = _strip(_migration_sql())

    async def run() -> None:
        conn = await asyncpg.connect(dsn=plain)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run())


def _schema_present(url: str) -> bool:
    """Накатана ли схема: datadir переиспользуется между запусками, повторять DDL нельзя."""
    import asyncio

    import asyncpg

    plain = re.sub(r"postgresql\+asyncpg://", "postgresql://", url)

    async def run() -> bool:
        conn = await asyncpg.connect(dsn=plain)
        try:
            return bool(await conn.fetchval("SELECT to_regclass('platform.events')"))
        finally:
            await conn.close()

    return asyncio.run(run())


def _stamp_head(url: str) -> None:
    """Схему накатали SQL'ом — alembic про это не знает, иначе следующий upgrade всё сломает."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "stamp", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=False,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strip-ext",
        action="store_true",
        help="вырезать из миграции statements с pg_trgm/pgcrypto",
    )
    parser.add_argument(
        "--keep", action="store_true", help="оставить кластер работать после команды"
    )
    parser.add_argument("--fresh", action="store_true", help="пересоздать datadir (чистая БД)")
    parser.add_argument("command", nargs="*", help="команда после `--`")
    ns = parser.parse_args(argv)

    try:
        import pgserver
    except ImportError:
        print(
            "! нужен portable-Postgres: python -m pip install 'aegis[dev]' (пакет pgserver)",
            file=sys.stderr,
        )
        return 2

    if ns.fresh and DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(DATA_DIR), cleanup_mode=None if ns.keep else "stop")
    info = server.get_postmaster_info()
    if info is None or not info.socket_dir:
        print("! pgserver не сообщил socket_dir", file=sys.stderr)
        return 2
    url = f"postgresql+asyncpg://postgres@/postgres?host={info.socket_dir}"
    print(f"Postgres (portable): {url}", flush=True)

    if _schema_present(url):
        print("миграции: схема уже накатана (datadir переиспользуется)", flush=True)
    elif _alembic(url) != 0:
        if not ns.strip_ext:
            print(
                "! миграции не легли; для переносного Postgres без pg_trgm/pgcrypto добавь "
                "--strip-ext",
                file=sys.stderr,
            )
            return 3
        print("alembic не смог (нет расширений) — накатываю SQL без них", flush=True)
        _apply_manual(url)
        _stamp_head(url)

    env = {
        **os.environ,
        "DATABASE_URL": url,
        "AEGIS_TEST_DATABASE_URL": url,
        "GLM_API_KEY": os.environ.get("GLM_API_KEY", "local-test-key"),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", "1:local-test-token"),
        "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    command = ns.command or ["pytest", "-q", "-m", "integration"]
    print(f">>> {' '.join(command)}", flush=True)
    # команду передаёт оператор руками в своей же репе — доверенный ввод
    return subprocess.run(command, cwd=ROOT, env=env).returncode  # noqa: S603


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    raise SystemExit(main(args))
