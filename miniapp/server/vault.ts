/**
 * Динамическое шифрование на стороне мини-аппа: тот же конверт, что печатает python
 * (`aegis.platform.vault`), поэтому след зрения читается ботом и переживает повороты ключа.
 *
 * Совпадение байт-в-байт проверено векторами: miniapp/testdata/vault_vectors.json генерируется
 * python'ом и вскрывается здесь, а reverse-поле генерируется этим модулем с фиксированными
 * nonce и вскрывается python-тестом. Формат (его нельзя «почти» повторить — либо он, либо мусор):
 *
 *   aeg1s:{"v":gen,"d":b64u("aeg1:"+ver(2B)+nonce12+GCM(kek,dek)),"c":b64u(nonce12+GCM(dek,pt))}
 *   kek  = HKDF-SHA256(master, salt="aegis:vault", info="aegis:vault:v{n}", 32B)
 */

import { createCipheriv, createDecipheriv, hkdfSync, randomBytes } from "node:crypto";

const SEAL_PREFIX = "aeg1s:";
const WRAP_PREFIX = "aeg1:";
const NONCE_LEN = 12;

export type KekRing = Map<number, Buffer>;

function b64u(raw: Buffer): string {
  return raw.toString("base64url");
}

function unb64u(text: string): Buffer {
  return Buffer.from(text, "base64url");
}

/** KEK поколения из master-KEK — дословно python derive_kek. */
export function deriveKek(master: Buffer, gen: number): Buffer {
  if (master.length !== 32) throw new Error("master для цепи должен быть ровно 32 байта");
  if (gen < 1) throw new Error("поколения нумеруются с 1");
  const out = hkdfSync("sha256", master, Buffer.from("aegis:vault"), Buffer.from(`aegis:vault:v${gen}`), 32);
  return Buffer.from(new Uint8Array(out));
}

/** Кольцо KEK из env: AEGIS_KEK — master цепи; AEGIS_KEK_V{n} — точечные env-ключи. */
export function keksFromEnv(
  env: NodeJS.ProcessEnv,
  gen: number,
  keep: number,
): { cipher: KekRing | null; master: Buffer | null } {
  const single = (env.AEGIS_KEK ?? "").trim();
  const out: KekRing = new Map();
  let master: Buffer | null = null;
  if (single) {
    try {
      master = Buffer.from(single, "base64");
    } catch {
      master = null;
    }
    if (master && master.length === 32) {
      for (let v = Math.max(1, gen - keep + 1); v <= gen; v++) out.set(v, deriveKek(master, v));
    } else {
      master = null;
    }
  }
  for (const [name, value] of Object.entries(env)) {
    const m = /^AEGIS_KEK_V(\d+)$/.exec(name);
    const raw = (value ?? "").trim();
    if (m && raw) {
      try {
        const key = Buffer.from(raw, "base64");
        if (key.length === 32) out.set(Number(m[1]), key);
      } catch {
        /* битый ключ — как будто его нет: честное открытие открытым текстом лучше паники */
      }
    }
  }
  return { cipher: out.size ? out : null, master };
}

function gcmSeal(key: Buffer, nonce: Buffer, plain: Buffer): Buffer {
  const c = createCipheriv("aes-256-gcm", key, nonce);
  return Buffer.concat([c.update(plain), c.final(), c.getAuthTag()]);
}

/** sealed здесь — УЖЕ без префиксного nonce: GCM(ct||tag), nonce передаётся отдельно. */
function gcmOpen(key: Buffer, nonce: Buffer, sealed: Buffer): Buffer {
  if (sealed.length <= 16) throw new Error("ciphertext короче тега");
  const d = createDecipheriv("aes-256-gcm", key, nonce);
  d.setAuthTag(sealed.subarray(sealed.length - 16));
  return Buffer.concat([d.update(sealed.subarray(0, sealed.length - 16)), d.final()]);
}

export interface SealOptions {
  /** Детерминированные nonce только для векторов совместимости; в проде — undefined. */
  nonce?: (len: number) => Buffer;
  dek?: Buffer;
}

/** Строка → колонка. Пустая строка или нет ключей — как есть (то же, что у python seal_text). */
export function sealText(
  keks: KekRing | null,
  gen: number,
  plaintext: string,
  opts: SealOptions = {},
): string {
  if (!keks || keks.size === 0 || !plaintext) return plaintext;
  const kek = keks.get(gen) ?? keks.get(Math.max(...keks.keys()));
  if (!kek) return plaintext;
  const rnd = opts.nonce ?? ((n: number) => randomBytes(n));
  const dek = opts.dek ?? randomBytes(32);
  const wNonce = rnd(NONCE_LEN);
  const ver = Buffer.alloc(2);
  ver.writeUInt16BE(gen);
  const wrapped = Buffer.concat([Buffer.from(WRAP_PREFIX, "ascii"), ver, wNonce, gcmSeal(kek, wNonce, dek)]);
  const cNonce = rnd(NONCE_LEN);
  const body = Buffer.concat([cNonce, gcmSeal(dek, cNonce, Buffer.from(plaintext, "utf8"))]);
  const env = JSON.stringify({ v: gen, d: b64u(wrapped), c: b64u(body) });
  return SEAL_PREFIX + env;
}

/** Колонка → строка. Незапечатанное проходит насквозь; испорченное — бросает, не возвращает пустоту. */
export function openText(keks: KekRing | null, stored: string): string {
  if (!stored || !stored.startsWith(SEAL_PREFIX)) return stored;
  if (!keks || keks.size === 0) throw new Error("строка запечатана, а ключей нет (AEGIS_KEK?)");
  const env = JSON.parse(stored.slice(SEAL_PREFIX.length)) as { v: number; d: string; c: string };
  const wrapped = unb64u(env.d);
  if (!wrapped.subarray(0, WRAP_PREFIX.length).equals(Buffer.from(WRAP_PREFIX, "ascii"))) {
    throw new Error("wrapped_dek повреждён: нет префикса формата");
  }
  const wbody = wrapped.subarray(WRAP_PREFIX.length);
  const ver = wbody.readUInt16BE(0);
  const kek = keks.get(ver);
  if (!kek) throw new Error(`KEK v${ver} недоступен: ключ уничтожен или не выдан`);
  const dek = gcmOpen(kek, wbody.subarray(2, 2 + NONCE_LEN), wbody.subarray(2 + NONCE_LEN));
  const ct = unb64u(env.c);
  return gcmOpen(dek, ct.subarray(0, NONCE_LEN), ct.subarray(NONCE_LEN)).toString("utf8");
}

export interface LiveVault {
  keks(): KekRing | null;
  gen(): number;
  /** принять поколение сверху (poller читает platform.vault_state — ту же строку, что крутит бот) */
  setGen(gen: number): void;
  stop(): void;
}

/**
 * Живое кольцо: строим из master по текущему gen, а gen подтягиваем из БД — иначе мини-апп
 * печатал бы «своим» поколением и разъезжался с ботом. Без master — null: открытая запись
 * честнее паники, ровно как в python (crypto без KEK = passthrough).
 */
export function makeVault(
  cfg: { cryptoKek: string; cryptoKeyVersion: number; cryptoKeep: number; databaseUrl?: string },
): LiveVault | null {
  if (!cfg.cryptoKek) return null;
  let master: Buffer;
  try {
    master = Buffer.from(cfg.cryptoKek, "base64");
  } catch {
    return null;
  }
  if (master.length !== 32) return null;
  let gen = Math.max(1, cfg.cryptoKeyVersion || 1);
  let ring: KekRing | null = new Map();
  const rebuild = () => {
    const m: KekRing = new Map();
    for (let v = Math.max(1, gen - cfg.cryptoKeep + 1); v <= gen; v++) m.set(v, deriveKek(master, v));
    ring = m;
  };
  rebuild();
  let timer: ReturnType<typeof setInterval> | null = null;
  if (cfg.databaseUrl) {
    void (async () => {
      try {
        const { Pool } = await import("pg");
        const pool = new Pool({ connectionString: cfg.databaseUrl, max: 1 });
        const poll = async () => {
          try {
            const r = await pool.query("SELECT gen FROM platform.vault_state WHERE id = 1");
            const g = Number((r.rows as { gen: number }[])[0]?.gen ?? 0);
            if (g > gen) {
              gen = g;
              rebuild();
            }
          } catch {
            /* БД — не критичный путь: печатаем последним известным поколением */
          }
        };
        await poll();
        timer = setInterval(() => void poll(), 30_000);
        timer.unref?.();
      } catch {
        /* нет драйвера/БД — живём на env-поколении */
      }
    })();
  }
  return {
    keks: () => ring,
    gen: () => gen,
    setGen: (g: number) => {
      if (g > gen) {
        gen = g;
        rebuild();
      }
    },
    stop: () => {
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}
