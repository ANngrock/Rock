"""Инструменты когнитивного слоя: словарь владельца и лента юзербота.

Границы выбраны по направлению угрозы:
* `lexicon_add` — LOW без подтверждения: записать значение слова, которое объяснил владелец,
  то же доверие, что у remember_fact;
* `inbox_recent` — только чтение: модель может рассказать владельцу, что накопилось в чатах,
  но НЕ может отвечать от его имени. Режимы автоответа (draft/auto) выставляются человеком
  через CLI: если это сможет модель, prompt-инъекция из чужого сообщения получит «руки»
  в чужом аккаунте — а это тот сценарий, который нельзя ни экономить, ни «доверять модели»."""

from __future__ import annotations

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk

__all__ = ["inbox_recent", "lexicon_add", "lexicon_list"]


class LexiconAddArgs(BaseModel):
    term: str = Field(min_length=1, max_length=64, description="слово/аббревиатура как их пишут")
    means: str = Field(min_length=1, max_length=500, description="что это значит для владельца")
    kind: str = Field(
        default="term",
        description="term (жаргон/сокращение) | person (кто этот человек) | style (просьба о тоне)",
    )


class NoArgs(BaseModel):
    """Пустой набор аргументов (для OpenAI-схемы важен ``type: object``)."""


class InboxArgs(BaseModel):
    limit: int = Field(default=8, ge=1, le=25, description="сколько последних входящих показать")


@registry.register(
    "lexicon_add",
    "Записать в словарь владельца, что значит его слово, сокращение, кто такой человек или как"
    " он просит отвечать. Дальше воронка будет подсовывать это объяснение в каждую реплику,"
    " где встречается термин.",
    LexiconAddArgs,
    writes=True,
    risk=Risk.LOW,
)
async def lexicon_add(args: LexiconAddArgs, ctx: ToolContext) -> str:
    from aegis.cognition.lexicon import SqlLexicon

    try:
        term = await SqlLexicon().upsert(ctx.owner_id, args.term, args.means, args.kind)
    except ValueError as exc:
        return f"! {exc}"
    return f"Записано в словарь: «{term}» ({args.kind})."


@registry.register(
    "lexicon_list",
    "Показать словарь владельца (термины, люди, пожелания тону). Только чтение.",
    NoArgs,
)
async def lexicon_list(args: NoArgs, ctx: ToolContext) -> str:
    from aegis.cognition.lexicon import SqlLexicon

    entries = await SqlLexicon().list_terms(ctx.owner_id)
    if not entries:
        return "Словарь пуст. Скажи «запомни: <термин> = <значение>» — и я запишу."
    return "\n".join(f"{e.kind} {e.term} = {e.means}" for e in entries)


@registry.register(
    "inbox_recent",
    "Последние входящие из личных чатов через юзербот: чат, от кого, вердикт воронки,"
    " есть ли черновик. Только чтение; отвечать может только владелец командами /ub.",
    InboxArgs,
)
async def inbox_recent(args: InboxArgs, ctx: ToolContext) -> str:
    from aegis.platform.config import settings

    cfg = settings()
    if not cfg.userbot_enabled:
        return "Юзербот выключен (USERBOT_ENABLED=false) — личных чатов не видно."
    from aegis.cognition.inbox import SqlInboxStore

    rows = await SqlInboxStore().list_recent(owner_id=ctx.owner_id, limit=args.limit)
    if not rows:
        return "Инбокс пуст: демон ещё ничего не принёс."
    return "\n".join(
        f"{r.id[:8]} · {r.chat_name or r.chat_id} · {r.from_name or '?'} · {r.verdict}/{r.status}"
        + ("" if r.reply is None else " · черновик есть")
        for r in rows
    )
