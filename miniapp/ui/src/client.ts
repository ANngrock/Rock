/**
 * Клиент сокета: одно соединение, автопереподключение, ровно один init.
 * Никакой «умной» логики — состояние рисует App, здесь только труба.
 */

export type ServerMsg =
  | { t: "hello"; engine: string; vision: boolean; db: boolean }
  | { t: "status"; state: string; frames: number; dropped: number; analyses: number; lastMs: number }
  | { t: "analysis"; text: string; ok: boolean; ms: number; kind: string }
  | { t: "answer"; text: string; ok: boolean; ms: number }
  | { t: "fatal"; error: string }
  | { t: "pong" };

export class VisionLink {
  private ws: WebSocket | null = null;
  private retry = 0;
  private dead = false;

  constructor(
    private readonly onMsg: (m: ServerMsg) => void,
    private readonly onState: (up: boolean) => void,
  ) {}

  connect(): void {
    if (this.dead) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.onState(true);
      ws.send(JSON.stringify({ t: "init", initData: initDataFromTelegram() }));
    };
    ws.onmessage = (ev) => {
      try {
        this.onMsg(JSON.parse(String(ev.data)) as ServerMsg);
      } catch {
        /* чужие байты игнорируем: протокол сервер всегда отвечает в схеме */
      }
    };
    ws.onclose = () => {
      this.onState(false);
      if (this.dead) return;
      const wait = Math.min(15_000, 500 * 2 ** this.retry++);
      setTimeout(() => this.connect(), wait);
    };
    ws.onerror = () => ws.close();
  }

  send(msg: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  close(): void {
    this.dead = true;
    this.ws?.close();
  }
}

/**
 * initData даёт Telegram. Вне Telegram (локальное превью) собираем dev-строку —
 * сервер примет её только при MINIAPP_ALLOW_UNVERIFIED=1, в проде такой ссылки нет.
 */
export function initDataFromTelegram(): string {
  const tg = (window as unknown as { Telegram?: { WebApp?: { initData?: string } } }).Telegram?.WebApp;
  if (tg?.initData) return tg.initData;
  const p = new URLSearchParams();
  p.set("user", JSON.stringify({ id: 1, first_name: "Превью", username: "dev" }));
  p.set("auth_date", String(Math.floor(Date.now() / 1000)));
  return p.toString();
}
