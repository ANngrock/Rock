"""Контракты deploy-артефактов: их не покрывают юнит-тесты, но именно они решают, жив ли бот.

Проверка текстовая и дешёвая: файл сборки — тоже код, и он уже ломал здоровье контейнера
(HEALTHCHECK вызывал полный `doctor`, который ходит в LLM: 401 у провайдера = «unhealthy»).
"""

from __future__ import annotations

import pathlib

DEPLOY = pathlib.Path(__file__).resolve().parents[1] / "deploy"


def _healthcheck_cmd() -> str:
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if "doctor" in line]
    assert lines, "в образе вообще нет healthcheck-команды"
    return lines[0]


def test_healthcheck_is_the_offline_probe() -> None:
    cmd = _healthcheck_cmd()
    assert "--quick" in cmd, f"HEALTHCHECK обязан быть офлайн: {cmd}"
    assert "--json" in cmd, "вывод должен оставаться машиночитаемым (одна строка JSON)"


def test_compose_bot_delegates_health_to_image() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    bot_block = compose.split("  bot:")[1].split("\n  ")[0]
    assert "healthcheck:" not in bot_block, "двойной healthcheck только запутает диагноз"
