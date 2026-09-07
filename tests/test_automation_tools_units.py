"""Инструменты и экраны автоматизации офлайн: моки хранилищ, настоящие тексты ответов.

Здесь проверяется договорённость «модель видит то, что ей можно»: ни одного секрета в
выводе, отказ без БД — словами, отказ при незапарсенном сроке — тоже словами, а кнопка
менё зовёт ровно тот метод, который обещан.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import aegis.agents.tools.automation as automation_tools
import aegis.agents.tools.jobs as jobs_tools
import aegis.agents.tools.tasks as tasks_tools
from aegis.agents.tools.registry import ToolContext
from aegis.automation.execute import ActionResult
from aegis.automation.store import EndpointRow, HookRow
from aegis.interaction.telegram import menu as menu_mod
from aegis.planning.jobs import JobRow
from aegis.planning.tasks import TaskRow
from aegis.platform.config import Settings, override_settings

NOW = datetime.now(UTC)


def ctx() -> ToolContext:
    return ToolContext(trace_id="t-1", owner_id=1, services=None)


def _task(**kw: Any) -> TaskRow:
    base: dict[str, Any] = dict(
        id="aaaa1111-2222-3333-4444-555566667777",
        owner_id=1,
        title="починить сарай",
        due_at=NOW - timedelta(hours=1),
        remind_on_due=True,
    )
    base.update(kw)
    return TaskRow(**base)


def _job(**kw: Any) -> JobRow:
    base: dict[str, Any] = dict(
        id="bbbb1111-2222-3333-4444-555566667777",
        owner_id=1,
        title="утренняя сводка",
        prompt="собери новости",
        repeat="daily",
        at_time="08:00",
        next_run=NOW + timedelta(hours=4),
    )
    base.update(kw)
    return JobRow(**base)


class FakeTaskStore:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result = result or _task()
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def add(self, **kw: Any) -> TaskRow:
        self.calls.append(("add", kw))
        if self.exc:
            raise self.exc
        return self.result

    async def update(self, **kw: Any) -> TaskRow | None:
        self.calls.append(("update", kw))
        return None if self.exc else self.result

    async def list_tasks(self, **kw: Any) -> list[TaskRow]:
        self.calls.append(("list", kw))
        if self.exc:
            raise self.exc
        return [self.result]

    async def stats(self, **kw: Any) -> dict[str, int]:
        return {"open": 1, "doing": 0, "overdue": 1, "done_week": 2}


async def test_add_task_passes_verbatim_phrase_to_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTaskStore()
    monkeypatch.setattr(tasks_tools, "SqlTaskStore", lambda: fake)
    out = await tasks_tools.add_task(
        tasks_tools.AddTaskArgs(title="починить сарай", due="завтра в 10:00"), ctx()
    )
    assert "aaaa1111" in out and "добавлена" in out
    action, kw = fake.calls[0]
    assert action == "add" and kw["remind_on_due"] is True
    assert kw["due_at"] is not None and kw["due_at"] > NOW


async def test_add_task_unparsable_due_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTaskStore()
    monkeypatch.setattr(tasks_tools, "SqlTaskStore", lambda: fake)
    out = await tasks_tools.add_task(
        tasks_tools.AddTaskArgs(title="x y", due="когда-нибудь потом"), ctx()
    )
    assert out.startswith("Не ставлю") and not fake.calls


async def test_add_task_no_db_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTaskStore(exc=RuntimeError("connection refused"))
    monkeypatch.setattr(tasks_tools, "SqlTaskStore", lambda: fake)
    out = await tasks_tools.add_task(tasks_tools.AddTaskArgs(title="x y"), ctx())
    assert "База недоступна" in out and "connection refused" in out


async def test_list_tasks_marks_overdue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_tools, "SqlTaskStore", lambda: FakeTaskStore())
    out = await tasks_tools.list_tasks(tasks_tools.ListTasksArgs(), ctx())
    assert "просрочено" in out and "☐ aaaa1111" in out


class FakeJobStore:
    def __init__(self, jobs: Any = (), exc: Exception | None = None) -> None:
        self.jobs = list(jobs)
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def add(self, **kw: Any) -> str:
        self.calls.append(("add", kw))
        if self.exc:
            raise self.exc
        return "cccc1111-0000-0000-0000-000000000000"

    async def list_jobs(self, **kw: Any) -> list[JobRow]:
        return self.jobs

    async def trigger_now(self, **kw: Any) -> str | None:
        self.calls.append(("trigger", kw))
        return "«утренняя сводка» в очереди — ближайший тик"

    async def set_status(self, **kw: Any) -> str | None:
        self.calls.append(("status", kw))
        return "пауза"

    async def drop(self, **kw: Any) -> bool:
        return True


async def test_add_job_once_requires_moment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeJobStore()
    monkeypatch.setattr(jobs_tools, "SqlJobStore", lambda: fake)
    out = await jobs_tools.add_job(
        jobs_tools.AddJobArgs(title="разовый прогон", prompt="сделай X", repeat="once"), ctx()
    )
    assert "once" in out and "at" in out and not fake.calls


async def test_add_job_daily_formats_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeJobStore()
    monkeypatch.setattr(jobs_tools, "SqlJobStore", lambda: fake)
    with override_settings(automation_enabled=True):
        out = await jobs_tools.add_job(
            jobs_tools.AddJobArgs(
                title="утренняя сводка", prompt="собери новости", repeat="daily", at_time="07:30"
            ),
            ctx(),
        )
    assert "daily, в 07:30" in out
    _, kw = fake.calls[0]
    assert kw["first_run"] is None and kw["tz"] is not None


async def test_list_jobs_shows_error_count(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job(status="paused", fail_count=5, last_error="timeout после 15 с")
    monkeypatch.setattr(jobs_tools, "SqlJobStore", lambda: FakeJobStore([job]))
    out = await jobs_tools.list_jobs(jobs_tools.NoArgs(), ctx())
    assert "⏸" in out and "ошибок 5" in out and "timeout" in out


class FakeAutoStore:
    def __init__(self, *, row: Any = None, exc: Exception | None = None) -> None:
        self.row = row or EndpointRow(
            id="d1",
            owner_id=1,
            name="notify",
            method="POST",
            url="http://93.184.216.34/x",
            secret_names=("TOKEN",),
        )
        self.exc = exc
        self.runs: list[dict[str, Any]] = []

    async def endpoint_for_run(self, **kw: Any) -> tuple[EndpointRow, dict[str, str]]:
        if self.exc:
            raise self.exc
        return self.row, {"TOKEN": "sup3r"}

    async def record_run(self, **kw: Any) -> None:
        self.runs.append(kw)

    async def list_endpoints(self, **kw: Any) -> list[EndpointRow]:
        return [self.row]

    async def list_hooks(self, **kw: Any) -> list[HookRow]:
        return [
            HookRow(
                id="h1",
                owner_id=1,
                name="ci",
                policy="notify",
                rate_per_min=5,
                enabled=True,
                fires=3,
                last_fire=None,
            )
        ]

    async def add_hook(self, **kw: Any) -> tuple[HookRow, str]:
        return (
            HookRow(
                id="h1",
                owner_id=1,
                name=kw["name"],
                policy=kw.get("policy", "notify"),
                rate_per_min=5,
                enabled=True,
                fires=0,
                last_fire=None,
            ),
            "SECRET-TOKEN",
        )


async def test_run_action_success_text_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAutoStore()

    async def ok_result(row: Any, secrets: Any, **kw: Any) -> ActionResult:
        assert secrets == {"TOKEN": "sup3r"}
        return ActionResult(ok=True, status=204, ms=41, digest="HTTP 204 · 41 мс · пустое тело")

    monkeypatch.setattr(automation_tools, "SqlAutomationStore", lambda: fake)
    monkeypatch.setattr(automation_tools, "run_endpoint", ok_result)
    with override_settings(automation_enabled=True):
        out = await automation_tools.run_action(
            automation_tools.RunActionArgs(endpoint="notify"), ctx()
        )
    assert "sup3r" not in out and "Вызвал" in out
    assert fake.runs and fake.runs[0]["triggered_by"] == "model"


async def test_run_action_error_records_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis.automation.execute import ActionError

    fake = FakeAutoStore()

    async def boom(*a: Any, **kw: Any) -> Any:
        raise ActionError("не хватает переменных: {{msg}}")

    monkeypatch.setattr(automation_tools, "SqlAutomationStore", lambda: fake)
    monkeypatch.setattr(automation_tools, "run_endpoint", boom)
    with override_settings(automation_enabled=True):
        out = await automation_tools.run_action(
            automation_tools.RunActionArgs(endpoint="notify"), ctx()
        )
    assert out.startswith("Действие не выполнено")
    assert fake.runs and fake.runs[0]["ok"] is False


async def test_run_action_switch_off_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(automation_tools, "SqlAutomationStore", lambda: FakeAutoStore())
    with override_settings(automation_enabled=False):
        out = await automation_tools.run_action(
            automation_tools.RunActionArgs(endpoint="notify"), ctx()
        )
    assert "выключены" in out

    class Missing(FakeAutoStore):
        async def endpoint_for_run(self, **kw: Any) -> Any:
            raise KeyError("нет-такого")

    monkeypatch.setattr(automation_tools, "SqlAutomationStore", lambda: Missing())
    with override_settings(automation_enabled=True):
        out = await automation_tools.run_action(
            automation_tools.RunActionArgs(endpoint="нет-такого"), ctx()
        )
    assert "не найден" in out


async def test_webhook_add_hides_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(automation_tools, "SqlAutomationStore", lambda: FakeAutoStore())
    with override_settings(automation_enabled=True, hooks_enabled=False, hooks_port=8791):
        out = await automation_tools.webhook_add(automation_tools.WebhookAddArgs(name="cix"), ctx())
    assert "SECRET-TOKEN" not in out
    assert "/h/cix" in out and "aegis hook url cix" in out and "выключен" in out


async def test_endpoint_list_shows_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(automation_tools, "SqlAutomationStore", lambda: FakeAutoStore())
    out = await automation_tools.endpoint_list(automation_tools.NoArgs(), ctx())
    assert "notify" in out and "TOKEN" in out and "sup3r" not in out
    hooks = await automation_tools.webhook_list(automation_tools.NoArgs(), ctx())
    assert "ci" in hooks and "notify" in hooks


# ---------- экраны и действия меню ----------


def _deps(**kw: Any) -> menu_mod.MenuDeps:
    return menu_mod.MenuDeps(cfg=Settings(_env_file=None), **kw)


async def test_menu_tasks_screen_lists_with_buttons() -> None:
    deps = _deps(tasks=FakeTaskStore())
    text, kb = await menu_mod.perform_screen("tasks", 1, deps, can_control=True)
    assert "aaaa1111" in text and "горят" in text and "⏰" in text
    data = kb.inline_keyboard[0][0].callback_data or ""
    assert data.startswith("m:act:task-done:")


async def test_menu_automation_screen_renders_all_three_blocks() -> None:
    class Auto(FakeAutoStore):
        pass

    deps = _deps(jobs=FakeJobStore([_job()]), automation=Auto())
    text, _kb = await menu_mod.perform_screen("automation", 1, deps, can_control=True)
    assert "Прогоны" in text and "утренняя сводка" in text
    assert "Действия" in text and "notify" in text
    assert "Вебхуки" in text and "ci" in text
    assert "приёмник выключен" in text


async def test_menu_actions_call_stores() -> None:
    tasks = FakeTaskStore()
    jobs = FakeJobStore()
    deps = _deps(tasks=tasks, jobs=jobs)
    note, screen = await menu_mod.apply_action("task-done", menu_mod.b64enc("aaaa1111"), 1, deps)
    assert "Готово" in note and screen == "tasks" and tasks.calls[0][0] == "update"
    note, screen = await menu_mod.apply_action(
        "job-toggle", menu_mod.b64enc("cccc1111|pause"), 1, deps
    )
    assert note == "пауза" and screen == "automation"


async def test_menu_ep_run_records_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    auto = FakeAutoStore()

    async def ok_result(*a: Any, **kw: Any) -> ActionResult:
        return ActionResult(ok=False, status=503, ms=90, digest="HTTP 503 · 90 мс · недоступен")

    monkeypatch.setattr("aegis.automation.execute.run_endpoint", ok_result)
    deps = _deps(automation=auto)
    note, screen = await menu_mod.apply_action("ep-run", menu_mod.b64enc("d1"), 1, deps)
    assert screen == "automation" and "503" in note
    assert auto.runs[0]["triggered_by"] == "menu"


async def test_menu_task_store_down_shows_warning() -> None:
    deps = _deps(tasks=FakeTaskStore(exc=RuntimeError("down")))
    text, _kb = await menu_mod.perform_screen("tasks", 1, deps, can_control=False)
    assert "⚠️" in text
