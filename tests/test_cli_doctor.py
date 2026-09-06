"""doctor: пробы независимы, вывод пригоден для диагноза без доступа к контейнеру."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _FakeSession:
    """Подменяет только _scalar: проверяем логику сборки отчёта, не драйвер."""

    def __init__(self, values: dict[str, Any], *, fail: str | None = None) -> None:
        self.values = values
        self.fail = fail

    async def __call__(self, sql: str, **params: Any) -> Any:
        for needle, value in self.values.items():
            if needle in sql:
                if self.fail and self.fail in sql:
                    raise OSError("Connection refused")
                return value
        raise AssertionError(f"неожиданный запрос: {sql[:80]}")


async def test_unreachable_db_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis import cli

    async def boom(sql: str) -> Any:
        raise OSError("[Errno 111] Connect call failed ('127.0.0.1', 5432)")

    monkeypatch.setattr(cli, "_scalar", boom)
    report = await cli._postgres_report()
    assert report["ok"] is False
    assert "ps postgres" in report["hint"]


async def test_db_up_but_schema_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis import cli

    fake = _FakeSession(
        {
            "current_setting": "16.2",
            "to_regclass": False,
            "pg_class": 0,
            "alembic_version": None,
        }
    )
    monkeypatch.setattr(cli, "_scalar", fake)
    report = await cli._postgres_report()
    assert report["ok"] is False
    assert report["tables"] == 0
    assert "alembic upgrade head" in report["hint"], "главный вопрос вечера — накатаны ли миграции"


async def test_healthy_db_reports_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis import cli

    fake = _FakeSession(
        {
            "current_setting": "16.2",
            "to_regclass": True,
            "pg_class": 7,
            "alembic_version": "0001",
            "count(*) FROM platform.events": 42,
        }
    )
    monkeypatch.setattr(cli, "_scalar", fake)
    report = await cli._postgres_report()
    assert report["ok"] is True
    assert report["events"] == 42
    assert "миграции 0001" in report["note"]


async def test_broken_session_does_not_poison_other_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Упавший запрос про таблицу alembic не должен превращать проверку в «БД недоступна»."""
    from aegis import cli

    async def partial(sql: str) -> Any:
        if "alembic_version" in sql:
            raise RuntimeError("relation alembic_version does not exist")
        if "current_setting" in sql:
            return "16.2"
        if "to_regclass" in sql:
            return True
        if "pg_class" in sql:
            return 7
        return 0

    monkeypatch.setattr(cli, "_scalar", partial)
    report = await cli._postgres_report()
    assert report["ok"] is True
    assert report["alembic_version"] is None
    assert report["alembic_version_error"] == "RuntimeError"


async def test_model_probe_survices_provider_failure() -> None:
    from aegis.cli import _model_report
    from aegis.platform.config import Settings
    from aegis.platform.gateway.client import ModelUnavailable

    class Gateway:
        async def chat(self, *args: Any, **kwargs: Any) -> Any:
            raise ModelUnavailable(
                "нет", cause="AuthenticationError: Error code: 401 api_key=SECRET"
            )

    app = SimpleNamespace(gateway=Gateway())
    cfg = Settings(_env_file=None, glm_api_key="k", llm_timeout_s=13)
    report = await _model_report(app, cfg)
    assert report["ok"] is False
    assert "401" in report["hint"]
    assert "SECRET" not in report["error"] + report["hint"]


async def test_models_report_names_unknown_model() -> None:
    from types import SimpleNamespace

    from aegis.cli import _models_report
    from aegis.platform.config import Settings
    from aegis.platform.gateway.client import ModelUnavailable

    class Gateway:
        async def chat(self, role: str, *args: object, **kwargs: object) -> object:
            if role == "brain":
                raise ModelUnavailable(
                    "нет", cause="BadRequestError: Error code: 400 - Model Not Found api_key=SECRET"
                )

            class Res:
                model = "glm-4.5-air"
                latency_ms = 42

            return Res()

        async def embed(self, texts: object, **kwargs: object) -> list[list[float]]:
            return [[0.1, 0.2]]

    cfg = Settings(_env_file=None, glm_api_key="k", embedding_dims=2)
    report = await _models_report(SimpleNamespace(gateway=Gateway()), cfg)
    assert report["ok"] is False
    assert "не знает такую модель" in report["hint"]
    assert "SECRET" not in report["error"]
    assert report["roles"]["embed"]["ok"] is True


async def test_models_report_flags_dimension_mismatch() -> None:
    from types import SimpleNamespace

    from aegis.cli import _models_report
    from aegis.platform.config import Settings

    class Gateway:
        async def chat(self, role: str, *args: object, **kwargs: object) -> object:
            class Res:
                model = "glm-4.6"
                latency_ms = 10

            return Res()

        async def embed(self, texts: object, **kwargs: object) -> list[list[float]]:
            return [[0.0] * 1024]

    cfg = Settings(_env_file=None, glm_api_key="k", embedding_dims=2048)
    report = await _models_report(SimpleNamespace(gateway=Gateway()), cfg)
    assert report["ok"] is False
    assert "1024" in report["roles"]["embed"]["hint"]


# ----------------------------------------------------------------- контракт HEALTHCHECK


async def test_quick_skips_every_live_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--quick` = офлайн-контракт: его вызывает HEALTHCHECK контейнера с таймаутом 10 c.

    Живой провайдер внутри health-пробы — это «бот выглядит мёртвым» при 401 или таймауте у
    провайдера (и лишний бюджет на каждый цикл проверки).
    """
    import json

    from aegis import cli
    from aegis.platform.config import Settings

    async def forbidden(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError(f"--quick не имеет права ходить в сеть: {args[:1]}")

    async def ok(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    cfg = Settings(_env_file=None, kv_backend="memory", glm_api_key="k", daily_budget_usd=1.0)
    # doctor настраивает structlog на stderr; в тесте этот поток закроется вместе с capsys,
    # и логгер следующих тестов упал бы на «I/O operation on closed file»
    monkeypatch.setattr("aegis.runtime.setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr("aegis.platform.config.settings", lambda: cfg)
    monkeypatch.setattr(cli, "_model_report", forbidden)
    monkeypatch.setattr(cli, "_models_report", forbidden)
    monkeypatch.setattr(cli, "_postgres_report", ok)
    monkeypatch.setattr(cli, "_reminders_report", ok)
    monkeypatch.setattr(cli, "_notes_index_report", ok)
    monkeypatch.setattr(cli, "_outbox_report", ok)
    monkeypatch.setattr(cli, "_langfuse_report", ok)
    monkeypatch.setattr(cli, "_turns_report", ok)
    assert await cli._cmd_doctor(as_json=True, quick=True) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(report["checks"]) == {
        "postgres",
        "redis",
        "reminders",
        "notes_index",
        "outbox",
        "turns",
        "langfuse",
    }, report["checks"]
    assert report["checks"]["redis"]["backend"] == "memory"


async def test_reminders_probe_reports_a_missing_table_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Невыкатанная миграция 0004 — это «напоминаний нет», а не «бот болен»: HEALTHCHECK не роняем.

    Иначе из-за неиспользуемой функции контейнер стал бы «unhealthy», и через неделю на это
    перестали бы смотреть — ровно так же, как на вечно падающий CI.
    """
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "_scalar", _FakeSession({"to_regclass": False}))
    report = await cli._reminders_report(cfg)
    assert report["ok"] is True, "проба обязана оставаться справочной"
    assert report["state"] == "нет таблицы"
    assert "0004" in report["hint"]


async def test_reminders_probe_shows_stuck_rows_and_the_way_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k", reminders_batch=7)  # type: ignore[call-arg]
    fake = _FakeSession(
        {"to_regclass": True, "IN ('scheduled', 'sending')": 3, "due_at <= now()": 2}
    )
    monkeypatch.setattr(cli, "_scalar", fake)
    report = await cli._reminders_report(cfg)
    assert report["live"] == 3 and report["overdue"] == 2
    assert "тик ≤ 7" in report["note"]
    assert "aegis-reminders.timer" in report["hint"], "просроченное обязано вести к таймеру"

    quiet = _FakeSession(
        {"to_regclass": True, "IN ('scheduled', 'sending')": 1, "due_at <= now()": 0}
    )
    monkeypatch.setattr(cli, "_scalar", quiet)
    report = await cli._reminders_report(cfg)
    assert "hint" not in report, "пустое расписание не поводом что-то проверять"


async def test_reminders_probe_survives_a_dead_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]

    async def boom(sql: str) -> Any:
        raise OSError("Connection refused")

    monkeypatch.setattr(cli, "_scalar", boom)
    report = await cli._reminders_report(cfg)
    assert report["ok"] is True and "не проверялось" in report["note"]
    assert "postgres" in report["hint"]


async def test_reminders_probe_says_when_the_feature_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None, _env_prefix="T_", glm_api_key="k", reminders_enabled=False
    )
    monkeypatch.setattr(cli, "_scalar", _FakeSession({"to_regclass": True}))
    report = await cli._reminders_report(cfg)
    assert report["state"] == "выключено" and "REMINDERS_ENABLED" in report["note"]


async def test_notes_index_probe_names_the_queue_and_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отстающая индексация — это `note` + `hint`, а не `ok=false`.

    Пока заметки ищутся по тексту, бот здоров; «162 заметки ждут эмбеддинга» должно читаться как
    очередь, а не как падение, иначе HEALTHCHECK снова станет шумом.
    """
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr(
        cli,
        "_scalar",
        _FakeSession(
            {
                "to_regclass": True,
                "WHERE embedding IS NULL": 162,
                "SELECT count(*) FROM knowledge.notes": 400,
            }
        ),
    )
    report = await cli._notes_index_report(cfg)
    assert report["ok"] is True
    assert report["pending"] == 162 and report["total"] == 400
    assert "162 из 400" in report["note"]
    assert "aegis index notes" in report["hint"]


async def test_notes_index_probe_when_everything_is_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr(
        cli,
        "_scalar",
        _FakeSession({"to_regclass": True, "WHERE embedding IS NULL": 0, "count(*)": 12}),
    )
    report = await cli._notes_index_report(cfg)
    assert report["pending"] == 0
    assert "всё" in report["note"] and "hint" not in report


async def test_notes_index_probe_survives_missing_schema_and_dead_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "_scalar", _FakeSession({"to_regclass": False}))
    report = await cli._notes_index_report(cfg)
    assert report["ok"] is True and report["state"] == "нет таблицы"
    assert "0001" in report["hint"]

    async def boom(sql: str) -> Any:
        raise OSError("[Errno 111] Connect call failed")

    monkeypatch.setattr(cli, "_scalar", boom)
    report = await cli._notes_index_report(cfg)
    assert report["ok"] is True and "не проверялось" in report["note"]


async def test_outbox_probe_shows_the_queue_and_the_way_to_fix_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Очередь outbox без NATS — это «копится и ждёт», а не болезнь: события в таблице целы."""
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr(
        cli,
        "_scalar",
        # порядок игл в _FakeSession значителен: совпадает первое подошедшее, поэтому более
        # специфичный «attempts >= :n» идёт раньше общего «published_at IS NULL»
        _FakeSession({"to_regclass": True, "attempts >= :n": 0, "published_at IS NULL": 4312}),
    )
    report = await cli._outbox_report(cfg)
    assert report["ok"] is True
    assert report["pending"] == 4312 and report["relay"] == "выключен"
    assert "OUTBOX_RELAY_ENABLED=true" in report["hint"]
    assert "4312 ждут публикации" in report["note"]


async def test_outbox_probe_calls_out_exhausted_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None, _env_prefix="T_", glm_api_key="k", outbox_relay_enabled=True
    )
    monkeypatch.setattr(
        cli,
        "_scalar",
        _FakeSession({"to_regclass": True, "attempts >= :n": 5, "published_at IS NULL": 7}),
    )
    report = await cli._outbox_report(cfg)
    assert report["stuck"] == 5
    assert "aegis outbox tick" in report["hint"] and "attempts = 0" in report["hint"]


async def test_langfuse_probe_stays_quiet_while_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Выключенный экспорт — не болезнь: журнал на месте, терять при витрине нечего."""
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]

    async def forbidden(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise AssertionError("выключенный экспорт не имеет права ходить в базу")

    monkeypatch.setattr(cli, "_scalar", forbidden)
    report = await cli._langfuse_report(cfg)
    assert set(report) == {"ok", "enabled", "note"}, "выключенная проба не должна ничего читать"
    assert report["ok"] is True and report["enabled"] is False
    assert "выключен" in report["note"]


async def test_langfuse_probe_counts_the_window_and_asks_for_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        langfuse_enabled=True,
        langfuse_host="http://langfuse:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    monkeypatch.setattr(cli, "_scalar", _FakeSession({"decision_records": 12}))
    report = await cli._langfuse_report(cfg)
    assert report["ok"] is True and report["records"] == 12 and report["window_h"] == 24
    assert "aegis export langfuse" in report["note"]

    monkeypatch.setattr(cfg, "langfuse_public_key", "")
    half = await cli._langfuse_report(cfg)
    assert half["ok"] is False and "PUBLIC_KEY" in half["error"] and "ключей" in half["hint"]


async def test_langfuse_probe_survives_a_dead_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Битую базу уже показывает проба postgres: дублировать её отказ в langfuse — значит учить
    владельца считать, сколько раз ему пожаловались на одну и ту же причину.
    """
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None,
        _env_prefix="T_",
        glm_api_key="k",
        langfuse_enabled=True,
        langfuse_host="http://langfuse:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )

    async def broken(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "_scalar", broken)
    report = await cli._langfuse_report(cfg)
    assert report["ok"] is True and "не прочитан" in report["note"]
    assert "langfuse:3000" in report["host"]


async def test_outbox_probe_survives_missing_table_and_dead_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis import cli
    from aegis.platform.config import Settings

    cfg = Settings(_env_file=None, _env_prefix="T_", glm_api_key="k")  # type: ignore[call-arg]
    monkeypatch.setattr(cli, "_scalar", _FakeSession({"to_regclass": False}))
    report = await cli._outbox_report(cfg)
    assert report["ok"] is True and report["state"] == "нет таблицы"

    async def boom(sql: str, **params: Any) -> Any:
        raise OSError("[Errno 111] Connect call failed")

    monkeypatch.setattr(cli, "_scalar", boom)
    report = await cli._outbox_report(cfg)
    assert report["ok"] is True and "не проверялось" in report["note"]


async def test_live_probe_cannot_hang_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Висящее соединение провайдера не превращает doctor в «ничего не выводится»."""
    import asyncio
    from types import SimpleNamespace

    from aegis import cli
    from aegis.platform.config import Settings

    class HangingGateway:
        async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            await asyncio.sleep(30)
            raise AssertionError("не должно быть достигнуто")

        async def embed(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            await asyncio.sleep(30)
            raise AssertionError("не должно быть достигнуто")

    monkeypatch.setattr(cli, "_PROBE_TIMEOUT_S", 0.05)
    cfg = Settings(_env_file=None, glm_api_key="k", llm_timeout_s=30)
    report = await cli._models_report(SimpleNamespace(gateway=HangingGateway()), cfg)
    assert report["ok"] is False
    assert all(e["error"].startswith("TimeoutError") for e in report["roles"].values())
    assert "--quick" in report["hint"]

    single = await cli._model_report(SimpleNamespace(gateway=HangingGateway()), cfg)
    assert single["ok"] is False and "TimeoutError" in single["error"]


async def test_model_hint_names_key_provider_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 от чужого endpoint: человек должен получить «ключ от z.ai, запрос на zenmux.ai»."""
    from types import SimpleNamespace

    from aegis import cli
    from aegis.platform.config import Settings
    from aegis.platform.gateway.client import ModelUnavailable

    class Gateway:
        async def chat(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            raise ModelUnavailable("нет", cause="AuthenticationError: Error code: 401")

        def auth_hint(self) -> str:
            return "ключ с префиксом sk-ai-v1-… выдан z.ai, а запрос уходит на zenmux.ai"

    cfg = Settings(_env_file=None, glm_api_key="k")
    report = await cli._model_report(SimpleNamespace(gateway=Gateway()), cfg)
    assert "выдан z.ai" in report["hint"]


async def test_search_probe_reports_every_engine_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`doctor` обязан показывать то же, что бот: не «поиск сломан», а кто именно и почему."""
    from aegis import cli
    from aegis.web.search import EngineReport, SearchHit, SearchOutcome

    async def fake_outcome(
        self: object, query: str, count: int = 5, **_kw: object
    ) -> SearchOutcome:
        return SearchOutcome(
            query=query,
            hits=[SearchHit(title="x", url="https://a.example/1", snippet="…")],
            engines=[
                EngineReport(engine="searxng", status="empty", note="не ответили: google→timeout"),
                EngineReport(engine="zai", status="ok", hits=1),
            ],
            verdict="ok",
        )

    from aegis.web.search import WebSearch

    monkeypatch.setattr(WebSearch, "outcome", fake_outcome)
    report = await cli._search_report(_doctor_cfg())
    assert report["ok"] is True
    assert any("google→timeout" in line for line in report["engines"])
    assert "zai: 1" in " | ".join(report["engines"])


async def test_rates_probe_says_which_source_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis import cli
    from aegis.web.rates import RateAnswer, RateQuestion

    async def dead(question: RateQuestion, **_kw: object) -> RateAnswer:
        return RateAnswer(
            question=question,
            verdict="unavailable",
            causes=["privatbank: ConnectError"],
            fetched_at="2026-09-05T01:13:00+03:00",
        )

    monkeypatch.setattr("aegis.web.rates.fetch_rates", dead)
    report = await cli._rates_report(_doctor_cfg())
    assert report["ok"] is False and "privatbank" in report["hint"]


async def test_bounded_probe_never_hangs_the_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Висящий внешний сервис не имеет права превращать doctor в «ничего не выводится»."""
    import asyncio

    from aegis import cli

    async def forever() -> dict[str, object]:
        await asyncio.sleep(5)
        return {"ok": True}

    monkeypatch.setattr(cli, "_PROBE_TIMEOUT_S", 0.01)
    report = await cli._bounded_probe("search", forever)
    assert report["ok"] is False and "TimeoutError" in report["error"]


def _doctor_cfg() -> Any:
    from aegis.platform.config import Settings

    return Settings(_env_file=None, _env_prefix="T_", search_engines="searxng,zai")


# ------------------------------------------------------------------ проба ходов (F1)


@pytest.mark.asyncio
async def test_turns_report_flags_dead_claims(monkeypatch) -> None:
    from aegis import cli  # noqa: PLC0415 — локальный импорт, как в остальных пробах файла

    async def fake_scalar(sql, params=None):
        if "to_regclass" in sql:
            return True
        if "turn_claims" in sql:
            return 0  # ни одной просроченной аренды
        return 2 if "attempts >= 3" in sql else 1  # 2 мёртвых, 1 на автомате

    monkeypatch.setattr(cli, "_scalar", fake_scalar)
    report = await cli._turns_report()
    assert report["ok"] is False and "drain" in report["hint"], (
        "исчерпавшие попытки claimed — единственное состояние, где сообщение не дойдёт само"
    )
    assert report["queue_dead"] == 2


@pytest.mark.asyncio
async def test_turns_report_is_green_but_tells(monkeypatch) -> None:
    from aegis import cli  # noqa: PLC0415

    async def fake_scalar(sql, params=None):
        if "to_regclass" in sql:
            return True
        if "turn_claims" in sql:
            return 1  # просроченная аренда: begin починит сам
        return 3 if "attempts < 3" in sql else 0

    monkeypatch.setattr(cli, "_scalar", fake_scalar)
    report = await cli._turns_report()
    assert report["ok"] is True and report["note"] == "3 claimed переберутся сами через 60s"


@pytest.mark.asyncio
async def test_turns_report_without_migration_is_note_not_fault(monkeypatch) -> None:
    from aegis import cli  # noqa: PLC0415

    async def fake_scalar(sql, params=None):
        assert "to_regclass" in sql  # дальше не ходим
        return False

    monkeypatch.setattr(cli, "_scalar", fake_scalar)
    report = await cli._turns_report()
    assert report["ok"] is True and report["state"] == "нет таблиц ходов"
