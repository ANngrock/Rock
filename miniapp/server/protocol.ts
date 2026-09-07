/**
 * Протокол сокета — узкое горлышко: всё, что приходит от браузера, проходит через
 * `parseClientMessage` и не бывает «доверенным объектом» напрямую.
 */

export const SESSION_MODES = ["stream", "ask", "brief"] as const;
export type SessionMode = (typeof SESSION_MODES)[number];

export type ClientMessage =
  | { t: "init"; initData: string }
  | { t: "start"; mode: SessionMode }
  | { t: "stop" }
  | { t: "frame"; img: string; note?: string }
  | { t: "ask"; text: string }
  | { t: "ping" };

export type ServerMessage =
  | { t: "hello"; engine: string; vision: boolean; db: boolean }
  | { t: "status"; state: string; frames: number; dropped: number; analyses: number; lastMs: number }
  | { t: "analysis"; text: string; ok: boolean; ms: number; kind: "stream" | "brief" }
  | { t: "answer"; text: string; ok: boolean; ms: number }
  | { t: "fatal"; error: string };

const MAX_IMG = 2_000_000; // ~1.5 МБ JPEG в base64 — больше камера и не выдаст при нашем масштабе
const MAX_TEXT = 2000;

export function parseClientMessage(raw: string): ClientMessage | null {
  let obj: unknown;
  try {
    obj = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof obj !== "object" || obj === null) return null;
  const m = obj as Record<string, unknown>;
  switch (m.t) {
    case "init":
      return typeof m.initData === "string" && m.initData.length <= MAX_TEXT
        ? { t: "init", initData: m.initData }
        : null;
    case "start": {
      const mode = SESSION_MODES.includes(m.mode as SessionMode) ? (m.mode as SessionMode) : "stream";
      return { t: "start", mode };
    }
    case "stop":
      return { t: "stop" };
    case "frame": {
      if (typeof m.img !== "string" || m.img.length === 0 || m.img.length > MAX_IMG) return null;
      const note = typeof m.note === "string" ? m.note.slice(0, MAX_TEXT) : undefined;
      return { t: "frame", img: m.img, note };
    }
    case "ask":
      return typeof m.text === "string" && m.text.trim()
        ? { t: "ask", text: m.text.trim().slice(0, MAX_TEXT) }
        : null;
    case "ping":
      return { t: "ping" };
    default:
      return null;
  }
}

export function encode(msg: ServerMessage): string {
  return JSON.stringify(msg);
}
