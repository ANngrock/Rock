/**
 * Проверка initData Telegram Mini App.
 *
 * Схема (docs.telegram.org, «Validate data for Mini App»): secret_key =
 * HMAC_SHA256(key="WebAppData", data=bot_token); hash — HMAC_SHA256(secret_key,
 * отсортированные строки "key=value" всех полей кроме hash). Значения — после
 * URL-декодирования: браузер шлёт percent-encoded, хэш считается по декодированным.
 *
 * Без BOT_TOKEN проверка невозможна в принципе; тогда разрешаем только явный
 * локальный обход (MINIAPP_ALLOW_UNVERIFIED=1) — «не проверено» и «подделано» не должны
 * выглядеть одинаково.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

export interface InitDataUser {
  id: number;
  first_name?: string;
  username?: string;
  [k: string]: unknown;
}

export interface VerifiedInitData {
  user: InitDataUser | null;
  /** «webapp» или «startapp» — откуда запускают; на доступ влияет только user.id */
  startParam: string;
  raw: Record<string, string>;
}

export function parseFields(initData: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of new URLSearchParams(initData)) out[k] = v;
  return out;
}

export function dataCheckString(fields: Record<string, string>): string {
  return Object.entries(fields)
    .filter(([k]) => k !== "hash")
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

export function secretKeyFor(botToken: string): Buffer {
  return createHmac("sha256", "WebAppData").update(botToken, "utf8").digest();
}

export function verifyInitData(
  initData: string,
  botToken: string,
  opts: { allowUnverified?: boolean; maxAgeSec?: number } = {},
): { ok: boolean; reason?: string; data?: VerifiedInitData } {
  const fields = parseFields(initData);
  const data: VerifiedInitData = {
    user: null,
    startParam: fields.start_param ?? "",
    raw: fields,
  };
  try {
    if (fields.user) data.user = JSON.parse(fields.user) as InitDataUser;
  } catch {
    return { ok: false, reason: "поле user не читается как JSON" };
  }

  if (!botToken) {
    if (!opts.allowUnverified) {
      return { ok: false, reason: "сервер не знает TELEGRAM_BOT_TOKEN — проверка невозможна" };
    }
    return { ok: true, data }; // локальная разработка: доверяем явно, не «случайно»
  }

  const hash = fields.hash ?? "";
  if (!hash) return { ok: false, reason: "нет hash" };
  const expected = createHmac("sha256", secretKeyFor(botToken))
    .update(dataCheckString(fields), "utf8")
    .digest("hex");
  const a = Buffer.from(hash, "hex");
  const b = Buffer.from(expected, "hex");
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    return { ok: false, reason: "подпись не совпала — данные не от Telegram" };
  }
  const authAge = Number(fields.auth_date ?? 0);
  const maxAge = opts.maxAgeSec ?? 86_400;
  if (authAge > 0 && Math.floor(Date.now() / 1000) - authAge > maxAge) {
    return { ok: false, reason: "сессия протухла (auth_date слишком старый)" };
  }
  if (!data.user) return { ok: false, reason: "в данных нет пользователя" };
  return { ok: true, data };
}

/** Подделка initData для тестов и локального прогона — ровно та же математика, что у Telegram. */
export function signInitData(fields: Record<string, string>, botToken: string): string {
  const hash = createHmac("sha256", secretKeyFor(botToken))
    .update(dataCheckString(fields), "utf8")
    .digest("hex");
  const q = new URLSearchParams({ ...fields, hash });
  return q.toString();
}
