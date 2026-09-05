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
from aegis.planning.reminders import NullReminderStore, ReminderStore
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
    #: расписание напоминаний (шаг 2): тоже порт, а не «подключим БД потом» — без БД инструмент
    #: обязан сказать «не могу сохранить», а не сохранить в памяти процесса, живущего один запрос
    reminders: ReminderStore = field(default_factory=NullReminderStore)

    @classmethod
    def build(
        cls,
        gateway: ModelGateway,
        session_factory: SessionFactory | None = None,
        *,
        repro: DecisionRecorder | None = None,
        reminders: ReminderStore | None = None,
    ) -> Self:
        return cls(
            gateway=gateway,
            facts=FactsRepo(session_factory),
            notes=NotesRepo(session_factory),
            repro=repro if repro is not None else NullDecisionRecorder(),
            reminders=reminders if reminders is not None else NullReminderStore(),
        )

    async def aclose(self) -> None:
        await self.gateway.aclose()
