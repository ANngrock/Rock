"""Инструменты действий: исходящие HTTP-вызовы по сохранённым эндпоинтам и вебхуки-входы.

Границы, которые не переступить:
* секреты эндпоинтов не вводятся и не показываются через модель — только через CLI
  (``aegis action add --secret``): в чат секреты не попадают вообще, в списке видны имена;
* каждый вызов проходит SSRF-guard и попадает в журнал ``automation.run`` с замаскированным
  содержимым; модель получает выжимку, не полный ответ сервера;
* входящий вебхук, созданный моделью, получает секрет ровно один раз и только через CLI —
  модель отдаёт адрес без ключа.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.automation.execute import ActionError, run_endpoint
from aegis.automation.store import SqlAutomationStore
from aegis.governance.policy import Risk
from aegis.platform.config import settings


class NoArgs(BaseModel):
    """Пустой набор аргументов (для OpenAI-схемы важен ``type: object``)."""


class EndpointAddArgs(BaseModel):
    name: str = Field(min_length=2, max_length=60, description="короткое имя: notify-deploy")
    url: str = Field(
        min_length=8,
        max_length=2000,
        description="https://… (http — только для явного локального теста)",
    )
    method: str = Field(default="POST", description="GET|POST|PUT|PATCH|DELETE")
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "заголовки; в значении можно писать {{secret:ИМЯ}} — сам секрет я НЕ принимаю "
            "и не храню: владелец вносит его через `aegis action add --secret`"
        ),
    )
    body_template: str = Field(
        default="",
        max_length=4000,
        description="тело запроса; плейсхолдеры {{переменная}} подставятся при вызове",
    )
    timeout_ms: int = Field(default=15000, ge=500, le=120000)


class RunActionArgs(BaseModel):
    endpoint: str = Field(min_length=1, max_length=120, description="имя эндпоинта или начало id")
    variables: dict[str, str] = Field(
        default_factory=dict,
        description="значения для {{плейсхолдеров}} из body_template/заголовков",
    )


class WebhookAddArgs(BaseModel):
    name: str = Field(min_length=3, max_length=41, description="имя в URL: /h/<имя>")
    policy: str = Field(
        default="notify",
        description=(
            "notify — прислать владельцу текст вебхука; turn — запустить по нему агентный ход"
        ),
    )
    rate_per_min: int = Field(default=6, ge=0, le=120, description="0 — без лимита")


class HookRefArgs(BaseModel):
    ref: str = Field(min_length=1, max_length=120, description="имя или начало id вебхука")


class HookToggleArgs(BaseModel):
    ref: str = Field(min_length=1, max_length=120)
    enabled: bool = Field(default=False, description="true — включить, false — выключить")


@registry.register(
    "run_action",
    "Вызвать настроенный владельцем HTTP-эндпоинт (действие): бот сам дёргает чужую систему — "
    "deploy, нотификацию, webhook-на-сторону. Эндпоинты и их секреты настраивает владелец.",
    RunActionArgs,
    writes=True,
    risk=Risk.MEDIUM,
)
async def run_action(args: RunActionArgs, ctx: ToolContext) -> str:
    cfg = settings()
    if not cfg.automation_enabled:
        return "Действия выключены (AUTOMATION_ENABLED=false)."
    store = SqlAutomationStore()
    try:
        row, secrets = await store.endpoint_for_run(owner_id=ctx.owner_id, ref=args.endpoint)
    except KeyError:
        return f"Эндпоинт «{args.endpoint}» не найден. Список: /menu → Автоматизация."
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if not row.enabled:
        return f"«{row.name}» выключен владельцем — не вызываю."
    try:
        result = await run_endpoint(
            row, secrets, variables=args.variables, max_body_kb=cfg.action_max_body_kb
        )
    except ActionError as exc:
        await store.record_run(
            endpoint_id=row.id,
            owner_id=ctx.owner_id,
            ok=False,
            status=0,
            ms=0,
            digest=str(exc),
            triggered_by="model",
        )
        return f"Действие не выполнено: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Сбой при вызове: {type(exc).__name__}: {str(exc)[:200]}"
    await store.record_run(
        endpoint_id=row.id,
        owner_id=ctx.owner_id,
        ok=result.ok,
        status=result.status,
        ms=result.ms,
        digest=result.digest,
        triggered_by="model",
    )
    head = "Вызвал" if result.ok else "Ответили плохим кодом"
    return f"{head} {row.method} «{row.name}»: {result.digest}"


@registry.register(
    "endpoint_add",
    "Сохранить HTTP-эндпоинт для действий (без секретов — их добавит сам владелец через CLI).",
    EndpointAddArgs,
    writes=True,
    risk=Risk.MEDIUM,
)
async def endpoint_add(args: EndpointAddArgs, ctx: ToolContext) -> str:
    if not settings().automation_enabled:
        return "Действия выключены (AUTOMATION_ENABLED=false)."
    try:
        row = await SqlAutomationStore().upsert_endpoint(
            owner_id=ctx.owner_id,
            name=args.name,
            url=args.url,
            method=args.method,
            headers=args.headers,
            body_template=args.body_template,
            timeout_ms=args.timeout_ms,
        )
    except ValueError as exc:
        return f"Не сохраняю: {exc}"
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    return (
        f"Эндпоинт «{row.name}» сохранён ({row.method} {row.url}). Секреты, если нужны, — "
        "только через `aegis action add` с --secret (я их не вижу и не храню)."
    )


@registry.register(
    "endpoint_list",
    "Список сохранённых эндпоинтов действий (без секретов).",
    NoArgs,
    risk=Risk.NONE,
)
async def endpoint_list(args: NoArgs, ctx: ToolContext) -> str:
    try:
        rows = await SqlAutomationStore().list_endpoints(owner_id=ctx.owner_id)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if not rows:
        return (
            "Эндпоинтов нет. Настроить: `aegis action add …` — или скажи мне url, сохраню каркас."
        )
    lines = []
    for r in rows:
        flag = "" if r.enabled else " [выключен]"
        sec = f" · секреты: {', '.join(r.secret_names)}" if r.secret_names else ""
        last = f" · последний: {r.last_status}" if r.last_status else ""
        lines.append(f"• {r.name} — {r.method} {r.url}{sec}{last}{flag}")
    return "\n".join(lines)


@registry.register(
    "endpoint_remove",
    "Удалить эндпоинт действия (журнал его запусков остаётся).",
    HookRefArgs,
    writes=True,
    risk=Risk.LOW,
)
async def endpoint_remove(args: HookRefArgs, ctx: ToolContext) -> str:
    try:
        ok = await SqlAutomationStore().drop_endpoint(owner_id=ctx.owner_id, ref=args.ref)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    return "Удалил." if ok else f"Не нашёл эндпоинт «{args.ref}»."


@registry.register(
    "action_history",
    "Последние запуски действий: код ответа, длительность, выжимка (секреты замаскированы).",
    NoArgs,
    risk=Risk.NONE,
)
async def action_history(args: NoArgs, ctx: ToolContext) -> str:
    try:
        runs = await SqlAutomationStore().recent_runs(owner_id=ctx.owner_id, limit=12)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if not runs:
        return "Журнал пуст — ни одного вызова ещё не было."
    lines = [
        f"{'✅' if r['ok'] else '❌'} {r['name']}: {r['status']} · {r['ms']} мс · "
        f"{r['triggered_by']} · {str(r['digest'])[:120]}"
        for r in runs
    ]
    return "Журнал действий:\n" + "\n".join(lines)


@registry.register(
    "webhook_add",
    "Создать входящий вебхук: внешний мир сможет пнуть бота POST'ом /h/<имя>. "
    "Секретный ключ владелец увидит через `aegis hook url` — я его не знаю и не передаю.",
    WebhookAddArgs,
    writes=True,
    risk=Risk.MEDIUM,
)
async def webhook_add(args: WebhookAddArgs, ctx: ToolContext) -> str:
    cfg = settings()
    if not cfg.automation_enabled:
        return "Действия выключены (AUTOMATION_ENABLED=false)."
    try:
        hook, _secret = await SqlAutomationStore().add_hook(
            owner_id=ctx.owner_id,
            name=args.name,
            policy=args.policy,
            rate_per_min=args.rate_per_min,
        )
    except ValueError as exc:
        return f"Не создаю: {exc}"
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    base = cfg.hooks_public_url or f"http://{cfg.hooks_bind}:{cfg.hooks_port}"
    note = ""
    if not cfg.hooks_enabled:
        note = " ⚠️ приёмник выключен (HOOKS_ENABLED=false) — включите, иначе не приму"
    return (
        f"Вебхук «{hook.name}» создан: POST {base}/h/{hook.name} (политика {hook.policy}). "
        f"Ключ одноразово: `aegis hook url {hook.name}`.{note}"
    )


@registry.register(
    "webhook_list", "Список входящих вебхуков владельца (без секретов).", NoArgs, risk=Risk.NONE
)
async def webhook_list(args: NoArgs, ctx: ToolContext) -> str:
    try:
        hooks = await SqlAutomationStore().list_hooks(owner_id=ctx.owner_id)
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    if not hooks:
        return "Вебхуков нет."
    lines = [
        f"• {h.name} — {h.policy}, лимит {h.rate_per_min}/мин, срабатываний {h.fires}"
        + ("" if h.enabled else " [выключен]")
        for h in hooks
    ]
    return "\n".join(lines)


@registry.register(
    "webhook_toggle",
    "Включить/выключить входящий вебхук (секрет при этом не показывается и не меняется).",
    HookToggleArgs,
    writes=True,
    risk=Risk.LOW,
)
async def webhook_toggle(args: HookToggleArgs, ctx: ToolContext) -> str:
    try:
        note = await SqlAutomationStore().set_hook_enabled(
            owner_id=ctx.owner_id, ref=args.ref, enabled=args.enabled
        )
    except Exception as exc:  # noqa: BLE001
        return _db_note(exc)
    return note or f"Не нашёл вебхук «{args.ref}»."


def _db_note(exc: Exception) -> str:
    return (
        "База недоступна — не сохранено (обычно не поднят Postgres, make up-core). "
        f"Подробность: {type(exc).__name__}: {str(exc)[:160]}"
    )
