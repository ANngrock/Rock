"""Динамическое шифрование: цепь KEK-поколений, фоновый поворот, запечатанные строки и payload.

Зачем это поверх :mod:`aegis.platform.crypto` (там конверт DEK→KEK на запись): тот модуль
честно шифрует, но KEK живёт в env и меняется руками. Владелец попросил «шифрование по всем
фронтам, обновляющееся в фоне каждые две минуты» — это значит, что «свежесть ключа» перестаёт
быть административной процедурой и становится свойством системы. Реализация, которая делает это
без иллюзий:

* **цепь поколений.** Активный KEK — не «ключ из env, когда админ вспомнил», а
  ``gen``-й элемент HKDF-цепи от master-ключа: ``KEK(n) = HKDF(master, "aegis:vault:v{n}")``.
  Все процессы с одним master выводят ту же цепь — договорённость о «какой сейчас ключ» сводится
  к одному числу в таблице ``platform.vault_state``;
* **поворот = число, не подвиг.** Тик раз в ``crypto_rotate_sec`` (по умолчанию 120) поднимает
  ``gen``; новые записи заворачиваются новым KEK; фоновый sweeper (тот же
  :func:`aegis.governance.retention.rewrap_batch`) догоняет старые строки re-wrap'ом —
  содержимое и sha блобов не меняются, история остаётся историей;
* **grace по числу поколений.** Ключи старше ``gen - crypto_keep_generations + 1`` из активности
  выкидываются: Read-путь на мёртвых номерах честно получает :class:`KeyShredded`. Смысл
  ограничения — не «секретность ради секретности», а потолок того, что система помнит;
* **transparent fallback.** ``crypto_mode=off`` и отсутствие master — ровно прежнее поведение
  (открытый текст, ноль поломок на dev); запечатанное читается всегда, а «открытое при
  включённом шифровании» остаётся открытым — ротация не имеет права превращать чтение в падение.

Чего здесь НЕТ: «секретного канала» между процессами без общего секрета и шифрования того, что
уже едет по TLS (Telegram API, https-документы). Шифровать на своём канале то, что читает только
наш же сервер, — театр; фронты этого модуля — всё, что переживает процесс и лежит в БД/Redis:
блобы журнала, секреты коннекторов, строки парсера, история сессий, текст в vision-событиях,
payload моста личных чатов в NATS.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import structlog

from aegis.platform.crypto import BlobCipher, load_keks

if TYPE_CHECKING:
    from aegis.platform.config import Settings

__all__ = [
    "SEAL_PREFIX",
    "Vault",
    "derive_kek",
    "get_vault",
    "open_payload",
    "open_text",
    "seal_payload",
    "seal_text",
]

log = structlog.get_logger(__name__)

#: префикс запечатанной строки в текстовой колонке (тот же конверт, что у блобов, но одним полем)
SEAL_PREFIX = "aeg1s:"
_PAYLOAD_PREFIX = b"aeg1j:"
_INFO = "aegis:vault:v"


def derive_kek(master: bytes, gen: int) -> bytes:
    """KEK поколения ``gen`` из master: детерминированно, без хранения самих поколений."""
    if len(master) != 32:  # noqa: PLR2004 — 32 = размер AES-256, то же, что у env-KEK
        raise ValueError("master для цепи должен быть ровно 32 байта")
    if gen < 1:
        raise ValueError("поколения нумеруются с 1")
    from cryptography.hazmat.primitives.hashes import SHA256  # noqa: PLC0415
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415

    return HKDF(
        algorithm=SHA256(), length=32, salt=b"aegis:vault", info=f"{_INFO}{gen}".encode()
    ).derive(master)


# ---------- конверт для одной строки / одного payload ----------


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal_text(cipher: BlobCipher | None, plaintext: str) -> str:
    """Строка → одна текстовая колонка. Без ключарки возвращаем как есть (dev/off)."""
    if cipher is None or not plaintext:
        return plaintext
    ct, wrapped, ver = cipher.encrypt(plaintext.encode("utf-8"))
    body = json.dumps({"v": ver, "d": _b64u(wrapped), "c": _b64u(ct)}, separators=(",", ":"))
    return SEAL_PREFIX + body


def open_text(cipher: BlobCipher | None, stored: str) -> str:
    """Обратная операция: незапечатанное проходит насквозь, испорченное — ошибка, а не пустота."""
    if not stored or not stored.startswith(SEAL_PREFIX):
        return stored
    if cipher is None:
        raise KeyUnavailable("строка зашифрована, а ключарка недоступна (AEGIS_KEK?)")
    try:
        env = json.loads(stored[len(SEAL_PREFIX) :])
        dek = cipher.unwrap(_unb64u(env["d"]), key_version=int(env["v"]))
    except (ValueError, KeyError) as exc:
        raise ValueError("конверт строки повреждён") from exc
    # KeyShredded из unwrap идёт наверх как есть: «ключ уничтожен» — факт, а не повод для заглушки
    # unwrap() возвращает DEK: распаковка содержимого — на нём
    return _open_body(dek, _unb64u(env["c"]))


def _open_body(dek: bytes, ct: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    if len(ct) <= 12:  # noqa: PLR2004 — nonce
        raise ValueError("ciphertext короче nonce: блоб повреждён")
    return AESGCM(dek).decrypt(ct[:12], ct[12:], None).decode("utf-8")


class KeyUnavailable(RuntimeError):
    """Запечатанное нечем читать: ключарка выключена или поколение за чертой grace."""


def seal_payload(cipher: BlobCipher | None, payload: dict[str, Any]) -> bytes:
    """JSON для транспорта (NATS): та же упаковка, байтовый префикс. Без ключей — обычный JSON."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if cipher is None:
        return raw
    ct, wrapped, ver = cipher.encrypt(raw)
    env = json.dumps({"v": ver, "d": _b64u(wrapped), "c": _b64u(ct)}, separators=(",", ":"))
    return _PAYLOAD_PREFIX + env.encode("utf-8")


def open_payload(cipher: BlobCipher | None, raw: bytes) -> dict[str, Any]:
    if raw.startswith(_PAYLOAD_PREFIX):
        if cipher is None:
            raise KeyUnavailable("payload запечатан, ключарки нет")
        env = json.loads(raw[len(_PAYLOAD_PREFIX) :])
        dek = cipher.unwrap(_unb64u(env["d"]), key_version=int(env["v"]))
        data: Any = json.loads(_open_body(dek, _unb64u(env["c"])))
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload обязан быть JSON-объектом")
    return cast("dict[str, Any]", data)


# ---------- сам Vault ----------


@dataclass(slots=True)
class Vault:
    """Ключарка с живой цепью поколений. Один объект на процесс (``get_vault``)."""

    cfg: Settings
    master: bytes | None
    env_keks: dict[int, bytes]
    base_gen: int
    gen: int
    _cipher: BlobCipher | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _db_gen_at: float = field(default=0.0, repr=False)

    @property
    def cipher(self) -> BlobCipher | None:
        return self._cipher

    @property
    def dynamic(self) -> bool:
        """Есть ли чем крутить цепь (master + не выключенный режим)."""
        return self.master is not None and getattr(self.cfg, "crypto_mode", "auto") != "off"

    def keys(self) -> dict[int, bytes]:
        """Актуальный набор KEK: env-версии как есть + поколения цепи в окне grace."""
        out = dict(self.env_keks)
        keep = int(getattr(self.cfg, "crypto_keep_generations", 40))
        if self.dynamic and self.master is not None:
            for v in range(self.base_gen + 1, self.gen + 1):
                if v <= 0 or v < self.gen - keep + 1:
                    continue  # за чертой grace — не помним (записи там обязан был догнать sweeper)
                out.setdefault(v, derive_kek(self.master, v))
        return out

    def refresh(self) -> None:
        with self._lock:
            keys = self.keys()
            if not keys:
                self._cipher = None
                return
            if self._cipher is None:
                self._cipher = BlobCipher(keys, active_version=self.gen)
            else:
                self._cipher.rebind(keys, active_version=self.gen)

    async def sync_from_db(self) -> int:
        """Подтянуть gen из platform.vault_state (кэш на 20с): процессы пишут по одному числу."""
        now = time.monotonic()
        if now - self._db_gen_at < 20:  # noqa: PLR2004 — «достаточно часто для писателя»
            return self.gen
        self._db_gen_at = now
        try:
            from sqlalchemy import text  # noqa: PLC0415

            from aegis.platform.db import session

            async with session() as s:
                row = (
                    await s.execute(text("SELECT gen FROM platform.vault_state WHERE id = 1"))
                ).scalar()
            if row is not None:
                self.gen = max(int(row), self.base_gen)
                self.refresh()
        except Exception as exc:  # noqa: BLE001 — без БД живём на локальном gen (dev/тесты)
            log.debug("vault.db_gen_skipped", err=repr(exc)[:120])
        return self.gen

    async def rotate(self) -> dict[str, Any]:
        """Один поворот: поднять gen, затем re-wrap догнать. Порядок выбран ради непрерывности
        записи: строка, написанная «старым» gen между bump'ом и её догонкой, не теряет ничего —
        ключи в окне grace живы, а sweeper их переведёт."""
        await self.sync_from_db()
        report: dict[str, Any] = {"from": self.gen, "to": self.gen, "rewrapped": 0, "failed": 0}
        if not self.dynamic:
            report["skipped"] = (
                "шифрование выключено"
                if getattr(self.cfg, "crypto_mode", "auto") == "off"
                else "нет AEGIS_KEK"
            )
            return report
        report["bumped"] = await self._bump()
        report["to"] = self.gen
        # догоняем старые строки: батчами, с бюджетом за тик — БД не должна встать на re-wrap
        budget = max(1, int(getattr(self.cfg, "crypto_rewrap_budget", 2000)))
        done = failed = 0
        try:
            from aegis.governance.retention import rewrap_batch

            while done + failed < budget:
                batch = await rewrap_batch(self._cipher, limit=min(500, budget - done - failed))
                done += int(batch["rewrapped"])
                failed += int(batch["failed"])
                if int(batch["seen"]) == 0 or int(batch["rewrapped"]) + int(batch["failed"]) == 0:
                    break
            report["rewrapped"], report["failed"] = done, failed
        except Exception as exc:  # noqa: BLE001 — без БД re-wrap не нужен (in-proc фронт: kv/relay)
            log.debug("vault.rewrap_skipped", err=repr(exc)[:120])
        return report

    async def _bump(self) -> bool:
        """CAS-подъём gen в БД; без БД — локальный подъём (тогда и чтецы те же процессы)."""
        self.gen += 1
        self.refresh()
        try:
            from sqlalchemy import text  # noqa: PLC0415

            from aegis.platform.db import session

            async with session() as s:
                got = (
                    await s.execute(
                        text(
                            "UPDATE platform.vault_state SET gen = :want, prev_gen = gen,"
                            " rotated_at = now(), note = :note WHERE id = 1 AND gen < :want"
                            " RETURNING gen"
                        ),
                        {"want": self.gen, "note": f"pid {os.getpid()} @{int(time.time())}"},
                    )
                ).scalar()
                await s.commit()
                cur = (
                    None
                    if got is not None
                    else (
                        await s.execute(text("SELECT gen FROM platform.vault_state WHERE id = 1"))
                    ).scalar()
                )  # got None — кто-то успел дальше: берём его gen, строки догонит sweeper
            if cur is not None and int(cur) > self.gen:
                self.gen = int(cur)
                self.refresh()
            return True
        except Exception as exc:  # noqa: BLE001 — нет таблицы/БД: цепь крутится внутри процесса
            log.debug("vault.bump_local_only", err=repr(exc)[:120])
            return True

    async def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": getattr(self.cfg, "crypto_mode", "auto"),
            "master": "есть" if self.master is not None else "нет",
            "gen": self.gen,
            "base_gen": self.base_gen,
            "env_versions": sorted(self.env_keks),
            "keep": int(getattr(self.cfg, "crypto_keep_generations", 40)),
            "rotate_sec": int(getattr(self.cfg, "crypto_rotate_sec", 120)),
            "active_versions": sorted(self.keys()),
        }
        try:
            from sqlalchemy import text  # noqa: PLC0415

            from aegis.platform.db import session

            async with session() as s:
                lag = (
                    await s.execute(
                        text(
                            "SELECT count(*) FROM platform.blobs WHERE content_cipher <> 'none'"
                            " AND key_version < :g"
                        ),
                        {"g": self.gen},
                    )
                ).scalar()
            out["lagging_blobs"] = int(lag or 0)
        except Exception:  # noqa: BLE001 — статус работает и без БД
            out["lagging_blobs"] = None
        return out


_vaults: dict[int, Vault] = {}


def get_vault(cfg: Settings) -> Vault | None:
    """Singleton на процесс; ``crypto_mode=off`` — None (никакой оверхед в тесты и dev)."""
    if getattr(cfg, "crypto_mode", "auto") == "off":
        return None
    key = id(cfg)
    vault = _vaults.get(key)
    if vault is None:
        master: bytes | None = None
        kek = getattr(cfg, "crypto_kek", None)
        if kek is not None:
            raw = kek.get_secret_value() if hasattr(kek, "get_secret_value") else str(kek)
            try:
                blob = base64.b64decode(raw.strip(), validate=True)
                master = blob if len(blob) == 32 else None  # noqa: PLR2004
            except Exception:  # noqa: BLE001
                master = None
        env_keks = load_keks(cfg)
        gen0 = int(getattr(cfg, "crypto_key_version", 1) or 1)
        # floor цепи — выше ВСЕХ env-версий: ручные ключи остаются руками оператора, цепь их
        # не перекрывает и не выкидывает; активность по умолчанию = ровно прошлое поведение
        base = max([*env_keks, gen0])
        vault = Vault(cfg=cfg, master=master, env_keks=env_keks, base_gen=base, gen=gen0)
        vault.refresh()
        _vaults[key] = vault
    return vault


def process_cipher(cfg: Settings) -> BlobCipher | None:
    """Сокращение для писателей: актуальный cipher процесса или None (open-режим)."""
    vault = get_vault(cfg)
    return vault.cipher if vault is not None else None
