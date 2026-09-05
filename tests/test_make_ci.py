"""`make ci` обязан совпадать со сборкой CI, иначе «локально зелёный» ничего не значит.

Смысл не в том, чтобы задублировать YAML в тесте, а в том, чтобы поймать конкретную неприятность:
кто-то добавляет проверку в workflow (или в `make lint`) — а второй список уезжает вперёд, и
расхождение всплывает через месяц как «в CI падает, у меня проходит». Тест сверяет наборы
инструментов, а не форматирование.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")


def _workflow_commands() -> list[str]:
    """Строки `run:` из workflow, склеенные по многострочным блокам."""
    text = WORKFLOW.read_text(encoding="utf-8")
    commands: list[str] = []
    for block in re.findall(r"run:\s*\|\n((?:[ \t]+.*\n?)+)", text):
        commands.append(" ".join(line.strip() for line in block.splitlines() if line.strip()))
    commands += [m.strip() for m in re.findall(r"run:\s*(?!\|)(.+)", text)]
    return commands


def test_ci_target_exists_and_is_documented() -> None:
    assert re.search(r"^ci: ## .+", MAKEFILE, re.M), "у `ci` должен быть текст для `make help`"


def test_every_tool_ci_runs_is_reachable_from_make_ci() -> None:
    """Каждый инструмент из workflow должен вызываться и из `make ci` (в т.ч. через check/lint)."""
    joined = " ".join(_workflow_commands())
    needed = {
        "ruff check": "ruff check",
        "ruff format --check": "ruff format --check",
        "синтаксис shell-скриптов": "bash -n",
        "mypy": "mypy src",
        "import-linter": "lint-imports",
        "юниты": 'pytest -q -m "not integration"',
        "golden-наборы": "evals/run_golden.py",
        "хэши журнала": "evals/run_repro.py",
        "интеграции": "pytest -q -m integration",
        "сборка образа": "docker build",
    }
    tools_in_ci = re.findall(
        r"\b(?:ruff|bash|mypy|lint-imports|pytest|python|docker|alembic)\b", joined
    )
    for tool in sorted(set(tools_in_ci)):
        assert tool in MAKEFILE, f"`{tool}` есть в CI и не вызывается ни одной целью Makefile"
    missing = [name for name, needle in needed.items() if needle not in MAKEFILE]
    assert not missing, f"в Makefile нет этих шлюзов CI: {missing}"


def test_make_ci_skips_optional_gates_instead_of_lying() -> None:
    """Без Postgres и без Docker цель обязана сказать «пропущено», а не «успех».

    Иначе `make ci` на машине без докера превращается в зелёную пустоту — ровно тот режим, из-за
    которого к CI перестают относиться как к проверке.
    """
    body = MAKEFILE[MAKEFILE.index("\nci:") :]
    body = body[: body.index("\n\n")]
    assert "пропущено: нет Postgres" in body
    assert "пропущено: нет docker" in body
    assert body.count('echo "  пропущено') == 2
