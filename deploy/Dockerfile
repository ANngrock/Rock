FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg — для STT/видео (шаг 2), но ставим сразу: пересборка образа дороже, чем 30 МБ.
# tini — корректная обработка SIGTERM (durable shutdown, no orphaned LLM calls).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ffmpeg libpq5 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e ".[durable]"

COPY alembic.ini ./
COPY migrations ./migrations

# Не root: компрометация инструмента агента ≠ компрометация хоста
RUN useradd -m -u 10001 aegis && chown -R aegis: /app
USER aegis

# health: конфиг читается, БД доступна. --quick обязателен: полная проверка ходит в SearXNG и
# в LLM, а HEALTHCHECK не имеет права зависеть от провайдера (таймаут пробы 10 c) и тратить бюджет.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -m aegis.cli doctor --quick --json >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["aegis", "bot"]
