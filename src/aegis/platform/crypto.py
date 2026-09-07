"""Конвертовое шифрование блобов (F3): DEK на запись, KEK из env, версия ключа в строке.

Модель такая же, как у KMS-обёрток в «взрослых» системах, и по тем же причинам:

* **DEK — на каждую запись.** Уничтожение ключа делает нечитаемым ровно один блоб: «забыть»
  конкретного человека не должно стоить содержимое всего журнала. Ключи дёшевы, живость — нет.
* **KEK — из окружения, в БД не лежит.** В базе хранятся только завёрнутые DEK и версия ключа.
  Кража дампа без env/vault даёт шифртекст без ключей; ротация KEK — это rewrap (перезавернуть
  DEK), а не переписывание содержимого: sha256 блоба (он же ссылка цепочки) не меняется вовсе.
* **key_version в строке.** Без него ротация превращается в «старые записи больше не читаются».
  С ним — старый DEK разворачивается старым KEK, новый записывается новым, и любое окно между
  состоит из одних только фактов.

Чего здесь сознательно нет: KMS-клиентов, генерации ключей, их хранения в БД и «шифрования
на лету для полей, кроме блобов». Блобы — единственное место, где лежит исходный текст владельца;
колонки метрик (стоимость, длительность) несут мало секретов, и шифрование сломало бы в них
индексы и агрегаты ради символического выигрыша.

Зависимость `cryptography` — не «библиотека для красоты», а единственный допустимый способ
взять AES-GCM: самодеятельные схемы на hashlib здесь были бы хуже, чем ничего.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from aegis.platform.canonical import sha256_bytes

if TYPE_CHECKING:
    from aegis.platform.config import Settings

__all__ = ["BlobCipher", "KeyShredded", "load_keks"]

_GCM_NONCE = 12
#: формат хранимого wrapped-ключа: nonce(12) || ciphertext
_WRAP_PREFIX = b"aeg1:"
_KEK_ENV = re.compile(r"^AEGIS_KEK_V(\d+)$")


class KeyShredded(RuntimeError):
    """Ключ уничтожен (crypto-shredding): содержимое более не читается — и это успех, не авария."""


def _decode_kek(value: str, *, source: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001 — битый base64 в env част как «вставил ключ с переносом»
        raise ValueError(f"{source}: не base64 ({type(exc).__name__})") from exc
    if len(raw) != 32:  # noqa: PLR2004 — AES-256
        raise ValueError(f"{source}: KEK обязан быть ровно 32 байта после base64-decode")
    return raw


def load_keks(cfg: Settings, env: Mapping[str, str] | None = None) -> dict[int, bytes]:
    """Версии KEK из окружения: ``AEGIS_KEK_V7`` и т.д.; ``AEGIS_KEK`` — активная версия.

    Старые версии остаются перечислимыми намеренно: rewrap и чтение прошлых записей требуют
    именно их. Ротация, «забывшая старый ключ», — это не ротация, а плановый шредер.
    """
    source = env if env is not None else os.environ
    out: dict[int, bytes] = {}
    for name, value in source.items():
        match = _KEK_ENV.match(name.strip().upper())
        if match and value and value.strip():
            out[int(match.group(1))] = _decode_kek(value.strip(), source=name)
    active = int(getattr(cfg, "crypto_key_version", 1) or 1)
    kek = getattr(cfg, "crypto_kek", None)
    if kek is not None and active not in out:
        secret = kek.get_secret_value() if hasattr(kek, "get_secret_value") else str(kek)
        if secret.strip():
            out[active] = _decode_kek(secret.strip(), source="AEGIS_KEK")
    return out


class BlobCipher:
    """Шифрование содержимого блоба DEK'ем; DEK завёрнут в активный KEK.

    Публичный контракт маленький: ``encrypt(bytes) -> (ciphertext, wrapped_dek, key_version)`` и
    ``decrypt(...) -> bytes``. Всё остальное — детали GCM, которые не должны сочиться в
    рекордер и retention.
    """

    def __init__(self, keks: Mapping[int, bytes], *, active_version: int | None = None) -> None:
        if not keks:
            raise ValueError("BlobCipher без ключей не имеет смысла: передайте пустой cipher")
        self._keks = {int(v): bytes(k) for v, k in keks.items()}
        self.active_version = int(active_version or max(self._keks))
        if self.active_version not in self._keks:
            raise ValueError(
                f"активная версия ключа {self.active_version} отсутствует в наборе: "
                "запишите её в env (AEGIS_KEK_V{}) или включите профиль vault".format(
                    self.active_version
                )
            )

    @staticmethod
    def fingerprint(kek: bytes) -> str:
        """Короткий отпечаток ключа для ``platform.master_keys`` — сам ключ не логируется."""
        return sha256_bytes(b"aegis:kek:" + kek).hex()[:16]

    # --- DEK lifecycle ---

    def new_dek(self) -> bytes:
        import secrets  # noqa: PLC0415 — один вызов CSPRNG на запись, бюджетно

        dek = secrets.token_bytes(32)  # noqa: S105 — не секрет в коде, это ключ данных
        return dek

    def wrap(self, dek: bytes, *, version: int | None = None) -> tuple[bytes, int]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        ver = int(version or self.active_version)
        kek = self._keks.get(ver)
        if kek is None:
            raise KeyShredded(f"KEK v{ver} недоступен: развернуть DEK нечем")
        nonce = os.urandom(_GCM_NONCE)
        sealed = AESGCM(kek).encrypt(nonce, dek, None)
        return _WRAP_PREFIX + ver.to_bytes(2, "big") + nonce + sealed, ver

    def unwrap(self, wrapped: bytes, *, key_version: int | None = None) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        if not wrapped.startswith(_WRAP_PREFIX):
            raise ValueError("wrapped_dek повреждён: нет префикса формата")
        body = wrapped[len(_WRAP_PREFIX) :]
        ver = int.from_bytes(body[:2], "big")  # noqa: PLR2004 — формат записи
        if key_version is not None and int(key_version) != ver:
            raise ValueError(f"key_version={key_version} не совпадает с обёрткой (v{ver})")
        kek = self._keks.get(ver)
        if kek is None:
            raise KeyShredded(f"KEK v{ver} недоступен: ключ уничтожен или не выдан")
        nonce, sealed = body[2 : 2 + _GCM_NONCE], body[2 + _GCM_NONCE :]
        return AESGCM(kek).decrypt(nonce, sealed, None)

    def rebind(self, keks: Mapping[int, bytes], *, active_version: int) -> None:
        """Переставить поколения ключей на живом объекте: cipher'ы живут долго (recorder,
        store), а ротация не имеет права требовать «пересоздай всё». Старые версии,
        оставшиеся в наборе, продолжают читать прошлые записи."""
        if not keks:
            raise ValueError("rebind с пустым набором: это шредер, а не ротация")
        self._keks = {int(v): bytes(k) for v, k in keks.items()}
        self.active_version = int(active_version)

    # --- content ---

    def encrypt(self, plaintext: bytes) -> tuple[bytes, bytes, int]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        dek = self.new_dek()
        wrapped, ver = self.wrap(dek)
        nonce = os.urandom(_GCM_NONCE)
        sealed = AESGCM(dek).encrypt(nonce, plaintext, None)
        return nonce + sealed, wrapped, ver

    def decrypt(
        self, ciphertext: bytes, wrapped_dek: bytes, *, key_version: int | None = None
    ) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        dek = self.unwrap(wrapped_dek, key_version=key_version)
        if len(ciphertext) <= _GCM_NONCE:
            raise ValueError("ciphertext короче nonce: блоб повреждён")
        return AESGCM(dek).decrypt(ciphertext[:_GCM_NONCE], ciphertext[_GCM_NONCE:], None)
