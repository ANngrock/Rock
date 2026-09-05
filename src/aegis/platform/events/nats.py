"""Адаптер nats-py для relay'я: connect / publish / close — и ничего больше.

Весь смысл в том, что здесь нечему быть логика: решения «что публиковать», «когда останавливаться» и
«что считать доставленным» живут в `relay.py` и покрыты тестами с подменённым транспортом. Этот
файл проверяется только живым брокером (его и не будет на машине без профиля `durable`), поэтому он
намеренно скучный: импорт `nats` — внутри метода, чтобы приложение поднималось и без пакета.

Публикация идёт с `ack=True`: `mark_published` вызывается только после подтверждения сервера, иначе
«опубликовано» означало бы «отправили и забыли».
"""

from __future__ import annotations

from typing import Any

import structlog

from aegis.platform.events.relay import RelayUnavailable

log = structlog.get_logger(__name__)

__all__ = ["NatsPublisher"]


class NatsPublisher:
    """Клиент JetStream для relay'я. Создаётся лениво: `start()` до первого `publish`."""

    def __init__(
        self,
        *,
        url: str,
        stream: str | None = "aegis_events",
        connect_timeout_s: float = 3.0,
        ack_wait_s: float = 5.0,
    ) -> None:
        self._url = url
        self._stream = (stream or "").strip() or None
        self._connect_timeout_s = float(connect_timeout_s)
        self._ack_wait_s = float(ack_wait_s)
        self._nc: Any = None
        self._js: Any = None

    @classmethod
    def from_settings(cls, cfg: Any) -> NatsPublisher:
        if not getattr(cfg, "outbox_relay_enabled", False):
            raise RelayUnavailable(
                "OUTBOX_RELAY_ENABLED=false: события остаются в platform.outbox. Включите "
                "переменную и поднимите брокер: docker compose -f deploy/docker-compose.yml "
                "--profile durable up -d nats"
            )
        url = str(getattr(cfg, "nats_url", "") or "").strip()
        if not url:
            raise RelayUnavailable("NATS_URL пуст: публиковать некуда")
        return cls(
            url=url,
            stream=str(getattr(cfg, "nats_stream", "") or ""),
            connect_timeout_s=float(getattr(cfg, "nats_connect_timeout_s", 3.0)),
        )

    async def start(self) -> None:
        try:
            import nats  # noqa: PLC0415 - необязательная зависимость: [durable]
        except ImportError as exc:  # noqa: BLE001
            raise RelayUnavailable(
                'нет пакета nats-py: pip install -e ".[durable]" (или включите профиль durable)'
            ) from exc
        try:
            self._nc = await nats.connect(
                servers=[self._url], connect_timeout=self._connect_timeout_s, allow_reconnect=True
            )
        except Exception as exc:  # noqa: BLE001 - классифицируем как «брокер недоступен»
            raise RelayUnavailable(f"NATS не отвечает ({self._url}): {type(exc).__name__}") from exc
        if self._stream is None:
            log.warning(
                "nats.core_mode", note="стрим не задан: публикация без подтверждения сервера"
            )
            return
        self._js = self._nc.jetstream()
        try:
            await self._js.stream_info(self._stream)
        except Exception as exc:  # noqa: BLE001 - «нет стрима» и «сервер лег» различимы по тексту
            if "not found" in str(exc).lower() or "no such" in str(exc).lower():
                await self.aclose()
                raise RelayUnavailable(
                    f"в JetStream нет стрима {self._stream!r}: создайте его один раз — "
                    "nats stream add --subjects 'aegis.*.*.*' "
                    f"--retention limits --max-age 336h --storage file {self._stream}; "
                    "полная команда и зачем именно так — в RUNBOOK, раздел про outbox"
                ) from exc
            await self.aclose()
            raise RelayUnavailable(
                f"JetStream не ответил: {type(exc).__name__}: {exc}"[:300]
            ) from exc

    async def publish(self, subject: str, body: bytes) -> None:
        if self._nc is None:
            raise RelayUnavailable("транспорт не запущен: вызовите start()")
        if self._js is not None:
            await self._js.publish(subject, body, ack=True, timeout=self._ack_wait_s)
            return
        await self._nc.publish(subject, body)

    async def aclose(self) -> None:
        client, self._nc, self._js = self._nc, None, None
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 - закрывать «в никуда» не ошибка
            log.debug("nats.close_failed", err=repr(exc)[:160])
