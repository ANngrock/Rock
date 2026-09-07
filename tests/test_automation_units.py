"""Юниты хаба автоматизации: планировщик, рендер/маска, приёмник, карманные инструменты.

Всё, что портит смысл «бот делает», не требуя БД: срок прогона, маска секрета в журнале,
отказ SSRF-guard, арифметика без eval, единицы измерения, HTTP-разбор вебхука.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from aegis.agents.tools.assistant import calc_value, convert_value
from aegis.agents.tools.devbox import devbox_transform
from aegis.automation.execute import (
    ActionError,
    mask_secrets,
    render_template,
    run_endpoint,
)
from aegis.automation.hooks_http import handle_hook_request
from aegis.automation.store import EndpointRow
from aegis.planning.jobs import schedule_next

TZ = ZoneInfo("Europe/Berlin")


# ---------- планировщик ----------


def test_daily_future_time_stays_today() -> None:
    now = datetime(2026, 3, 10, 6, 0, tzinfo=UTC).astimezone(TZ)  # 07:00 берлинское
    nxt = schedule_next(repeat="daily", at_time="09:00", after=now, tz=TZ)
    assert nxt is not None
    assert (nxt.year, nxt.month, nxt.day, nxt.astimezone(TZ).hour) == (2026, 3, 10, 9)


def test_daily_past_time_rolls_to_tomorrow() -> None:
    now = datetime(2026, 3, 10, 12, 30, tzinfo=UTC)  # 13:30 — «12:15» уже прошло
    nxt = schedule_next(repeat="daily", at_time="12:15", after=now, tz=TZ)
    assert nxt is not None and nxt.astimezone(TZ).day == 11
    assert (nxt.astimezone(TZ).hour, nxt.astimezone(TZ).minute) == (12, 15)


def test_weekdays_skips_weekend() -> None:
    sat = datetime(2026, 3, 7, 23, 0, tzinfo=UTC)  # суббота вечер
    nxt = schedule_next(repeat="weekdays", at_time="07:30", after=sat, tz=TZ)
    assert nxt is not None
    assert nxt.astimezone(TZ).weekday() == 0  # понедельник


def test_weekly_lands_on_requested_dow() -> None:
    now = datetime(2026, 9, 7, 9, 15, tzinfo=UTC)  # понедельник 12:15 берлинское
    nxt = schedule_next(repeat="weekly", at_time="09:00", dow=1, after=now, tz=TZ)
    assert nxt is not None and nxt.astimezone(TZ).weekday() == 0
    assert nxt.astimezone(TZ).day == 14  # сегодня 09:00 уже прошло — следующий пн


def test_weekly_dow_before_now_stays_this_week() -> None:
    wed = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)  # среда 08:00
    nxt = schedule_next(repeat="weekly", at_time="10:00", dow=3, after=wed, tz=TZ)
    assert nxt is not None and nxt.astimezone(TZ).day == 9  # сегодня, ещё не прошло


def test_every_min_and_hourly() -> None:
    now = datetime(2026, 1, 1, 10, 7, 42, tzinfo=UTC)
    every = schedule_next(repeat="every_min", every_min=17, after=now, tz=TZ)
    hourly = schedule_next(repeat="hourly", after=now, tz=TZ)
    assert every == datetime(2026, 1, 1, 10, 24, tzinfo=UTC)
    assert hourly == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)


def test_once_is_none_and_bad_inputs_raise() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert schedule_next(repeat="once", after=now, tz=TZ) is None
    with pytest.raises(ValueError, match="repeat"):
        schedule_next(repeat="yearly", after=now, tz=TZ)
    with pytest.raises(ValueError, match="ЧЧ:ММ"):
        schedule_next(repeat="daily", at_time="завтра", after=now, tz=TZ)
    with pytest.raises(ValueError, match="диапазон"):
        schedule_next(repeat="daily", at_time="99:99", after=now, tz=TZ)


def test_dst_morning_survives_clock_change() -> None:
    # в ночь 25→26 октября 2026 в Берлине часы назад; «08:00» обязано остаться 08:00 местным
    before = datetime(
        2026, 10, 24, 22, 0, tzinfo=UTC
    )  # суббота 00:00 летнее? нет: 24.10 00:00 CEST
    nxt = schedule_next(repeat="daily", at_time="08:00", after=before, tz=TZ)
    assert nxt is not None
    assert nxt.astimezone(TZ).strftime("%H:%M %Z") == "08:00 CEST" or nxt.astimezone(TZ).hour == 8
    after_shift = datetime(2026, 10, 25, 22, 0, tzinfo=UTC)  # уже после перевода (UTC 00:00 26.10)
    nxt2 = schedule_next(repeat="daily", at_time="08:00", after=after_shift, tz=TZ)
    assert nxt2 is not None and nxt2.astimezone(TZ).strftime("%d %H:%M") == "26 08:00"


# ---------- рендер, маска, исполнение ----------


def test_render_lists_missing_without_leaking() -> None:
    text, missing = render_template(
        "{'k':'{{secret:TOKEN}}','v':'{{val}}','x':'{{nothere}}'}",
        {"val": "42", "secret:TOKEN": "s3cr3t"},
    )
    assert "s3cr3t" in text and "42" in text
    assert missing == ["nothere"]


def test_mask_secrets_long_first() -> None:
    out = mask_secrets("token ab and secret abc123 end", ["ab", "abc123"])
    assert "abc123" not in out and "token *** and secret *** end" == out.replace("***", "***")
    assert mask_secrets("plain", ["", "x"]) == "plain"


class _Recorder:
    def __init__(self) -> None:
        self.request: httpx.Request | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(201, text=f"created with key={request.headers.get('X-Key')}")


def _ep(**kw: Any) -> EndpointRow:
    base: dict[str, Any] = dict(
        id="e1",
        owner_id=1,
        name="notify",
        method="POST",
        url="http://93.184.216.34/hook",
        headers={"X-Key": "{{secret:TOKEN}}"},
        body_template='{"text":"{{msg}}"}',
        secret_names=("TOKEN",),
        timeout_ms=15000,
    )
    base.update(kw)
    return EndpointRow(**base)


async def test_run_endpoint_success_masks_and_records() -> None:
    rec = _Recorder()
    transport = httpx.MockTransport(rec.handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await run_endpoint(
            _ep(), {"TOKEN": "sup3r-k3y"}, variables={"msg": "git push ok"}, client=client
        )
    assert res.ok and res.status == 201
    assert "sup3r-k3y" not in res.digest
    assert "***" in res.digest
    assert rec.request is not None
    assert rec.request.headers["X-Key"] == "sup3r-k3y"
    assert rec.request.read() == b'{"text":"git push ok"}'


async def test_run_endpoint_error_code_is_not_ok() -> None:
    async def fail(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        res = await run_endpoint(_ep(), {"TOKEN": "k"}, variables={"msg": "x"}, client=client)
    assert not res.ok and res.status == 500


async def test_run_endpoint_missing_variable_lists_names() -> None:
    with pytest.raises(ActionError, match="msg"):
        await run_endpoint(_ep(), {}, variables={})


async def test_run_endpoint_guard_blocks_private_targets() -> None:
    plain = {"headers": {}, "body_template": ""}
    with pytest.raises(ActionError, match="guard"):
        await run_endpoint(_ep(url="http://127.0.0.1:8080/admin", **plain), {})
    with pytest.raises(ActionError, match="guard"):
        await run_endpoint(_ep(url="http://169.254.169.254/latest/meta-data/", **plain), {})


# ---------- приёмник вебхуков ----------


class _Hooks:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.bumps: list[str] = []

    async def hook_by_name(self, name: str) -> Any:
        return self.row if name == self.row[0].name else None

    async def bump_fire(self, hook_id: str) -> None:
        self.bumps.append(hook_id)


def _hooks(policy: str = "notify", rate: int = 2, enabled: bool = True) -> _Hooks:
    from aegis.automation.store import HookRow

    row = HookRow(
        id="h1",
        owner_id=1,
        name="ci",
        policy=policy,
        rate_per_min=rate,
        enabled=enabled,
        fires=0,
        last_fire=None,
    )
    return _Hooks((row, "topsecret"))


async def test_hook_auth_and_lifecycle() -> None:
    store = _hooks()
    r = await handle_hook_request(
        store=store, method="POST", path="/h/other", headers={}, body=b"x"
    )
    assert r.status == 404
    r = await handle_hook_request(store=store, method="POST", path="/h/ci", headers={}, body=b"x")
    assert r.status == 401
    r = await handle_hook_request(
        store=store, method="POST", path="/h/ci?key=nope", headers={}, body=b"x"
    )
    assert r.status == 401
    r = await handle_hook_request(
        store=store,
        method="POST",
        path="/h/ci",
        headers={"x-aegis-key": "topsecret"},
        body=b"deploy  finished\nnow",
        rate_state={},
    )
    assert r.status == 200 and r.payload == "deploy finished now" and r.hook is not None
    assert r.hook.policy == "notify" and store.bumps == ["h1"]


async def test_hook_rate_limit_and_size_caps() -> None:
    store, rate = _hooks(rate=2), {}
    ok = {"x-aegis-key": "topsecret"}
    for _ in range(2):
        assert (
            await handle_hook_request(
                store=store,
                method="POST",
                path="/h/ci",
                headers=ok,
                body=b"a",
                rate_state=rate,
                max_body=10,
            )
        ).status == 200
    third = await handle_hook_request(
        store=store,
        method="POST",
        path="/h/ci",
        headers=ok,
        body=b"a",
        rate_state=rate,
        max_body=10,
    )
    assert third.status == 429
    big = await handle_hook_request(
        store=_hooks(rate=0), method="POST", path="/h/ci", headers=ok, body=b"z" * 11, max_body=10
    )
    assert big.status == 413


async def test_hook_disabled_404_methods_health() -> None:
    off = _hooks(enabled=False)
    r = await handle_hook_request(
        store=off, method="POST", path="/h/ci", headers={"x-aegis-key": "topsecret"}, body=b""
    )
    assert r.status == 403
    put = await handle_hook_request(
        store=_hooks(), method="PUT", path="/h/ci", headers={}, body=b""
    )
    assert put.status == 405
    health = await handle_hook_request(
        store=_hooks(), method="GET", path="/health", headers={}, body=b""
    )
    assert health.status == 200
    bad = await handle_hook_request(
        store=_hooks(), method="POST", path="/other", headers={}, body=b""
    )
    assert bad.status == 404


# ---------- calc ----------


@pytest.mark.parametrize(
    ("expr", "want"),
    [
        ("2+2*3", 8.0),
        ("(2+2)*3", 12.0),
        ("-3 ** 2", -9.0),
        ("7//2", 3.0),
        ("7%3", 1.0),
        ("2**10", 1024.0),
        ("1,5+1", 2.5),
        ("sqrt(16)+abs(-2)", 6.0),
        ("round(2.4)+round(2.6)", 5.0),
        ("max(1, 9, 3) - min(2, 8)", 7.0),
        ("sin(pi)", 0.0),
        ("log(e)", 1.0),
        ("floor(3.9)", 3.0),
    ],
)
def test_calc_exact(expr: str, want: float) -> None:
    assert math.isclose(calc_value(expr), want, abs_tol=1e-12)


@pytest.mark.parametrize(
    "expr",
    [
        "9 ** 9 ** 9",
        "2 ** 10_000",
        "1/0",
        "import os",
        "__import__('os')",
        "'abc' * 3",
        "x + 1",
        "open('/etc/passwd')",
        "1+" + "2" * 600,
    ],
)
def test_calc_refuses(expr: str) -> None:
    with pytest.raises(ValueError):
        calc_value(expr)


# ---------- convert ----------


@pytest.mark.parametrize(
    ("v", "src", "dst", "want"),
    [
        (10, "km", "m", 10000.0),
        (1, "mi", "km", 1.609344),
        (1, "kg", "lb", 2.2046226218),
        (100, "F", "C", 37.7777777778),
        (0, "C", "K", 273.15),
        (1, "gal", "l", 3.785411784),
        (2, "m/s", "km/h", 7.2),
        (1, "GiB", "MiB", 1024.0),
        (1, "ha", "m2", 10000.0),
        (180, "deg", "rad", math.pi),
        (1, "atm", "kPa", 101.325),
        (1, "wk", "h", 168.0),
        (2.5, "t", "kg", 2500.0),
    ],
)
def test_convert_vectors(v: float, src: str, dst: str, want: float) -> None:
    assert math.isclose(convert_value(v, src, dst), want, rel_tol=1e-9)


def test_convert_domain_errors_are_clear() -> None:
    with pytest.raises(ValueError, match="неизвестна"):
        convert_value(1, "parsec", "m")
    with pytest.raises(ValueError, match="разные вещи"):
        convert_value(1, "km", "kg")
    with pytest.raises(ValueError, match="только между"):
        convert_value(1, "C", "m")


# ---------- devbox ----------


def test_devbox_hash_and_b64_roundtrip() -> None:
    assert devbox_transform("hash", "abc") == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert devbox_transform("hash", "abc", "md5") == "md5:900150983cd24fb0d6963f7d28e17f72"
    enc = devbox_transform("b64e", "привет мир")
    assert devbox_transform("b64d", enc) == "привет мир"
    urlish = devbox_transform("b64e", "?=>", "url")
    assert "+" not in urlish and "=" not in urlish


def test_devbox_slug_case_uuid_json() -> None:
    assert devbox_transform("slug", "Привет, Мир!") == "privet-mir"
    assert devbox_transform("slug", "Hello World 42") == "hello-world-42"
    assert devbox_transform("case", "deploy system status", "snake") == "deploy_system_status"
    assert devbox_transform("case", "deploy system status", "camel") == "deploySystemStatus"
    assert devbox_transform("case", "deploy system status", "kebab") == "deploy-system-status"
    assert re.fullmatch(r"[0-9a-f-]{36}", devbox_transform("uuid", ""))
    assert devbox_transform("json", '{"b":1,"a":[2]}', "min") == '{"b":1,"a":[2]}'
    assert devbox_transform("json", '{"b":1,"a":{"c":2}}', "keys") == "a, a.c, b"
    assert "не JSON" in devbox_transform("json", "{щщщ")


def test_devbox_stats_ts_escape_bad_action() -> None:
    stats = devbox_transform("stats", "раз два\nтри")
    assert "слов 3" in stats and "строк 2" in stats
    ts = devbox_transform("ts", "1757289600")
    assert "UTC" in ts and "2025-09" in ts
    back = devbox_transform("ts", "07.09.2025")
    assert "unix" in back
    assert devbox_transform("escape", "<b>&") == "&lt;b&gt;&amp;"
    with pytest.raises(ValueError, match="action"):
        devbox_transform("rm -rf", "")
    with pytest.raises(ValueError, match="длиннее"):
        devbox_transform("hash", "z" * 20001)


# ---------- приёмник: настоящий сокет ----------


async def test_serve_hooks_end_to_end() -> None:
    """serve_hooks против tcp-сокета: парсинг запроса, 401/200, ответ без утечек."""
    import asyncio

    import httpx

    from aegis.automation.hooks_http import serve_hooks
    from aegis.automation.store import HookRow

    row = HookRow(
        id="h1",
        owner_id=1,
        name="ci",
        policy="notify",
        rate_per_min=10,
        enabled=True,
        fires=0,
        last_fire=None,
    )

    class _Store:
        async def hook_by_name(self, name: str) -> Any:
            return (row, "topsecret") if name == "ci" else None

        async def bump_fire(self, hook_id: str) -> None:
            pass

    fired: list[tuple[str, str]] = []

    async def on_fire(hook: Any, payload: str) -> None:
        fired.append((hook.name, payload))

    stop = asyncio.Event()
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    task = asyncio.create_task(
        serve_hooks(store=_Store(), host="127.0.0.1", port=port, on_fire=on_fire, stop_event=stop)
    )
    await asyncio.sleep(0.15)
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(f"http://127.0.0.1:{port}/h/ci", content=b"deploy done")
            assert r.status_code == 401
            r = await client.post(
                f"http://127.0.0.1:{port}/h/ci",
                content=b"deploy done",
                headers={"X-Aegis-Key": "topsecret"},
            )
            assert r.status_code == 200 and "принято" in r.text
            r = await client.get(f"http://127.0.0.1:{port}/health")
            assert r.status_code == 200
        for _ in range(20):
            if fired:
                break
            await asyncio.sleep(0.05)
        assert fired == [("ci", "deploy done")]
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=3)
