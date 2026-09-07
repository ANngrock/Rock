"""Голос: распознавание входящих и синтез ответов. Без иллюзий о гарантиях сети.

Порядок выбора STT жёстко прописан, потому что «попробуй на глаз» в проде означает «молчит
полгода»: 1) OpenAI-совместимый эндпоинт (у любого провайдера свой, ключ в env), 2) локальный
faster-whisper, если пакет и модель реально установлены, 3) честный отказ одной строкой —
«голос не настроен», и владелец слышит правду, а не тишину.

TTS так же: эндпоинт отдаёт mp3 — Telegram принимает mp3 как голосовое, поэтому ffmpeg в
контейнер не просится: «а вот ещё бинарь на 200 МБ ради перекодирования» — это не фича, это долг.

Ответ голосом — отдельное решение (reply_mode), потому что автоголос на каждый чат — спам,
а не «человечность»: match = зеркало («голосом спросил — голосом отвечу»), onrequest = только
когда явно попросят.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

__all__ = ["VoiceError", "decide_voice_reply", "synthesize", "transcribe"]

_TTS_MAX_INPUT = 3500  # символов: больше — не «ответ голосом», а аудиокнига
_STT_MAX_BYTES = 25_000_000  # лимит whisper-совместимых эндпоинтов


class VoiceError(RuntimeError):
    """Голос недоступен — с причиной, которую можно показать владельцу дословно."""


def _bearer(cfg: Any) -> str:
    key = getattr(cfg, "voice_api_key", None) or cfg.glm_api_key
    return key.get_secret_value() if key is not None else ""


def decide_voice_reply(mode: str, *, came_voice: bool, wants_voice: bool) -> bool:
    if mode == "never":
        return False
    if mode == "onrequest":
        return wants_voice
    if mode == "match":
        return came_voice or wants_voice
    return False


@dataclass(frozen=True, slots=True)
class _Endpoint:
    base: str
    model: str
    api_key: str


def _stt_endpoint(cfg: Any) -> _Endpoint | None:
    base = str(getattr(cfg, "voice_stt_base_url", "") or cfg.glm_base_url or "").rstrip("/")
    model = str(getattr(cfg, "voice_stt_model", "") or "").strip()
    if not base or not model:
        return None
    return _Endpoint(base, model, _bearer(cfg))


async def transcribe(cfg: Any, data: bytes, *, mime: str = "audio/ogg") -> tuple[str, str]:
    """(текст, движок). Пустой текст — это VoiceError, а не «успешное молчание»."""
    if not data:
        raise VoiceError("пустой аудиофайл")
    if len(data) > _STT_MAX_BYTES:  # noqa: PLR2004
        raise VoiceError("голосовое слишком большое для распознавания (>25 МБ)")
    ep = _stt_endpoint(cfg)
    if ep is not None:
        try:
            return await _stt_http(ep, data, mime), f"http:{ep.model}"
        except VoiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - эндпоинт сдох — пробуем локальный путь
            log.warning("audio.stt_endpoint_failed", err=repr(exc)[:200])
    local = await _stt_local(cfg, data)
    if local is not None:
        return local, "faster-whisper"
    raise VoiceError(
        "распознавание не настроено: заполните VOICE_STT_MODEL (+VOICE_STT_BASE_URL) или"
        " поставьте faster-whisper в контейнер бота"
    )


async def _stt_http(ep: _Endpoint, data: bytes, mime: str) -> str:
    import httpx

    files = {"file": ("voice." + ("ogg" if "ogg" in mime else "mp3"), data, mime)}
    try:
        async with httpx.AsyncClient(timeout=90.0) as c:
            r = await c.post(
                f"{ep.base}/audio/transcriptions",
                headers={"Authorization": f"Bearer {ep.api_key}"},
                data={"model": ep.model},
                files=files,
            )
    except httpx.HTTPError as exc:
        raise VoiceError(f"STT эндпоинт недоступен: {type(exc).__name__}") from exc
    if r.status_code >= 300:  # noqa: PLR2004
        raise VoiceError(f"STT ответил {r.status_code}: {r.text[:200]}")
    text = str((r.json() or {}).get("text") or "").strip()
    if not text:
        raise VoiceError("распознано пусто: тишина или шум вместо речи")
    return text


async def _stt_local(cfg: Any, data: bytes) -> str | None:
    try:
        import faster_whisper  # noqa: F401, PLC0415 - опциональный тяжёлый путь
    except ImportError:
        return None
    import asyncio
    import io

    def _run() -> str:
        model = faster_whisper.WhisperModel(  # noqa: F841 - имя пакета живёт только здесь
            str(getattr(cfg, "voice_whisper_model", "base") or "base")
        )
        segments, _info = model.transcribe(io.BytesIO(data), language=None, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    return (await asyncio.to_thread(_run)) or None


async def synthesize(cfg: Any, text: str) -> tuple[bytes, str]:
    """(mp3, движок) или VoiceError с причиной. Локального синтеза нет — и не притворяемся."""
    base = str(getattr(cfg, "voice_tts_base_url", "") or cfg.glm_base_url or "").rstrip("/")
    model = str(getattr(cfg, "voice_tts_model", "") or "").strip()
    if not base or not model:
        raise VoiceError("синтез не настроен: заполните VOICE_TTS_MODEL (+VOICE_TTS_BASE_URL)")
    import httpx

    voice = str(getattr(cfg, "voice_tts_voice", "alloy") or "alloy")
    body = {
        "model": model,
        "voice": voice,
        "input": text[:_TTS_MAX_INPUT],
        "response_format": "mp3",
    }
    try:
        async with httpx.AsyncClient(timeout=90.0) as c:
            r = await c.post(
                f"{base}/audio/speech",
                headers={"Authorization": f"Bearer {_bearer(cfg)}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise VoiceError(f"TTS эндпоинт недоступен: {type(exc).__name__}") from exc
    if r.status_code >= 300 or not r.content:  # noqa: PLR2004
        raise VoiceError(f"TTS ответил {r.status_code}: {r.text[:200]}")
    return r.content, f"http:{model}"
