"""Индексатор эмбеддингов: очередь, пакеты, и главное — что он делает при отказах.

Магазин заметок здесь подменён, потому что проверяем не pgvector, а решения: «что уходит в модель»,
«когда остановиться» и «что считается проиндексированным». Живой pgvector — в
`tests/integration/test_notes_index.py`.
"""

from __future__ import annotations

from typing import Any

from aegis.knowledge.index import IndexReport, index_pending, index_text
from aegis.knowledge.notes import Note


class Store:
    """Минимальный `Indexable`: очередь, запись вектора, счётчик.

    `write_fails` — по id: так проверяется «одна битая заметка не роняет проход», не изобретая
    отдельный флаг для каждой ветки.
    """

    def __init__(
        self,
        notes: list[Note],
        *,
        write_fails: tuple[str, ...] = (),
        write_error: Exception | None = None,
    ) -> None:
        self.notes = notes
        self.saved: dict[str, list[float]] = {}
        self.write_fails = write_fails
        self.write_error = write_error or RuntimeError("база легла")
        self.limit_calls: list[int] = []

    async def pending_embeddings(self, limit: int = 50) -> list[Note]:
        self.limit_calls.append(limit)
        return self.notes[:limit]

    async def set_embedding(self, note_id: str, embedding: list[float]) -> None:
        if note_id in self.write_fails:
            raise self.write_error
        self.saved[note_id] = embedding

    async def count_pending(self) -> int:
        return len(self.notes) - len(self.saved)


class Embedder:
    """Возвращает векторы по одному на текст; режимы ломают контракт, чтобы проверить реакцию."""

    def __init__(
        self,
        *,
        fail: Exception | None = None,
        short_by: int = 0,
        dims: int = 3,
    ) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.short_by = short_by
        self.dims = dims

    async def __call__(self, texts: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail is not None:
            raise self.fail
        count = max(len(texts) - self.short_by, 0)
        return [[float(i), 1.0, 0.0][: self.dims] for i in range(count)]


def notes(n: int) -> list[Note]:
    return [Note(id=f"n{i}", title=f"заголовок {i}", body=f"тело {i}", tags=[]) for i in range(n)]


# ------------------------------------------------------------------ текст индекса


def test_indexed_text_is_title_and_body_and_is_cut_from_the_end() -> None:
    assert index_text("Итоги", "квартал 3") == "Итоги\nквартал 3"
    assert index_text("только заголовок", "") == "только заголовок"
    assert index_text("", "   ") == "(пустая заметка)"
    long = index_text("т", "а" * 5000, max_chars=100)
    assert len(long) == 100 and long.startswith("т\n"), "обрезаем хвост: начало важнее"


async def test_batch_is_one_call_and_covers_the_whole_group() -> None:
    store, embed = Store(notes(3)), Embedder()
    report = await index_pending(store, embed, batch=2)

    assert [len(c) for c in embed.calls] == [2, 1]
    assert report.batches == 2, "два пакета на три заметки при batch=2"
    assert report.indexed == 3 and report.ok
    assert set(store.saved) == {"n0", "n1", "n2"}


# ------------------------------------------------------------------ очередь


async def test_empty_queue_says_so_and_does_not_touch_the_model() -> None:
    embed = Embedder()
    report = await index_pending(Store([]), embed)

    assert report.scanned == 0 and report.ok
    assert embed.calls == []
    assert "не требовалась" in report.summary()


async def test_limit_bounds_the_queue_taken_in_one_pass() -> None:
    store = Store(notes(5))
    report = await index_pending(store, Embedder(), limit=2, batch=2)

    assert report.scanned == 2 and store.limit_calls == [2]
    assert len(store.saved) == 2, "остаток очереди — это следующий проход, а не потеря"


async def test_dry_run_reads_the_queue_and_writes_nothing() -> None:
    store, embed = Store(notes(2)), Embedder()
    report = await index_pending(store, embed, dry_run=True)

    assert store.saved == {} and embed.calls != []
    assert report.dry_run and report.indexed == 2 and report.failed == 0
    assert "ничего не изменено" in report.summary()


# ------------------------------------------------------------------ отказы


async def test_provider_outage_stops_and_keeps_the_queue_intact() -> None:
    store = Store(notes(4))
    embed = Embedder(fail=ConnectionError("embeddings: таймаут"))
    report = await index_pending(store, embed, batch=2)

    assert store.saved == {}
    assert report.failed == 4 and not report.ok
    assert "провайдер эмбеддингов не ответил" in (report.stopped or "")
    assert ConnectionError.__name__ in (report.stopped or "")
    assert len(embed.calls) == 1, "лежачего провайдера не надо дёргать всеми пакетами сразу"
    assert "не проиндексировано: 4" in report.summary()


async def test_vector_count_mismatch_is_refused_instead_of_guessed() -> None:
    """Текстов 2, векторов 1 — сопоставлять по позиции нельзя: это чужой вектор в заметке.

    «Почти совпало» здесь означало бы «поиск находит не то» без единого следа в логах, поэтому
    пакет отклоняется целиком и проход останавливается.
    """
    store = Store(notes(4))
    report = await index_pending(store, Embedder(short_by=1), batch=2)

    assert store.saved == {}
    assert "1 векторов на 2 текстов" in (report.stopped or "")
    assert report.failed == 4 and not report.ok


async def test_dimension_mismatch_stops_the_whole_pass() -> None:
    """Размерность — настройка окружения: следующая заметка упала бы так же.

    Поэтому остановка, а не «продолжим и соберём 200 одинаковых ошибок».
    """

    async def wrong_dims(texts: Any) -> list[list[float]]:
        return [[0.0] * 7 for _ in texts]

    class StrictStore(Store):
        async def set_embedding(self, note_id: str, embedding: list[float]) -> None:
            if len(embedding) != 3:
                raise ValueError("эмбеддинг для заметки должен быть 3-мерным, получено 7")
            self.saved[note_id] = embedding

    store = StrictStore(notes(3))
    report = await index_pending(store, wrong_dims, batch=3)

    assert store.saved == {}
    assert "размерность эмбеддинга" in (report.stopped or "")
    assert "3-мерным" in (report.stopped or "")
    assert report.failed == 3


async def test_one_broken_note_does_not_break_the_pass() -> None:
    """Запись одной заметки упала (база/диск) — остальные идут, а сбой виден в отчёте."""
    store = Store(notes(3), write_fails=("n1",))
    report = await index_pending(store, Embedder(), batch=3)

    assert set(store.saved) == {"n0", "n2"}
    assert report.indexed == 2 and report.failed == 1
    assert report.stopped is None, "точечный сбой — не повод останавливать проход"


# ------------------------------------------------------------------ отчёт


def test_report_helpers_are_readable_and_honest() -> None:
    ok = IndexReport(scanned=2, indexed=2, batches=1)
    assert ok.ok and "Проиндексировано 2 из 2" in ok.summary()

    bad = IndexReport(scanned=5, indexed=1, failed=4, batches=2, stopped="провайдер молчит")
    assert not bad.ok
    assert "не проиндексировано: 4" in bad.summary()
    assert "остановлено: провайдер молчит" in bad.summary()
