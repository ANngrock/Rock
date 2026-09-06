"""F1-приёмка на живой базе: «200 параллельных ходов, ни один не исполнен дважды».

Юнит-слой проверяет порядок и чистые функции; здесь проверяется именно Postgres-контракт:
advisory-lock на trace, частичный индекс ``(owner_id) WHERE status='active'``, CAS по
fencing-токену и ON CONFLICT-дедупликацию реестра. Два объекта ``SqlTurnLedger`` — это честная
модель «два процесса на одной базе» (в бою у них разные пулы соединений; в процессе — разные
экземпляры, общий только сервер), поэтому разминуться они могут только по-настоящему.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from aegis.governance.turns import SqlTurnLedger, TurnBusy
from aegis.platform.config import override_settings
from aegis.platform.db import reset_engine
from aegis.workflows.ledger import SqlLedger, run_once

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("AEGIS_TEST_DATABASE_URL"), reason="нужен Postgres"),
]


@pytest.fixture
def db_url() -> str:
    url = os.environ["AEGIS_TEST_DATABASE_URL"]
    with override_settings(database_url=url):
        reset_engine()
        try:
            yield url
        finally:
            reset_engine()


async def _purge(*owner_ids: int) -> None:
    """Стартовые данные прошлых прогонов не должны влиять на счётчики этого."""
    from sqlalchemy import text  # noqa: PLC0415

    from aegis.platform.db import session  # noqa: PLC0415

    async with session() as s:
        for owner in owner_ids:
            await s.execute(
                text("DELETE FROM governance.turn_claims WHERE owner_id = :o").bindparams(
                    o=int(owner)
                )
            )
            await s.execute(
                text("DELETE FROM governance.turn_queue WHERE owner_id = :o").bindparams(
                    o=int(owner)
                )
            )
        await s.commit()


async def _begin_with_retry(
    ledger: SqlTurnLedger, trace_id: str, *, owner_id: int, tries: int = 400
) -> tuple[int, int]:
    """Взять ход, уступая занятому владельцу. Возвращает (fencing_token, число попыток)."""
    for attempt in range(tries):
        try:
            handle = await ledger.begin(trace_id, owner_id=owner_id)
            return handle.fencing_token, attempt
        except TurnBusy:
            await asyncio.sleep(0.01)
    raise AssertionError("владелец не освободился за 400 попыток — сериализация сломана")


async def test_two_hundred_turns_never_executed_twice(db_url: str) -> None:
    """200 ходов на 4 владельца через два «процесса»: каждый закрыт ровно один раз."""
    owners = [998801, 998802, 998803, 998804]
    await _purge(*owners)
    ledgers = [SqlTurnLedger(), SqlTurnLedger()]
    traces = [str(uuid.uuid4()) for _ in range(200)]

    async def one(i: int) -> tuple[int, bool]:
        owner = owners[i % len(owners)]
        ledger = ledgers[i % 2]
        trace = traces[i]
        token, _tries = await _begin_with_retry(ledger, trace, owner_id=owner)
        first = await ledger.advance(trace, token)
        second = await ledger.advance(trace, token)
        assert second == first + 1, "счётчик шагов теряет инкременты под гонкой"
        return token, await ledger.finish(trace, token)

    results = await asyncio.gather(*(one(i) for i in range(200)))

    assert all(closed for _, closed in results), "не каждый ход закрыл тот, кто его открыл"
    tokens = [token for token, _ in results]
    assert len(set(tokens)) == 200, (
        "fencing-токен обязан быть уникальным — иначе CAS по нему ничего не стоит"
    )
    status = await ledgers[0].status()
    assert status["active"] == 0 and status["queued"] == 0, (
        f"после штатного прогона не должно остаться ни аренды, ни осевшего обновления: {status}"
    )


async def test_repeat_begin_touches_lease_and_never_steals_step(db_url: str) -> None:
    """Двойная доставка update_id: повторный begin активного хода — та же заявка, не вторая.

    Если бы «вторая доставка» вставляла новый claim, шаг счёркивался бы дважды (двойной ответ,
    двойной счёт). Прикосновение к аренде — единственный честный исход: исполнитель тот же.
    """
    ledger = SqlTurnLedger()
    owner = 998811
    await _purge(owner)
    trace = str(uuid.uuid4())
    token1, _ = await _begin_with_retry(ledger, trace, owner_id=owner)
    assert await ledger.advance(trace, token1) == 1
    token2, _ = await _begin_with_retry(ledger, trace, owner_id=owner)
    assert token2 == token1, "активная заявка того же trace не имеет права завестись второй"
    assert await ledger.advance(trace, token1) == 2
    assert await ledger.finish(trace, token1)


async def test_finished_trace_reactivates_with_new_fencing(db_url: str) -> None:
    """После закрытия тот же trace переактивируется новым токеном, а просроченный — остеклён.

    Это защита от «зомби-исполнителя»: процесс, заснувший между шагами, проснётся и попробует
    дописать ход старым забором; CAS по токену обязан превратить это в ноль, а не в порчу журнала.
    """
    ledger = SqlTurnLedger()
    owner = 998812
    await _purge(owner)
    trace = str(uuid.uuid4())
    first_token, _ = await _begin_with_retry(ledger, trace, owner_id=owner)
    assert await ledger.advance(trace, first_token) == 1
    assert await ledger.finish(trace, first_token)
    second_token, _ = await _begin_with_retry(ledger, trace, owner_id=owner)
    assert second_token != first_token, "переактивация обязана выдать новый забор"
    assert await ledger.advance(trace, first_token) == 0, (
        "продвинуть ход просроченным токеном нельзя"
    )
    assert await ledger.advance(trace, second_token) == 2, (
        "шаги переактивации наследуют счётчик, а не обнуляют его — цепочка журнала непрерывна"
    )
    assert not await ledger.finish(trace, first_token), "чужой (старый) токен не закрывает ход"
    assert await ledger.finish(trace, second_token)


async def test_updates_arriving_mid_turn_are_queued_not_eaten(db_url: str) -> None:
    """«Второе сообщение не потеряно»: busy-отказ превращается в очередь, очередь разбирается."""
    owner = 998831
    await _purge(owner)
    ledger = SqlTurnLedger()
    busy_trace = str(uuid.uuid4())
    await ledger.begin(busy_trace, owner_id=owner)

    queued = [str(uuid.uuid4()) for _ in range(3)]
    for i, trace in enumerate(queued):
        with pytest.raises(TurnBusy):
            await ledger.begin(trace, owner_id=owner)
        await ledger.enqueue(owner, trace_id=trace, kind="message", payload={"n": i})
    assert await ledger.queue_count(owner) == 3
    assert await ledger.finish(busy_trace)

    popped: list = []

    async def drain() -> None:
        while True:
            item = await ledger.pop_next(owner)
            if item is None:
                return
            popped.append(item)
            await ledger.close_item(item.queue_id)

    await asyncio.gather(drain(), drain(), drain())
    ids = [item.trace_id for item in popped]
    assert sorted(ids) == sorted(queued), (
        "из очереди не должно потеряться ни одного, и ни одного дважды"
    )
    assert len({p.queue_id for p in popped}) == 3
    order = [p.payload["n"] for p in sorted(popped, key=lambda p: p.queue_id)]
    assert order == [0, 1, 2], (
        "FIFO: «второе сообщение» обязано исполняться вторым, "
        "иначе контекст хода соберётся наоборот"
    )
    assert await ledger.queue_count(owner) == 0


async def test_run_once_dedups_forty_racers_on_one_activity(db_url: str) -> None:
    """F8 на Postgres: 40 одновременных попыток одного activity_id — эффект исполнен один раз.

    Остальные видят чужой running и уходят ни с чем (skip), а добравшийся после завершения
    получает сохранённый результат из реестра (replayed). «Платёж не уйдёт дважды» проверяется
    счётчиком исполнений, а не обещаниями.
    """
    activity = f"{uuid.uuid4()}:1:tool:pay"
    trace = str(uuid.uuid4())
    calls = 0
    lock = asyncio.Lock()
    ledger = SqlLedger()

    async def effect() -> dict[str, object]:
        nonlocal calls
        async with lock:
            calls += 1
            n = calls
        await asyncio.sleep(0.01)
        return {"calls": n, "mark": activity[:8]}

    outcomes = await asyncio.gather(
        *(run_once(ledger, activity, effect, trace_id=trace) for _ in range(40))
    )
    assert calls == 1, f"эффект обязан исполниться ровно раз, исполнен {calls}"
    for o in outcomes:
        if o.state == "running":
            assert o.result == {}, "skip не выдумывает результат — он возвращает «идёт работа»"
    again = await run_once(ledger, activity, effect, trace_id=trace)
    assert again.replayed and again.result == {"calls": 1, "mark": activity[:8]}, (
        "после завершения повтор обязан читать сохранённый результат, а не пересчитывать"
    )
    assert calls == 1
