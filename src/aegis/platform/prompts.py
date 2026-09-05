"""Промпты как файлы: `prompts/<модуль>/<имя>.v<мажор>.<минор>.md` + YAML-frontmatter.

Почему файлы, а не строки в коде: промпт — продукт, его правят чаще кода. Правка обязана быть
видимой в диффе, проверяемой в CI и, главное, идентифицируемой постфактум. Когда через месяц
владелец спросит «почему ты тогда так ответил», ответ должен опираться на текст правил конкретной
версии, а не на «в git, кажется, был какой-то sys-v0.3.0».

Отсюда контракт: у промпта есть `id`, `version` и `sha256` тела — все три попадают в decision record
(M1), и «версия промпта» перестаёт быть строкой в логе, становясь проверяемым байтом.

Frontmatter разбирается подмножеством YAML без внешних зависимостей: только `key: value` на верхнем
уровне. Понадобится вложенность — это сигнал перейти на pyyaml, а не дописывать парсер.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aegis.platform.canonical import sha256_hex

__all__ = ["Prompt", "PromptNotFound", "clear_cache", "index", "load", "prompts_dir"]

_PACKAGE_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
#: judge.v1.0.md → (name=judge, 1, 0); имя без версии допустимо и считается v0.0
_FILENAME = re.compile(r"^(?P<name>.+?)\.v(?P<major>\d+)\.(?P<minor>\d+)$")


class PromptNotFound(FileNotFoundError):
    """Промпта нет на диске. Подставить «дефолтный текст» молча — потерять версионирование."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """Один промпт: метаданные из frontmatter + тело + хэш тела."""

    id: str
    version: str
    text: str
    sha256: str
    model_role: str = "brain"
    thinking: bool = False
    temperature: float = 0.3
    schema: str | None = None
    path: str | None = None

    def as_record(self) -> dict[str, str]:
        """Именно это кладётся в `decision_records.prompt_ids`."""
        return {"id": self.id, "version": self.version, "sha256": self.sha256}

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.text))

    def render(self, *, strict: bool = True, **values: Any) -> str:
        """Подставить `{{имя}}`.

        `strict` падает на незаполленном плейсхолдере: `{{plan}}`, улетевший в модель, — это не
        «мелочь формата», а вызов, который забыл передать данные.
        """
        out = _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), self.text)
        if strict:
            left = sorted(set(_PLACEHOLDER.findall(out)))
            if left:
                raise KeyError(f"в промпте {self.id} не подставлены: {', '.join(left)}")
        return out


def prompts_dir() -> Path:
    """Каталог промптов; `AEGIS_PROMPTS_DIR` перебивает — для canary-наборов и тестов."""
    override = os.environ.get("AEGIS_PROMPTS_DIR")
    return Path(override) if override else _PACKAGE_PROMPTS


_cache: dict[Path, Prompt] = {}


def clear_cache() -> None:
    _cache.clear()


def load(name: str, *, version: str | None = None, root: Path | None = None) -> Prompt:
    """Загрузить промпт по `модуль/имя` (или просто `имя`); по умолчанию — свежайшая версия.

    `version="1.0"` — конкретная: воспроизводить ход трёхмесячной давности надо по тому тексту, что
    был в ходу, а не по «самому новому похожнему».
    """
    base = root or prompts_dir()
    matches = _candidates(base, name)
    if not matches:
        raise PromptNotFound(f"нет промпта {name!r} в {base} (ожидаем файл вида <имя>.v1.0.md)")
    if version is not None:
        wanted = [path for path in matches if _version_of(path) == version]
        if not wanted:
            raise PromptNotFound(
                f"версии {version} промпта {name!r} нет; есть: "
                f"{', '.join(_version_of(path) for path in matches)}"
            )
        return _read(wanted[0])
    return _read(max(matches, key=_version_key))


def index(root: Path | None = None) -> dict[str, list[str]]:
    """`{"repro/judge": ["1.0"]}` — что реально лежит на диске; это видно в `aegis doctor`."""
    base = root or prompts_dir()
    out: dict[str, list[str]] = {}
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*.md")):
        if path.name.startswith("_"):
            continue  # заготовки и заметки не промпты
        out.setdefault(f"{path.parent.name}/{_name_of(path)}", []).append(_version_of(path))
    return {key: sorted(versions) for key, versions in out.items()}


def _candidates(base: Path, name: str) -> list[Path]:
    if not base.is_dir():
        return []
    module, _, short = name.rpartition("/")
    found: list[Path] = []
    for path in base.rglob("*.md"):
        if _name_of(path) != short:
            continue
        if module and path.parent.name != module:
            continue
        found.append(path)
    return sorted(found)


def _name_of(path: Path) -> str:
    meta = _FILENAME.match(path.stem)
    return meta.group("name") if meta else path.stem


def _version_of(path: Path) -> str:
    meta = _FILENAME.match(path.stem)
    return f"{meta.group('major')}.{meta.group('minor')}" if meta else "0.0"


def _version_key(path: Path) -> tuple[int, int]:
    major, _, minor = _version_of(path).partition(".")
    return int(major or 0), int(minor or 0)


def _read(path: Path) -> Prompt:
    cached = _cache.get(path)
    if cached is not None:
        return cached
    meta, body = _split(path.read_text(encoding="utf-8"))
    text = body.strip("\n")
    prompt = Prompt(
        id=str(meta.get("id") or f"{path.parent.name}/{_name_of(path)}"),
        version=str(meta.get("version") or _version_of(path)),
        text=text,
        sha256=sha256_hex(text.encode("utf-8")),
        model_role=str(meta.get("model_role") or "brain"),
        thinking=_bool(meta.get("thinking")),
        temperature=float(meta.get("temperature") or 0.3),
        schema=str(meta["schema"]) if meta.get("schema") else None,
        path=str(path),
    )
    _cache[path] = prompt
    return prompt


def _split(raw: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(raw)
    if not match:
        return {}, raw
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if ":" not in stripped or line[:1].isspace() or stripped.startswith("#"):
            continue  # списки и вложенность мы не обещали
        key, _, value = stripped.partition(":")
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, raw[match.end() :]


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}
