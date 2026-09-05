"""Индексация заметок: эмбеддинги строит фоновый проход, а не путь записи.

Почему не «встроить сразу при сохранении», хотя кажется очевидным:

* путь записи принадлежит владельцу и обязан оставаться предсказуемым: вызов эмбеддинга — это ещё
  несколько сотен миллисекунд и отдельная причина упасть, а заметка тем временем уже сохранена;
* у провайдера эмбеддингов бывают окна недоступности и лимиты. Десяток заметок за час — один
  пакетный запрос вместо десятка вызовов внутри пользовательского действия с ретраями;
* индекс должен быть идемпотентным пересчётом, а не частью транзакции: «что проиндексировано» —
  производные данные, они имеют право отставать на один проход.

Поиск индексации не ждёт: `NotesRepo.search` сначала пробует вектор, и если ни одного эмбеддинга нет
(или провайдер недоступен), уходит в text-match, а затем в ILIKE (принцип 5). Задерживается только
семантика «найди похожее», а не доступность заметок.

Размеры пакетов и обрезка текста — настройки `EMBED_BATCH_SIZE`, `EMBED_INDEX_LIMIT`,
`EMBED_MAX_CHARS`. Обрезаем хвост, а не начало: заголовок и первые абзацы несут смысл, конец
сохранённой страницы — чаще всего мусор.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

__all__ = ["EmbedBatch", "IndexReport", "Indexable", "index_pending", "index_text"]

log = structlog.get_logger(__name__)

#: чем индексируем: принимает тексты, возвращает векторы той же длины (тот же контракт, что у
#: `ModelGateway.embed`, — поэтому боевой вызов и тестовая заглушка не различаются)
EmbedBatch = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


@runtime_checkable
class Indexable(Protocol):
    """Ровно то, что нужно индексатору от магазина заметок.

    Отдельный протокол, а не `NotesRepo`: иначе индексатор потянул бы сессионную фабрику и стал бы
    неотделим от Postgres — а проверять «что считать очередью» и «как записать вектор» хочется без
    базы.
    """

    async def pending_embeddings(self, limit: int = 50) -> list[Any]: ...

    async def set_embedding(self, note_id: str, embedding: list[float]) -> None: ...

    async def count_pending(self) -> int: ...


def index_text(title: str, body: str, *, max_chars: int = 2000) -> str:
    """Текст, который реально уходит в модель.

    Отдельная функция потому, что «что проиндексировано» определяет «что найдётся»: разбираться с
    «почему не нашлось» придётся с этим текстом, а не с заметкой целиком.
    """
    head = (title or "").strip()
    tail = (body or "").strip()
    text = f"{head}\n{tail}" if head and tail else (head or tail)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text or "(пустая заметка)"


@dataclass(frozen=True, slots=True)
class IndexReport:
    """Итог прохода. `stopped` — причина, по которой проход прекращён досрочно.

    `failed` не считается отдельно от `indexed`: не проиндексировано = осталось в очереди, и это
    одно и то же число. Расходиться они начали бы ровно тогда, когда отчёт врал бы о состоянии БД.
    """

    scanned: int = 0
    indexed: int = 0
    failed: int = 0
    batches: int = 0
    dry_run: bool = False
    stopped: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        if not self.scanned:
            return "Заметок без эмбеддинга нет — индексация не требовалась."
        if self.dry_run:
            base = f"Ждали бы индексации {self.scanned} замет. (ничего не изменено)"
        else:
            base = f"Проиндексировано {self.indexed} из {self.scanned}"
        if self.batches:
            base += f" · пакетов: {self.batches}"
        if self.failed:
            base += (
                f"; не проиндексировано: {self.failed} — останутся в очереди на следующий проход"
            )
        if self.stopped:
            base += f"\n  остановлено: {self.stopped}"
        return base


async def index_pending(
    source: Indexable,
    embed: EmbedBatch,
    *,
    limit: int = 200,
    batch: int = 32,
    max_chars: int = 2000,
    dry_run: bool = False,
) -> IndexReport:
    """Заполнить эмбеддинги для заметок, у которых их нет.

    Не бросает наружу ни одного сбоя провайдера: это фоновая задача, и «провайдер лег» обязан быть
    строкой в выводе тика и в логе, а не traceback'ом в systemd (тот же договор, что у доставки
    напоминаний).
    """
    scanned: list[Any] = list(await source.pending_embeddings(limit=limit))
    indexed = 0
    batches = 0
    stopped: str | None = None

    for start in range(0, len(scanned), max(batch, 1)):
        group = scanned[start : start + max(batch, 1)]
        texts = [index_text(n.title, n.body, max_chars=max_chars) for n in group]
        batches += 1
        try:
            vectors = list(await embed(texts))
        except Exception as exc:  # noqa: BLE001 - лежачий провайдер: остаток очереди не трогаем
            stopped = f"провайдер эмбеддингов не ответил: {type(exc).__name__}: {str(exc)[:180]}"
            break
        if len(vectors) != len(texts):
            # сопоставлять векторы с заметками по позиции нельзя при любом расхождении: «почти
            # угадали» означало бы чужой вектор в заметке и «находит не то» без следов в логе
            stopped = (
                f"провайдер вернул {len(vectors)} векторов на {len(texts)} текстов — "
                "пакет пропущен, чтобы не перепутать заметки"
            )
            log.warning("index.length_mismatch", got=len(vectors), want=len(texts))
            break
        if dry_run:
            indexed += len(group)
            continue
        for note, vector in zip(group, vectors, strict=True):
            try:
                await source.set_embedding(str(note.id), list(vector))
            except ValueError as exc:
                # размерность не совпадает — это настройка окружения, а не данные: следующая заметка
                # упадёт так же, поэтому дальше идти бессмысленно
                stopped = f"размерность эмбеддинга: {str(exc)[:200]}"
                break
            except Exception as exc:  # noqa: BLE001 - одна заметка не роняет весь проход
                log.warning("index.write_failed", note=note.id, err=repr(exc)[:200])
                continue
            indexed += 1
        if stopped:
            break

    return IndexReport(
        scanned=len(scanned),
        indexed=indexed,
        failed=max(len(scanned) - indexed, 0),
        batches=batches,
        dry_run=dry_run,
        stopped=stopped,
    )
