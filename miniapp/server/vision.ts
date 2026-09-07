/**
 * Обращение к модели зрения (OpenAI-совместимый /chat/completions).
 *
 * Правило деградации то же, что во всём Aegis: без ключа/эндпоинта не врать —
 * ok:false с внятной причиной, а не пустой ответ «как будто проанализировали».
 */

import type { MiniappConfig } from "./config.ts";
import { visionReady } from "./config.ts";

export interface AnalysisResult {
  text: string;
  ok: boolean;
  ms: number;
}

const SYSTEM_PROMPT = [
  "Ты — зрение приватного ассистента Aegis. Тебе приходит кадр с камеры пользователя.",
  "Отвечай по-русски, 1–3 коротких предложения: что происходит, где люди/животные, что двигается,",
  "есть ли риск (огонь, открытая дверь, падение, незнакомец у двери). Не выдумывай детали,",
  "которых не видно. Если кадр тёмный/смазанный — так и скажи одним предложением.",
].join(" ");

export interface FrameRequest {
  /** jpeg в base64 (без data: префикса) */
  img: string;
  /** вопрос пользователя, если это «спросить», а не поток */
  question?: string;
  /** последние сводки потока — чтобы стрим не начинал каждый раз с чистого листа */
  recent: string[];
}

export function buildMessages(req: FrameRequest): unknown[] {
  const ctx = req.recent.length
    ? `\nПредыдущие наблюдения (для непрерывности): ${req.recent.join(" → ")}`
    : "";
  const task = req.question
    ? `Вопрос пользователя: «${req.question}». Ответь по кадру, коротко и по делу.`
    : "Опиши, что происходит в кадре прямо сейчас.";
  return [
    { role: "system", content: SYSTEM_PROMPT + ctx },
    {
      role: "user",
      content: [
        { type: "text", text: task },
        { type: "image_url", image_url: { url: `data:image/jpeg;base64,${req.img}` } },
      ],
    },
  ];
}

export async function analyzeFrame(cfg: MiniappConfig, req: FrameRequest): Promise<AnalysisResult> {
  if (!visionReady(cfg)) {
    return {
      ok: false,
      ms: 0,
      text: "модель зрения не подключена: задай VISION_BASE_URL / VISION_MODEL / VISION_API_KEY",
    };
  }
  const t0 = Date.now();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 20_000);
  try {
    const res = await fetch(`${cfg.visionBaseUrl}/chat/completions`, {
      method: "POST",
      signal: ctrl.signal,
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${cfg.visionApiKey}`,
      },
      body: JSON.stringify({
        model: cfg.visionModel,
        messages: buildMessages(req),
        max_tokens: 220,
        temperature: 0.3,
      }),
    });
    if (!res.ok) {
      const body = (await res.text()).slice(0, 300);
      return { ok: false, ms: Date.now() - t0, text: `модель ответила ${res.status}: ${body}` };
    }
    const data = (await res.json()) as {
      choices?: { message?: { content?: string } }[];
    };
    const text = data.choices?.[0]?.message?.content?.trim();
    if (!text) return { ok: false, ms: Date.now() - t0, text: "модель вернула пустой ответ" };
    return { ok: true, ms: Date.now() - t0, text: text.slice(0, 900) };
  } catch (err) {
    const why = (err as Error).name === "AbortError" ? "таймаут 20s" : String((err as Error).message).slice(0, 200);
    return { ok: false, ms: Date.now() - t0, text: `зрение недоступно: ${why}` };
  } finally {
    clearTimeout(timer);
  }
}
