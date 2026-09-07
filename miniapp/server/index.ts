/**
 * Vision-сервер мини-аппа: HTTP (статика UI + healthz) и WebSocket (поток).
 *
 * Рамки, в которых всё живёт:
 *  * доступ — только через проверенный initData (или явный локальный обход);
 *  * кадры существуют секунды: в БД летят сводки и события, пиксели — никогда;
 *  * DDL схемы vision принадлежит python/alembic, здесь только вставки;
 *  * нет модели или БД — приложение работает и честно об этом говорит.
 */

import { createServer, type ServerResponse } from "node:http";
import { createReadStream, existsSync } from "node:fs";
import { stat } from "node:fs/promises";
import path from "node:path";
import { WebSocketServer, type WebSocket } from "ws";

import { loadConfig, visionReady, type MiniappConfig } from "./config.ts";
import { verifyInitData } from "./initdata.ts";
import { TokenBucket } from "./ratelimit.ts";
import { encode, parseClientMessage, type ServerMessage, type SessionMode } from "./protocol.ts";
import { analyzeFrame } from "./vision.ts";
import { makeVault } from "./vault.ts";
import { VisionStore } from "./store.ts";

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".json": "application/json",
  ".woff2": "font/woff2",
};

interface ConnState {
  tgUserId: number | null;
  name: string;
  mode: SessionMode;
  sessionId: string | null;
  running: boolean;
  lastFrame: { img: string; at: number } | null;
  recent: string[];
  stats: { frames: number; dropped: number; analyses: number; lastMs: number };
  bucket: TokenBucket;
  busy: boolean;
}

export function makeState(cfg: MiniappConfig): ConnState {
  return {
    tgUserId: null,
    name: "",
    mode: "stream",
    sessionId: null,
    running: false,
    lastFrame: null,
    recent: [],
    stats: { frames: 0, dropped: 0, analyses: 0, lastMs: 0 },
    bucket: new TokenBucket(cfg.fpsLimit, Math.max(1, Math.floor(cfg.fpsLimit))),
    busy: false,
  };
}

function send(ws: WebSocket, msg: ServerMessage): void {
  if (ws.readyState === ws.OPEN) ws.send(encode(msg));
}

export class VisionHub {
  readonly store: VisionStore;
  private readonly cfg: MiniappConfig;

  constructor(cfg: MiniappConfig) {
    this.cfg = cfg;
    this.store = new VisionStore(cfg.databaseUrl, makeVault(cfg));
  }

  async init(): Promise<void> {
    await this.store.init();
  }

  uiDir(): string {
    const guess = [
      this.cfg.uiDist,
      path.join(import.meta.dirname, "..", "ui", "dist"),
      path.join(process.cwd(), "ui", "dist"),
    ].filter(Boolean);
    for (const dir of guess) if (existsSync(path.join(dir, "index.html"))) return dir;
    return "";
  }

  handleHttp(req: import("node:http").IncomingMessage, res: ServerResponse): void {
    const url = new URL(req.url ?? "/", "http://internal");
    if (url.pathname === "/healthz") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          ok: true,
          vision: visionReady(this.cfg),
          db: this.store.active,
          engine: this.cfg.visionModel || "нет",
        }),
      );
      return;
    }
    const dir = this.uiDir();
    if (!dir) {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(
        "<!doctype html><meta charset=utf-8><title>Aegis Vision</title><body style=\"font:16px system-ui;" +
          "background:#0b0d12;color:#e8eaf0;padding:40px\"><h2>UI не собран</h2><p>В <code>miniapp/</code>" +
          ": <code>npm install &amp;&amp; npm run build</code>, затем перезапустить сервер.</p>",
      );
      return;
    }
    this.serveStatic(dir, url.pathname, res);
  }

  private serveStatic(dir: string, pathname: string, res: ServerResponse): void {
    const rel = pathname === "/" ? "index.html" : pathname.slice(1);
    const file = path.normalize(path.join(dir, rel));
    if (!file.startsWith(dir)) {
      res.writeHead(403).end();
      return;
    }
    void (async () => {
      try {
        const info = await stat(file);
        if (!info.isFile()) throw new Error("not a file");
        res.writeHead(200, {
          "content-type": MIME[path.extname(file)] ?? "application/octet-stream",
          "cache-control": file.includes("assets/") ? "public, max-age=31536000, immutable" : "no-cache",
        });
        createReadStream(file).pipe(res);
      } catch {
        // SPA: всё, чего нет на диске, — фронт роутит сам
        const html = await stat(path.join(dir, "index.html")).catch(() => null);
        if (!html) {
          res.writeHead(404).end();
          return;
        }
        res.writeHead(200, { "content-type": MIME[".html"] });
        createReadStream(path.join(dir, "index.html")).pipe(res);
      }
    })();
  }

  attach(): WebSocketServer {
    const wss = new WebSocketServer({ noServer: true });
    wss.on("connection", (ws: WebSocket) => {
      const st = makeState(this.cfg);
      ws.on("message", (buf: Buffer) => {
        void this.onMessage(ws, st, buf.toString("utf8"));
      });
      ws.on("close", () => {
        if (st.sessionId) void this.store.close(st.sessionId, "клиент отключился");
      });
    });
    return wss;
  }

  private async onMessage(ws: WebSocket, st: ConnState, raw: string): Promise<void> {
    const msg = parseClientMessage(raw);
    if (!msg) return send(ws, { t: "fatal", error: "непонятное сообщение" });
    if (msg.t === "ping") return void ws.send('{"t":"pong"}');

    if (msg.t === "init") {
      const v = verifyInitData(msg.initData, this.cfg.botToken, {
        allowUnverified: this.cfg.allowUnverified,
      });
      if (!v.ok) return send(ws, { t: "fatal", error: `доступ запрещён: ${v.reason}` });
      st.tgUserId = v.data!.user?.id ?? 0;
      st.name = v.data!.user?.username ?? v.data!.user?.first_name ?? "гость";
      if (!st.tgUserId) return send(ws, { t: "fatal", error: "в initData нет идентификатора" });
      return send(ws, {
        t: "hello",
        engine: this.cfg.visionModel || "нет",
        vision: visionReady(this.cfg),
        db: this.store.active,
      });
    }
    if (st.tgUserId === null) return send(ws, { t: "fatal", error: "сначала init" });

    if (msg.t === "start") {
      st.mode = msg.mode;
      st.running = true;
      st.sessionId = await this.store.openSession(st.tgUserId, msg.mode, this.cfg.visionModel);
      await this.store.event(st.sessionId, "note", `сессия: режим ${msg.mode}, пользователь ${st.name}`, true, null);
      this.status(ws, st);
      return;
    }
    if (msg.t === "stop") {
      st.running = false;
      if (st.sessionId) {
        await this.store.event(
          st.sessionId,
          "note",
          `стоп: кадров ${st.stats.frames}, анализов ${st.stats.analyses}, отброшено ${st.stats.dropped}`,
          true,
          null,
        );
      }
      this.status(ws, st);
      return;
    }
    if (msg.t === "frame") {
      st.lastFrame = { img: msg.img, at: Date.now() };
      if (!st.running) return;
      if (!st.bucket.allow()) {
        st.stats.dropped += 1;
        return; // тише воды: кадр лишний — поток продолжится следующим
      }
      st.stats.frames += 1;
      if (st.busy) {
        st.stats.dropped += 1;
        return; // один анализ за раз — очередь из «последнего кадра» в модели не нужно
      }
      if (st.mode === "ask") return this.status(ws, st); // кадры копим, анализируем по вопросу
      st.busy = true;
      try {
        const res = await analyzeFrame(this.cfg, { img: msg.img, recent: st.recent });
        st.busy = false;
        st.stats.analyses += 1;
        st.stats.lastMs = res.ms;
        if (res.ok) {
          st.recent.push(res.text.slice(0, 160));
          if (st.recent.length > 2) st.recent.shift();
        }
        await this.store.event(st.sessionId, "frame", null, true, res.ms);
        await this.store.event(st.sessionId, "analysis", res.text, res.ok, res.ms);
        send(ws, { t: "analysis", text: res.text, ok: res.ok, ms: res.ms, kind: "stream" });
        this.status(ws, st);
        if (st.mode === "brief") st.running = false; // «глянь один раз» — и стоп
        if (res.ok && this.cfg.pushToOwner) void this.pushToOwner(res.text);
      } finally {
        st.busy = false;
      }
      return;
    }
    if (msg.t === "ask") {
      await this.store.event(st.sessionId, "question", msg.text, true, null);
      const fresh =
        st.lastFrame && Date.now() - st.lastFrame.at <= this.cfg.frameTtlSec * 1000 ? st.lastFrame : null;
      if (!fresh) {
        const why = st.lastFrame
          ? "последний кадр устарел — включи камеру и повтори вопрос"
          : "кадров ещё не было — включи камеру";
        await this.store.event(st.sessionId, "answer", why, false, null);
        return send(ws, { t: "answer", text: why, ok: false, ms: 0 });
      }
      const res = await analyzeFrame(this.cfg, { img: fresh.img, question: msg.text, recent: [] });
      await this.store.event(st.sessionId, "answer", res.text, res.ok, res.ms);
      st.stats.analyses += 1;
      st.stats.lastMs = res.ms;
      send(ws, { t: "answer", text: res.text, ok: res.ok, ms: res.ms });
      this.status(ws, st);
    }
  }

  private status(ws: WebSocket, st: ConnState): void {
    send(ws, {
      t: "status",
      state: st.running ? `стрим: ${st.mode}` : "ожидание",
      frames: st.stats.frames,
      dropped: st.stats.dropped,
      analyses: st.stats.analyses,
      lastMs: st.stats.lastMs,
    });
  }

  private async pushToOwner(text: string): Promise<void> {
    if (!this.cfg.botToken || !this.cfg.ownerTgId) return;
    try {
      await fetch(`https://api.telegram.org/bot${this.cfg.botToken}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: this.cfg.ownerTgId, text: `📷 Зрение: ${text}`.slice(0, 4000) }),
      });
    } catch {
      /* доставка — не критичный путь; след уже в vision.events */
    }
  }
}

const cfg = loadConfig();
const hub = new VisionHub(cfg);
await hub.init();

const server = createServer((req, res) => hub.handleHttp(req, res));
const wss = hub.attach();
server.on("upgrade", (req, socket, head) => {
  const url = new URL(req.url ?? "/", "http://internal");
  if (url.pathname !== "/ws") {
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit("connection", ws, req));
});

server.listen(cfg.port, "0.0.0.0", () => {
  console.log(
    `[miniapp] :${cfg.port} · зрение=${visionReady(cfg) ? cfg.visionModel : "нет"} · ` +
      `БД=${hub.store.active ? "да" : "нет"} · обход=${cfg.allowUnverified ? "ВКЛЮЧЁН (dev)" : "выкл"}`,
  );
});

for (const sig of ["SIGINT", "SIGTERM"] as const) {
  process.on(sig, () => {
    console.log(`[miniapp] ${sig} — закрываюсь`);
    wss.close();
    server.close();
    void hub.store.dispose().finally(() => process.exit(0));
  });
}
