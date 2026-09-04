"""Aegis — личный AI-менеджер. Шаг 1: фундамент.

Структура (см. docs/MASTER_PLAN.md):

* :mod:`aegis.platform`    — конфигурация, БД, event store, Model Gateway, наблюдаемость;
* :mod:`aegis.governance`  — policy engine, аудит, kill switch;
* :mod:`aegis.agents`      — supervisor, реестр инструментов, промпты;
* :mod:`aegis.memory` / :mod:`aegis.knowledge` / :mod:`aegis.web` — домены шага 1;
* :mod:`aegis.finance` / :mod:`aegis.planning` / :mod:`aegis.proactivity` — заглушки (шаги 3/5/8);
* :mod:`aegis.interaction`  — Telegram и (позже) mini app API.

Границы между доменами проверяются import-linter (см. pyproject.toml).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
