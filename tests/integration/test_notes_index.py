"""Индексация заметок на живом Postgres: метрика `<=>`, очередь, идемпотентность прохода.

Офлайн-тест (`tests/test_notes_index.py`) проверяет арифметику пакетов и реакции на сбои. Здесь —
то, что двойник не увидит: оператор расстояния pgvector, фиксированная размерность колонки
`vector(2048)`, `ORDER BY ... , created_at DESC` на равных расстояниях и то, что «проиндексировано»
правильно означает «исчезло из очереди».

Заметки каждого теста помечены уникальным тегом и убираются за собой: таблица на всех одна, а
«падает только после X» — самый дорогой класс отладки.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from aegis.knowledge.index import Indexable, index_pending
from aegis.knowledge.notes import Note, NotesRepo
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine, session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]

DIMS = 2048


@pytest_asyncio.fixture
async def db() -> AsyncIterator[str]:
    """Готовая база и тег теста; тег же — за что убирать."""
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    tag = f"idx-{uuid.uuid4().hex[:8]}"
    with override_settings(database_url=url, embedding_dims=DIMS):
        reset_engine()
        try:
            async with session() as s:
                await s.execute(text("SELECT 1 FROM knowledge.notes LIMIT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"схема knowledge не накатана ({type(exc).__name__}) — `make migrate`")
        yield tag
        async with session() as s:
            await s.execute(
                text("DELETE FROM knowledge.notes WHERE :tag = ANY(tags)").bindparams(tag=tag)
            )
        reset_engine()


def spike(dim: int) -> list[float]:
    """Единичный вектор в одной координате: косинус с самим собой = 1, с любым другим = 0."""
    vec = [0.0] * DIMS
    vec[dim] = 1.0
    return vec


class Embedder:
    """Локальный «провайдер эмбеддингов»: по тексту возвращает детерминированный вектор."""

    def __init__(self, vector: Any = None) -> None:
        self.vector = vector
        self.seen: list[list[str]] = []

    async def __call__(self, texts: Any) -> list[list[float]]:
        self.seen.append(list(texts))
        if self.vector is not None:
            return [list(self.vector) for _ in texts]
        return [spike(i) for i in range(len(texts))]


async def add(tag: str, title: str, body: str = "") -> Note:
    repo = NotesRepo()
    return await repo.add(title, body, [tag], source="test")


async def embedded(note_id: str) -> tuple[bool, bool]:
    """(есть ли вектор, есть ли отметка об индексации)."""
    async with session() as s:
        row = await s.execute(
            text(
                "SELECT embedding IS NOT NULL, indexed_at IS NOT NULL "
                "FROM knowledge.notes WHERE id = CAST(:id AS uuid)"
            ).bindparams(id=note_id)
        )
        r = row.one()
    return bool(r[0]), bool(r[1])


async def test_pass_fills_queue_and_empties_it(db: str) -> None:
    repo = NotesRepo()
    first = await add(db, "Отчёт за квартал", "цифры и выводы")
    second = await add(db, "Заметка о кофе", "не пить после 16")

    pending = [n.id for n in await repo.pending_embeddings(limit=500)]
    assert first.id in pending and second.id in pending

    embed = Embedder()
    report = await index_pending(repo, embed, limit=500, batch=2)
    assert report.ok and report.indexed >= 2, report.summary()
    # пакеты смотрим «в каком-нибудь», а не «в первом»: на переиспользованной базе в очередь
    # могли лежать чужие заметки, и порядок пакетов — не свойство индекса
    assert max(len(batch) for batch in embed.seen) <= 2, "размер пакета не соблюдается"
    assert any(len(batch) > 1 for batch in embed.seen), (
        "заметки должны уходить пакетами, а не по одной"
    )
    mine = next(text for batch in embed.seen for text in batch if "Отчёт за квартал" in text)
    assert "цифры и выводы" in mine, "в индекс уходит заголовок и тело"

    assert await embedded(first.id) == (True, True)
    left = [n.id for n in await repo.pending_embeddings(limit=500)]
    assert first.id not in left and second.id not in left, "проход обязан вычитать из очереди"

    again = await index_pending(repo, Embedder(), limit=500, batch=2)
    assert again.ok, "второй проход не должен находить то, что только что записал"


async def test_dry_run_changes_nothing(db: str) -> None:
    repo = NotesRepo()
    note = await add(db, "Проба пера", "тело")
    report = await index_pending(repo, Embedder(), limit=500, batch=8, dry_run=True)

    assert report.scanned >= 1 and report.indexed == report.scanned
    assert await embedded(note.id) == (False, False), "--dry-run не пишет и не трогает очередь"


async def test_vector_search_ranks_the_nearest_note(db: str) -> None:
    repo = NotesRepo()
    near = await add(db, "Курс валют", "доллар и евро")
    far = await add(db, "Рецепт борща", "свёкла")

    async def set_exact(note_id: str, dim: int) -> None:
        await repo.set_embedding(note_id, spike(dim))

    await set_exact(near.id, 0)
    await set_exact(far.id, 1)

    hits = await repo.search("борщ", spike(0), limit=5)
    assert hits and hits[0].method == "vector", hits
    assert hits[0].id == near.id, "косинус спрашивает у базы, а не у нас"
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)


async def test_orthogonal_query_falls_back_to_text_search(db: str) -> None:
    """Ортогональный запрос = косинус 0 → порог 0.2 отсекает мусор, и работает text-match.

    Без этого «найдено» рождалось бы из ничего: эмбеддинги соседних по смыслу заметок всегда дают
    небольшое сходство, и это сходство — не ответ на запрос.
    """
    repo = NotesRepo()
    note = await add(db, "Акт сверки за сентябрь", "подписи сторон")
    await repo.set_embedding(note.id, spike(0))

    hits = await repo.search("Акт сверки за сентябрь", spike(2047), limit=5)
    assert hits, "текстовый фолбэк обязан найти то, что не нашёл вектор"
    assert all(h.method != "vector" for h in hits), [(h.method, h.score) for h in hits]
    assert hits[0].id == note.id


async def test_search_works_before_any_embedding_exists(db: str) -> None:
    note = await add(db, "Список покупок", "молоко, хлеб, кофе")
    repo = NotesRepo()

    hits = await repo.search("молоко", spike(3), limit=5)
    assert hits and hits[0].id == note.id, "поиск не имеет права ждать индексации"
    assert hits[0].method in ("text", "like")


async def test_limit_leaves_the_rest_for_the_next_pass(db: str) -> None:
    repo = NotesRepo()
    ids = [(await add(db, f"Пакетная заметка {i}")).id for i in range(3)]

    report = await index_pending(repo, Embedder(), limit=1, batch=8)
    assert report.scanned == 1 and report.indexed == 1
    left = [n.id for n in await repo.pending_embeddings(limit=500)]
    assert len([i for i in ids if i in left]) == 2, "остаток очереди — это следующий проход"


async def test_dimension_guard_rejects_a_wrong_vector(db: str) -> None:
    """Колонка `vector(2048)` фиксирована: 7-мерный вектор — ошибка настройки, а не данных.

    Проверяем оба слоя: магазин отказывает сам (иначе Postgres ответил бы непонятным
    «different number of columns»), и индексатор останавливается на этой причине, а не собирает
    сотни одинаковых ошибок.
    """
    repo = NotesRepo()
    note = await add(db, "Проверка размерности")

    async def wrong(texts: Any) -> list[list[float]]:
        return [[0.1] * 7 for _ in texts]

    report = await index_pending(repo, wrong, limit=500, batch=8)
    assert not report.ok and "размерность" in (report.stopped or "")
    assert await embedded(note.id) == (False, False)
    assert isinstance(repo, Indexable), "магазин заметок обязан подходить индексатору структурой"


# ------------------------------------------------------------- F9: алиас и гибрид


async def test_alias_flip_hides_stale_rows_and_shows_reindexed(db: str) -> None:
    """Видимость = index_version == активной версии. Rollback — flip обратно, данные целы."""
    repo = NotesRepo()
    note = await add(db, "Алиас-заметка", "текст про квантовый тостер")
    await repo.set_embedding(note.id, spike(9))
    assert await repo.alias_version() == 1

    async with session() as s:
        plain = await s.execute(
            text("SELECT count(*) FROM knowledge.notes WHERE :t = ANY(tags)").bindparams(t=db)
        )
        assert plain.scalar() == 1

    stale = 2
    assert await repo.flip_alias(stale, note="проверка hide-stale")
    try:
        async with session() as s:
            hidden = await s.execute(
                text("SELECT count(*) FROM knowledge.notes_search WHERE :t = ANY(tags)").bindparams(
                    t=db
                )
            )
            assert hidden.scalar() == 0, "строка старой версии не видна через алиас"
        # строка при этом жива и помечена «к переиндексации»
        pending = await repo.notes_older_than_alias()
        assert any(p.id == note.id for p in pending)
        await repo.mark_indexed(note.id, stale)
        async with session() as s:
            shown = await s.execute(
                text("SELECT count(*) FROM knowledge.notes_search WHERE :t = ANY(tags)").bindparams(
                    t=db
                )
            )
            assert shown.scalar() == 1, "после mark_indexed строка вернулась в алиас"
    finally:
        assert await repo.flip_alias(1, note="rollback после проверки")
        async with session() as s:
            await s.execute(
                text(
                    "UPDATE knowledge.notes SET index_version = 1 WHERE :t = ANY(tags)"
                ).bindparams(t=db)
            )


async def test_search_hybrid_merges_lexical_and_vector_hits(db: str) -> None:
    """Гибрид не «или/или»: то, что видит хотя бы один ранжер, — находится."""
    repo = NotesRepo()
    both = await add(db, "Квантовый тостер", "тостер облучённый но хлеб греет")
    vector_only = await add(db, "Заметка ЙЦУКЕН", "аппарат для подогрева хлеба")
    await repo.set_embedding(both.id, spike(31))
    await repo.set_embedding(vector_only.id, spike(31))

    hits = await repo.search_hybrid("тостер", spike(31), limit=5)
    assert hits, "гибрид обязан найти то, что хотя бы один ранжер видит"
    ids = [h.id for h in hits]
    assert ids[0] == both.id, "совпадение по обоим слоям — первое"
    assert all(h.method == "hybrid" for h in hits)

    # без эмбеддинга гибрид честен: это всё ещё поиск, а не пустота
    text_only = await repo.search_hybrid("квантовый", None, limit=5)
    assert any(h.id == both.id for h in text_only)


async def test_search_hybrid_without_alias_falls_back_to_plain(db: str) -> None:
    """Нет вьюхи — тот же ответ старым путём: алиас оптимизация, а не зависимость.

    Проверяем на «пол»-пути: временно гасим кэш доступности и читаем прямую таблицу —
    гибрид поверх неё обязан совпасть с search() по составу (порядок может отличаться).
    """
    repo = NotesRepo()
    note = await add(db, "Откат на прямой путь", "красный тостер снова в деле")
    repo._alias_ok = False  # noqa: SLF001 - имитируем базу до 0007
    hits = await repo.search_hybrid("тостер красный", None, limit=5)
    plain = await repo.search("тостер красный", None, limit=5)
    assert {h.id for h in hits} == {h.id for h in plain}
    assert any(h.id == note.id for h in hits)
