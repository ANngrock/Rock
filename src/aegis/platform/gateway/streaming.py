"""Сборка потока `chat/completions (stream=True)` в обычное сообщение.

Зачем отдельный модуль: у стриминга ровно одна ответственность — склеить чанки в то, что вернул бы
обычный запрос, и ни одной больше. Провайдеры отдают это очень по-разному, и именно здесь это
безразлично:

* `delta.content` приходит кусками произвольной длины (иногда посреди слова, иногда по одному
  знаку), поэтому текст накапливается строкой, а не списком — иначе вложенные markdown-конструкции
  разъезжаются;
* `delta.tool_calls` приходит фрагментами с `index`: `id` и `function.name` приходят один раз, а
  `function.arguments` — десятками кусков, и несобранные `arguments` это вызов инструмента с битым
  JSON (policy не пройдёт валидацию, и владелец получит «инструмент недоступен»);
* usage-чанк обычно приходит последним и **без** `choices` — обращение к `choices[0]` на нём падает;
* reasoning (`reasoning_content` у z.ai, `reasoning` у других) — отдельная строка: её не показывают
  владельцу, но она нужна в журнале, чтобы «почему такой ответ» читалось и через год.

Поток может оборваться. Модуль не решает, что с этим делать — он отдаёт накопленное через
:meth:`StreamCollector.result` и честит `emitted`, чтобы вызывающий код различал «не пришло ничего»
(можно ретраить) и «текст уже пошёл владельцу» (ретраить нельзя, надо отмечать обрыв).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["StreamCollector", "Streamed", "StreamedToolCall"]

TextSink = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class StreamedToolCall:
    """Один вызов инструмента, собранный из фрагментов."""

    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""

    def as_raw(self) -> dict[str, Any]:
        """Форма OpenAI для истории: ровно то, что вернул бы не-стриминговый ответ."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(slots=True)
class Streamed:
    """Слепок накопленного. `finish_reason=None` = поток не дошёл до конца."""

    content: str | None = None
    reasoning: str = ""
    tool_calls: list[StreamedToolCall] = field(default_factory=list)
    model: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    chunks: int = 0
    emitted: int = 0

    @property
    def has_text(self) -> bool:
        return self.emitted > 0


class StreamCollector:
    """Аккумулятор чанков с опциональной отдачей текста наружу (`on_text`).

    `on_text` вызывается на каждый содержательный кусок и только для `content`: показывать владельцу
    reasoning или обрывки JSON инструмента — не «живой ответ», а подглядывание в черновик.
    """

    def __init__(self, on_text: TextSink | None = None) -> None:
        self._on_text = on_text
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, StreamedToolCall] = {}
        self._model: str | None = None
        self._finish: str | None = None
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._chunks = 0
        self._emitted = 0

    async def collect(self, stream: AsyncIterator[Any]) -> None:
        """Пройти поток до конца. Исключение наружу — решение принимает вызывающий код."""
        async for chunk in stream:
            self._chunks += 1
            self._model = self._model or getattr(chunk, "model", None) or None
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                # usage-чанк не содержит choices: порядок проверок здесь важнее красоты
                self._prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                self._completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            reason = getattr(choice, "finish_reason", None)
            if reason:
                self._finish = str(reason)
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            piece = getattr(delta, "content", None)
            if piece:
                self._content.append(piece)
                self._emitted += len(piece)
                if self._on_text is not None:
                    await self._on_text(piece)
            thought = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if isinstance(thought, str) and thought:
                self._reasoning.append(thought)
            for frag in getattr(delta, "tool_calls", None) or []:
                self._take_fragment(frag)

    def result(self) -> Streamed:
        content = "".join(self._content) or None
        return Streamed(
            content=content,
            reasoning="".join(self._reasoning),
            tool_calls=[self._calls[key] for key in sorted(self._calls)],
            model=self._model,
            finish_reason=self._finish,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            chunks=self._chunks,
            emitted=self._emitted,
        )

    @property
    def chunks(self) -> int:
        return self._chunks

    @property
    def has_text(self) -> bool:
        """Текст уже ушёл наружу — отыграть назад нельзя, ретраить нельзя."""
        return self._emitted > 0

    def _take_fragment(self, frag: Any) -> None:
        index = int(getattr(frag, "index", 0) or 0)
        call = self._calls.setdefault(index, StreamedToolCall(index=index))
        call.id = getattr(frag, "id", None) or call.id
        fn = getattr(frag, "function", None)
        if fn is None:
            return
        call.name = getattr(fn, "name", None) or call.name
        args = getattr(fn, "arguments", None)
        if args:
            call.arguments += args
