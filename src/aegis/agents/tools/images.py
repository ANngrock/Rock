"""Подготовка изображения к vision-запросу.

Зачем, если модель «и так понимает»: Telegram присылает фото до 10 МБ, а vision-модель берёт
деньги за токены, которые растут от разрешения. Даунскейл до разумной стороны + JPEG — это в
3–10 раз дешевле при том же качестве понимания. EXIF-поворот применяем сразу: иначе повёрнутое
фото модель читает неправильно, а владелец делает выводы о «глупости» ассистента.
"""

from __future__ import annotations

import io

import structlog

__all__ = ["prepare_image", "sniff_mime"]

log = structlog.get_logger(__name__)

_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",
}


def sniff_mime(data: bytes) -> str:
    for magic, mime in _MAGIC.items():
        if data.startswith(magic):
            return mime
    return "application/octet-stream"


def prepare_image(data: bytes, *, max_side: int = 1568, quality: int = 85) -> tuple[bytes, str]:
    """-> (байты, mime). При любой проблеме с декодированием отдаём исходники как есть.

    Никакого «упал — значит фото плохое»: vision-запрос возможен и с оригиналом, а вот
    исключение внутри подготовки не должно лишать владельца ответа.
    """
    original_mime = sniff_mime(data)
    if original_mime == "image/gif":  # анимация: не трогаем, модель сама разберётся с первым кадром
        return data, original_mime
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as opened:
            image: Image.Image = ImageOps.exif_transpose(opened)
            if max(image.width, image.height) > max_side:
                scale = max_side / max(image.width, image.height)
                image = image.resize(
                    (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
        # если «оптимизация» раздула файл — дешевле отправить оригинал
        if len(out) >= len(data):
            return data, original_mime
        return out, "image/jpeg"
    except Exception as exc:  # noqa: BLE001 - битый/неизвестный формат: отдаём как есть
        log.warning("image.prepare_failed", err=repr(exc)[:200], size=len(data))
        return data, original_mime
