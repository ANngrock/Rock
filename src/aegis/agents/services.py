"""DI-контейнер агента: gateway + репозитории доменов в одном объекте.

Инструменты получают сервисы через :class:`ToolContext`, поэтому handler'ы не импортируют
``aegis.platform.db`` и не создают репозитории сами — их можно подменить в тестах целиком.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from aegis.governance.recorder import DecisionRecorder, NullDecisionRecorder
from aegis.knowledge.notes import Notes, NotesRepo
from aegis.memory.facts import Facts, FactsRepo
from aegis.platform.db import SessionFactory
from aegis.platform.gateway.client import ModelGateway
from aegis.web.fetch import WebFetch
from aegis.web.search import WebSearch

__all__ = ["Services"]


@dataclass(slots=True)
class Services:
    gateway: ModelGateway
    facts: Facts
    notes: Notes
    search: WebSearch = field(default_factory=WebSearch)
    fetch: WebFetch = field(default_factory=WebFetch)
    #: журнал решений (M1): нужен инструментам объяснения, поэтому живёт в сервисах, а не только
    #: в supervisor'е — иначе «объяснить ход» не смог бы читать трассу
    repro: DecisionRecorder = field(default_factory=NullDecisionRecorder)

    @classmethod
    def build(
        cls,
        gateway: ModelGateway,
        session_factory: SessionFactory | None = None,
        *,
        repro: DecisionRecorder | None = None,
    ) -> Self:
        return cls(
            gateway=gateway,
            facts=FactsRepo(session_factory),
            notes=NotesRepo(session_factory),
            repro=repro if repro is not None else NullDecisionRecorder(),
        )

    async def aclose(self) -> None:
        await self.gateway.aclose()
