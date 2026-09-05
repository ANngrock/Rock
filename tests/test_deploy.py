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


def test_every_systemd_unit_is_an_aegis_oneshot() -> None:
    """Юниты — тоже код: `Type=oneshot` без `WantedBy`, опечатка в `OnCalendar` или вызов не `aegis`
    означают, что бэкап/якорь/напоминания просто никогда не запустятся — и молча."""
    for path in sorted((DEPLOY / "systemd").glob("*.service")):
        body = path.read_text(encoding="utf-8")
        assert "[Service]" in body, f"{path.name}: нет секции [Service]"
        assert "Type=oneshot" in body, f"{path.name}: ожидается разовая задача"
        start = [ln for ln in body.splitlines() if ln.startswith("ExecStart=")]
        assert start, f"{path.name}: нечем запускать"
        assert any("aegis " in ln or "backup.sh" in ln for ln in start), (
            f"{path.name}: ExecStart обязан идти в aegis (или в скрипт бэкапа)"
        )
        assert "WantedBy=multi-user.target" in body, f"{path.name}: юнит никуда не включается"
    assert (DEPLOY / "systemd").glob("*.timer"), "в репозитории не осталось таймеров"


def test_reminder_timer_fires_more_often_than_a_day() -> None:
    """Смысл напоминаний — в частоте: ежедневный тик превращает «в 9:00» в «когда-нибудь утром»."""
    timer = (DEPLOY / "systemd" / "aegis-reminders.timer").read_text(encoding="utf-8")
    service = (DEPLOY / "systemd" / "aegis-reminders.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:00/5:00" in timer, "интервал тика съехал"
    assert "Persistent=true" in timer, "после простоя просроченное должно догнаться"
    assert "WantedBy=timers.target" in timer
    assert "aegis remind tick" in service
    # боевой юнит отправляет по-настоящему: «посмотреть без отправки» — отдельный ручной запуск CLI
    assert "--dry-run" not in service, "таймер не должен работать вхолостую"


def test_outbox_timer_publishes_and_reports_stuck_rows() -> None:
    """Тик relay'я обязан «гореть» красным, когда попытки исчерпаны: очередь — это не норма.

    Проверяется контракт, а не строки юнита «вообще»: `TimeoutStartSec` ограничен (строки держатся
    `FOR UPDATE` до commit'а, и висящий тик блокирует следующий), а `--dry-run` в боёвке означал бы
    «события никогда не уедут».
    """
    timer = (DEPLOY / "systemd" / "aegis-outbox.timer").read_text(encoding="utf-8")
    service = (DEPLOY / "systemd" / "aegis-outbox.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:00/5:00" in timer
    assert "Persistent=true" in timer, "после простоя очередь догоняется целиком"
    assert "aegis outbox tick" in service
    assert "--dry-run" not in service
    assert "TimeoutStartSec=" in service, "без потолка тик мог бы держать locks до бесконечности"


def test_index_timer_runs_a_batch_and_is_calm_about_its_schedule() -> None:
    """Индексация — не сервис здоровья: редкий таймер, низкий приоритет, «догнать после простоя».

    Полчаса между проходами здесь важнее, чем у напоминаний: очередь эмбеддингов не обязана быть
    мгновенной (поиск работает и без неё), а вызовы к провайдеру — платные.
    """
    timer = (DEPLOY / "systemd" / "aegis-index.timer").read_text(encoding="utf-8")
    service = (DEPLOY / "systemd" / "aegis-index.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:00/15:00" in timer, "интервал съехал — очередь будет отставать"
    assert "Persistent=true" in timer, "ночь без сервера = дневная пачка заметок без индекса"
    assert "aegis index notes" in service
    assert "Nice=10" in service and "IOSchedulingClass=idle" in service, (
        "индекс не должен мешать боту"
    )
    assert "--dry-run" not in service, "таймер обязан работать, а не докладывать"


def test_compose_bot_delegates_health_to_image() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    bot_block = compose.split("  bot:")[1].split("\n  ")[0]
    assert "healthcheck:" not in bot_block, "двойной healthcheck только запутает диагноз"
