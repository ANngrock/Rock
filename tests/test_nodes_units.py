"""Блок B (узлы) — юниты чистой логики: argv по ОС, идемпотентность, payload, диспетч, заметки.

Здесь нет ни БД, ни брокера — ровно то, что можно проверить без сети: «что именно выполнится
на Windows» и «как трактуются границы» должны читаться тестом, а не прогоном на живой машине.
"""

from datetime import UTC, datetime, timedelta

import pytest

from aegis.interaction.nodes.actions import (
    ExecutedRing,
    build_argv,
    execute,
)
from aegis.planning.nodes import (
    PairingCode,
    build_result_note,
    decide_dispatch,
    validate_payload,
)

# ---------- build_argv: каждая ОС своим способом, и никакого shell=True ----------


def test_build_argv_notify_per_os() -> None:
    assert build_argv("notify", {"text": "привет"}, os_name="linux") == [
        "notify-send",
        "Aegis",
        "привет",
    ]
    mac = build_argv("notify", {"text": "т"}, os_name="mac")
    assert mac is not None and mac[:2] == ["osascript", "-e"] and "display notification" in mac[2]
    win = build_argv("notify", {"text": "т"}, os_name="win")
    assert win is not None and win[0] == "powershell"
    assert "[System.Windows.Forms.MessageBox]::Show(" in win[3]


def test_build_argv_run_never_uses_shell_flag() -> None:
    lin = build_argv("run", {"command": "ls -la"}, os_name="linux")
    assert lin == ["/bin/bash", "-lc", "ls -la"]  # -lc — выбор владельца, не shell=True из кода
    win = build_argv("run", {"command": "dir"}, os_name="win")
    assert win is not None and win[:3] == ["powershell", "-NoProfile", "-Command"]


def test_build_argv_screenshot_tool_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name if name == "grim" else None)
    argv = build_argv("screenshot", {}, os_name="linux")
    assert argv is not None and argv[0] == "grim"
    assert "aegis-shot-" in argv[1] and argv[1].endswith(".png")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ValueError, match="grim"):
        build_argv("screenshot", {}, os_name="linux")
    assert build_argv("screenshot", {}, os_name="win") is None  # отдельно обработанный путь


def test_unknown_action_builds_nothing() -> None:
    assert build_argv("rm-rf", {}, os_name="linux") is None


# ---------- идемпотентность: доставка at-most-once, исполнение — ровно раз ----------


def test_executed_ring_dedupes_with_capacity() -> None:
    ring = ExecutedRing(capacity=3)
    assert ring.remember_if_new("a") is True
    assert ring.remember_if_new("a") is False  # повторная доставка тем же id — не второе исполнение
    for i in "bcd":
        ring.remember_if_new(i)
    assert ring.remember_if_new("a") is True  # «a» вытеснена кольцом — для узла это новая команда


# ---------- execute: живой подпроцесс, обрезка, честные ошибки ----------


def test_execute_run_echo_and_exit_code() -> None:
    out = execute({"action": "run", "payload": {"command": "echo привет"}})
    assert out["ok"] is True and out["result"].strip() == "привет"
    bad = execute({"action": "run", "payload": {"command": "exit 3"}})
    assert bad["ok"] is False and "exit=3" in str(bad["error"])


def test_execute_truncates_long_output() -> None:
    out = execute({"action": "run", "payload": {"command": "printf 'x%.0s' $(seq 1 9000)"}})
    assert out["ok"] is True
    assert "[срезано]" in out["result"] and len(out["result"]) < 9000


def test_execute_notify_without_binary_reports_honestly() -> None:
    # в песочнице notify-send нет: ошибка должна быть текстом, а не молчанием или падением
    out = execute({"action": "notify", "payload": {"text": "т"}})
    assert out["ok"] is False
    assert "запуск не удался" in str(out["error"]) or "не найден" in str(out["error"])


def test_execute_screenshot_missing_tool_is_error_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    out = execute({"action": "screenshot", "payload": {}})
    assert out["ok"] is False and "grim" in str(out["error"])


def test_execute_unknown_action() -> None:
    out = execute({"action": "wat", "payload": {}})
    assert out["ok"] is False and "неизвестное действие" in str(out["error"])


# ---------- валидация payload ----------


def test_validate_payload_normalizes() -> None:
    assert validate_payload("system", {}) == {}
    assert validate_payload("notify", {"text": "  привет   мир  ", "junk": 1}) == {
        "text": "привет мир"
    }
    assert validate_payload("screenshot", {"junk": 1}) == {"display": "auto"}
    assert validate_payload("run", {"command": "ls"}) == {"command": "ls", "timeout_s": 60}
    long_text = validate_payload("notify", {"text": "x" * 5000})
    assert len(long_text["text"]) == 500  # текст обрезается, а не отвергается: человек печатал


@pytest.mark.parametrize(
    ("action", "payload", "match"),
    [
        ("rm", {}, "действие обязано быть одним"),
        ("notify", {"text": "   "}, "пустой текст"),
        ("run", {"command": "  "}, "пустая команда"),
        ("run", {"command": "x" * 2001}, "длиннее 2000"),
        ("run", {"command": "ls", "timeout_s": 3600}, "timeout_s"),
    ],
)
def test_validate_payload_rejects(action: str, payload: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_payload(action, payload)


def test_pairing_code_roundtrip() -> None:
    code = PairingCode.generate()
    assert len(code) == 6 and code.isdigit()
    assert PairingCode.verify(code, PairingCode.hash(code)) is True
    assert PairingCode.verify("000000", PairingCode.hash(code)) is False
    assert PairingCode.verify(code, None) is False
    with pytest.raises(ValueError, match="6 цифр"):
        PairingCode.hash("abc123")


# ---------- decide_dispatch: таблица решений — без БД ----------

_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _d(
    *,
    status: str = "queued",
    node_status: str = "paired",
    last_seen: datetime | None = _NOW,
    dispatched_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> str:
    return decide_dispatch(
        now=_NOW,
        status=status,
        node_status=node_status,
        node_last_seen=last_seen,
        dispatched_at=dispatched_at,
        expires_at=expires_at or (_NOW + timedelta(minutes=10)),
    )


def test_decide_dispatch_matrix() -> None:
    assert _d() == "send"
    assert _d(last_seen=None) == "parked"  # узел без вестей — ждём, не считаем ошибкой
    assert _d(last_seen=_NOW - timedelta(seconds=89)) == "send"  # грань 90s включительно
    assert _d(last_seen=_NOW - timedelta(seconds=91)) == "parked"
    assert _d(node_status="revoked") == "parked"
    assert _d(node_status="pending") == "parked"
    assert _d(expires_at=_NOW) == "expired"  # «пора» на самой границе — уже просрочено
    assert _d(status="done") == "settled"
    assert _d(status="cancelled") == "settled"
    assert _d(status="dispatched") == "wait"  # ждём ответ: перепоставлять run нельзя
    assert (
        _d(status="dispatched", dispatched_at=_NOW - timedelta(minutes=6)) == "lost"
    )  # не «в очередь ещё раз», а «неизвестно, могла исполниться»


def test_expired_beats_parked() -> None:
    # узел офлайн И срок вышел: иток — expired, команда закрыта, а не «вечно parked»
    assert _d(last_seen=None, expires_at=_NOW - timedelta(seconds=1)) == "expired"


# ---------- заметка владельцу ----------


def test_build_result_note_forms() -> None:
    assert build_result_note("system", "linux|ok", None) == "💻 система:\nlinux|ok"
    assert build_result_note("notify", None, None) == "💻 уведомление доставлено: ok"
    assert build_result_note("notify", "ok", None).startswith("💻 уведомление доставлено")
    assert build_result_note("run", None, "exit=1") == "💻 команда: ! exit=1"
    big = build_result_note("screenshot", "п" * 4000, None)
    assert "[срезано]" in big and len(big) < 1200
    assert build_result_note("run", " " * 10, None) == "💻 команда: ok"
