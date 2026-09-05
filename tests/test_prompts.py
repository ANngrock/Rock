"""Промпты как файлы: версионность, frontmatter, строгий рендер.

Тесты идут и по реальному каталогу пакета (``src/aegis/prompts``): он попадает в сборку как
package-data, и «в контейнере файла нет» должно ловиться здесь, а не в проде на первом ``/replay``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.platform import prompts
from aegis.platform.prompts import Prompt, PromptNotFound, index, load, prompts_dir


@pytest.fixture(autouse=True)
def _no_cache() -> object:
    prompts.clear_cache()
    yield
    prompts.clear_cache()


def _write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_package_prompt_loads_and_is_described(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AEGIS_PROMPTS_DIR", raising=False)
    prompt = load("repro/judge")
    assert prompt.id == "repro/judge"
    assert prompt.version
    assert prompt.model_role in {"fast", "brain"}
    assert prompt.temperature == pytest.approx(0.0)
    assert prompt.thinking is False
    assert prompt.schema == "ReplayJudgement"
    assert prompt.placeholders() == {"original", "replay"}
    # хэш тела обязан меняться вместе с текстом — иначе «версия» ничего не доказывает
    assert len(prompt.sha256) == 64


def test_every_prompt_catalog_is_a_package_and_shipped(tmp_path: Path) -> None:
    """Каталог промптов без `__init__.py` живёт в editable-установке и исчезает из wheel.

    Именно так «в контейнере нет файла судьи» и появляется: тесты на исходниках зелёные, образ —
    без .md. Проверка дешёвая и ловит класс, а не один случай: каждый каталог с промптами обязан
    быть пакетом, а package-data — объявлять `**/*.md`.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "aegis" / "prompts"
    markdown = sorted(root.rglob("*.md"))
    assert markdown, "в пакете нет ни одного промпта — что-то сломалось в путях"
    for path in markdown:
        assert (path.parent / "__init__.py").exists(), f"{path}: каталог не пакет → wheel без файла"
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert '"aegis.prompts" = ["**/*.md"]' in pyproject.read_text(encoding="utf-8")


def test_judge_prompt_cannot_demand_what_replay_never_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Версия 1.0 требовала «одинаковый набор вызванных инструментов».

    Replay не исполняет инструменты повторно: их записанные результаты уже в контексте, поэтому
    такой критерий означал бы «не эквивалентно» на любом ходе — вау-эффект от `/replay` сменился бы
    вечным красным флагом. Правки в критерии сравнения живут в файле промпта (и его версии), а не в
    коде судьи, — иначе «что именно считалось эквивалентным» нельзя было бы восстановить по журналу.
    """
    monkeypatch.delenv("AEGIS_PROMPTS_DIR", raising=False)
    text = load("repro/judge").text
    assert "набор вызванных инструментов" not in text
    assert "НЕ перевыполняет" in text
    assert "сравнивай только тексты" in text


def test_old_prompt_version_resolves_by_the_sha_in_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ход записан с `sha256` промпта v1.0 — значит, v1.0 обязана оставаться загружаемой."""
    monkeypatch.delenv("AEGIS_PROMPTS_DIR", raising=False)
    versions = index()["repro/judge"]
    assert versions == ["1.0", "1.1"]
    old = load("repro/judge", version=versions[0])
    current = load("repro/judge")
    assert old.sha256 != current.sha256
    with pytest.raises(PromptNotFound, match="версии 9.9 промпта 'repro/judge' нет"):
        load("repro/judge", version="9.9")


def test_env_override_switches_the_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "x", "y.v1.0.md", "---\nid: x/y\n---\nТело\n")
    monkeypatch.setenv("AEGIS_PROMPTS_DIR", str(tmp_path))
    assert prompts_dir() == tmp_path
    assert load("x/y").text == "Тело"
    with pytest.raises(PromptNotFound):
        load("repro/judge")


def test_missing_prompt_is_an_error_not_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AEGIS_PROMPTS_DIR", str(tmp_path))
    with pytest.raises(PromptNotFound, match="ожидаем файл вида"):
        load("нет/такого")


def test_newest_version_wins_by_number_not_by_string(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write(root / "p", "a.v1.9.md", "девять")
    _write(root / "p", "a.v2.0.md", "два")
    _write(root / "p", "a.v10.0.md", "десять")
    assert load("p/a", root=root).text == "десять"


def test_explicit_version_is_exact(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write(root / "p", "a.v1.0.md", "старый")
    _write(root / "p", "a.v2.0.md", "новый")
    assert load("p/a", version="1.0", root=root).text == "старый"
    with pytest.raises(PromptNotFound, match="есть: "):
        load("p/a", version="3.0", root=root)


def test_render_is_strict_by_default(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write(root / "p", "a.v1.0.md", "Привет, {{name}}! План: {{plan}}")
    prompt = load("p/a", root=root)
    assert prompt.render(name="А", plan="Б") == "Привет, А! План: Б"
    with pytest.raises(KeyError, match="не подставлены: plan"):
        prompt.render(name="А")
    assert "{{plan}}" in prompt.render(name="А", strict=False)


def test_frontmatter_variants(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write(
        root / "p",
        "a.v0.1.md",
        "---\n"
        'id: "custom/id"\n'
        "version: '7.7'\n"
        "thinking: да\n"
        "temperature: 0.2\n"
        "# комментарий\n"
        "  nested: skipped\n"
        "---\n"
        "Текст\n",
    )
    prompt = load("p/a", root=root)
    assert (prompt.id, prompt.version, prompt.thinking, prompt.temperature) == (
        "custom/id",
        "7.7",
        True,
        pytest.approx(0.2),
    )
    assert prompt.model_role == "brain"  # дефолт, если в frontmatter не указан
    assert prompt.schema is None


def test_file_name_version_is_the_fallback(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    path = _write(root / "mod", "name.v3.2.md", "без frontmatter")
    prompt = load("mod/name", root=root)
    assert prompt.version == "3.2" and prompt.id == "mod/name"
    assert prompt.path is not None and Path(prompt.path).name == path.name


def test_sha256_tracks_the_body(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write(root / "p", "a.v1.0.md", "раз")
    first = load("p/a", root=root)
    prompts.clear_cache()
    _write(root / "p", "a.v1.0.md", "два")
    second = load("p/a", root=root)
    assert first.sha256 != second.sha256
    assert first.version == second.version  # версия та же — и это как раз видно по хэшу


def test_index_lists_catalog(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    _write(root / "a", "one.v1.0.md", "x")
    _write(root / "b", "two.v1.1.md", "y")
    _write(root / "b", "_draft.md", "заметка, не промпт")
    assert index(root=root) == {"a/one": ["1.0"], "b/two": ["1.1"]}


def test_real_catalog_is_indexable() -> None:
    """Файлы обязаны доезжать до сборки: молча подставить «дефолт из кода» = потерять версию."""
    found = index()
    assert "repro/judge" in found, f"в пакете нет промптов: {found}"
    assert all(versions for versions in found.values())


def test_as_record_shape() -> None:
    record = Prompt(
        id="a/b", version="1.0", text="t", sha256="x" * 64, model_role="fast", thinking=False
    ).as_record()
    assert record == {"id": "a/b", "version": "1.0", "sha256": "x" * 64}
