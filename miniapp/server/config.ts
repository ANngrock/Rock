/** Конфигурация мини-аппа: только окружение, без файлов — как в остальной системе. */

export interface MiniappConfig {
  port: number;
  botToken: string;
  ownerTgId: string;
  /** локальная разработка вне Telegram: принимать initData без проверки подписи */
  allowUnverified: boolean;
  /** OpenAI-совместимый эндпоинт зрения; пусто — честная деградация «модель не подключена» */
  visionBaseUrl: string;
  visionModel: string;
  visionApiKey: string;
  /** максимум кадров в секунду на сессию (защита от «пожара») */
  fpsLimit: number;
  /** секунды, в течение которых один ответ зрения переиспользуется как «последний анализ» */
  frameTtlSec: number;
  databaseUrl: string;
  pushToOwner: boolean;
  uiDist: string;
}

function intEnv(name: string, dflt: number): number {
  const raw = process.env[name] ?? "";
  if (!raw) return dflt;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0) throw new Error(`${name} должен быть положительным числом, а не "${raw}"`);
  return n;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): MiniappConfig {
  return {
    port: intEnv("PORT", 8790),
    botToken: (env.TELEGRAM_BOT_TOKEN ?? "").trim(),
    ownerTgId: (env.TELEGRAM_OWNER_ID ?? "").trim(),
    allowUnverified: env.MINIAPP_ALLOW_UNVERIFIED === "1",
    visionBaseUrl: (env.VISION_BASE_URL ?? "").trim().replace(/\/+$/, ""),
    visionModel: (env.VISION_MODEL ?? "").trim(),
    visionApiKey: (env.VISION_API_KEY ?? "").trim(),
    fpsLimit: intEnv("VISION_FPS_LIMIT", 2),
    frameTtlSec: intEnv("VISION_FRAME_TTL", 20),
    // python владеет схемой; мы только вставляем. "+asyncpg" — из общего DATABASE_URL
    databaseUrl: (env.DATABASE_URL ?? "").replace("+asyncpg", "").trim(),
    pushToOwner: env.VISION_PUSH_TO_OWNER === "1",
    uiDist: (env.MINIAPP_UI_DIST ?? "").trim(),
  };
}

export function visionReady(cfg: MiniappConfig): boolean {
  return Boolean(cfg.visionBaseUrl && cfg.visionModel && cfg.visionApiKey);
}
