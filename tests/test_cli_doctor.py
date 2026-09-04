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

    async def __call__(self, sql: str) -> Any:
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
