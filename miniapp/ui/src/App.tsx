import { useCallback, useEffect, useRef, useState } from "react";
import { VisionLink, type ServerMsg } from "./client.ts";

type FeedItem = {
  id: number;
  dir: "in" | "out" | "sys";
  text: string;
  ok: boolean;
  ms: number;
  at: string;
};

type Status = { state: string; frames: number; dropped: number; analyses: number; lastMs: number };

const MODES = [
  ["stream", "🔴 стрим"],
  ["ask", "💬 по вопросу"],
  ["brief", "👁 один взгляд"],
] as const;

let feedSeq = 0;
const clock = () => new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function App(): React.ReactNode {
  const [up, setUp] = useState(false);
  const [env, setEnv] = useState<{ engine: string; vision: boolean; db: boolean } | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [mode, setMode] = useState<(typeof MODES)[number][0]>("stream");
  const [running, setRunning] = useState(false);
  const [camError, setCamError] = useState("");
  const [question, setQuestion] = useState("");
  const [thinking, setThinking] = useState(false);

  const linkRef = useRef<VisionLink | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const timerRef = useRef<number | null>(null);
  const feedRef = useRef<HTMLDivElement | null>(null);
  const runningRef = useRef(running);
  runningRef.current = running;

  const push = useCallback((dir: FeedItem["dir"], text: string, ok = true, ms = 0) => {
    setFeed((f) => [...f.slice(-60), { id: ++feedSeq, dir, text, ok, ms, at: clock() }]);
  }, []);

  useEffect(() => {
    const link = new VisionLink(
      (m: ServerMsg) => {
        if (m.t === "hello") {
          setEnv({ engine: m.engine, vision: m.vision, db: m.db });
          push("sys", m.vision ? `движок: ${m.engine}` : "модель зрения не подключена — буду честен в ошибках");
          if (!m.db) push("sys", "БД не видна — след сессии не пишется", false);
        } else if (m.t === "status") {
          setStatus({ state: m.state, frames: m.frames, dropped: m.dropped, analyses: m.analyses, lastMs: m.lastMs });
        } else if (m.t === "analysis") {
          setThinking(false);
          push("in", m.text, m.ok, m.ms);
        } else if (m.t === "answer") {
          setThinking(false);
          push("out", m.text, m.ok, m.ms);
        } else if (m.t === "fatal") {
          push("sys", m.error, false);
        }
      },
      (isUp) => {
        setUp(isUp);
        if (!isUp) setRunning(false);
      },
    );
    linkRef.current = link;
    link.connect();
    return () => link.close();
  }, [push]);

  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [feed]);

  const stopCamera = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
  }, []);

  const shoot = useCallback(() => {
    const video = videoRef.current;
    const link = linkRef.current;
    if (!video || !streamRef.current || !link) return;
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (!w || !h) return;
    const scale = Math.min(1, 768 / Math.max(w, h));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const b64 = canvas.toDataURL("image/jpeg", 0.62).split(",", 2)[1] ?? "";
    if (b64) link.send({ t: "frame", img: b64 });
  }, []);

  const startStream = useCallback(async () => {
    const link = linkRef.current;
    if (!link || !up) return;
    setCamError("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment", width: { ideal: 1280 } },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play().catch(() => undefined);
      }
      link.send({ t: "start", mode });
      setRunning(true);
      if (mode !== "ask") {
        timerRef.current = window.setInterval(shoot, 950);
        setTimeout(shoot, 120);
      }
      if (mode === "brief") {
        // сервер сам выключит стрим после одного анализа — у клиента задача одна: не спамить
        window.setTimeout(() => {
          stopCamera();
          setRunning(false);
        }, 12_000);
      }
    } catch (err) {
      const name = (err as DOMException).name;
      setCamError(
        name === "NotAllowedError"
          ? "камеру не разрешили — без неё я слепой"
          : name === "NotFoundError"
            ? "камеры нет — можно спрашивать про последние кадры, но их тоже нет"
            : `камера: ${name}`,
      );
    }
  }, [mode, shoot, stopCamera, up]);

  const stopStream = useCallback(() => {
    linkRef.current?.send({ t: "stop" });
    setRunning(false);
    stopCamera();
  }, [stopCamera]);

  useEffect(() => () => stopCamera(), [stopCamera]);

  const ask = useCallback(async () => {
    const q = question.trim();
    const link = linkRef.current;
    if (!q || !link || !up) return;
    setQuestion("");
    setThinking(true);
    push("out", q);
    if (mode === "ask" && !runningRef.current) await startStream(); // камера включится, авто-кадров не будет
    if (mode === "ask") shoot(); // вопрос всегда при себе тащит свежий кадр
    link.send({ t: "ask", text: q });
  }, [question, up, mode, push, shoot, startStream]);

  return (
    <main className="stage">
      <div className="aurora" aria-hidden />
      <header className="glass topbar rise">
        <div className="brand">
          <span className="lens" data-live={running ? "1" : "0"} />
          <div>
            <h1>Aegis · Зрение</h1>
            <p>{env ? `движок ${env.engine} · ${env.db ? "след в БД" : "без следа"}` : "подключение…"}</p>
          </div>
        </div>
        <div className="chips">
          <i className={`chip ${up ? "ok" : "bad"}`}>{up ? "связь" : "нет связи"}</i>
          <i className={`chip ${env?.vision ? "ok" : "warn"}`}>{env?.vision ? "зрение" : "нет модели"}</i>
        </div>
      </header>

      <section className="glass viewer rise d1">
        <video ref={videoRef} muted playsInline className={running ? "on" : "off"} />
        {!running && (
          <div className="idle">
            <span>📷</span>
            <p>{camError || "камера спит — нажми «смотреть»"}</p>
          </div>
        )}
        {running && status && (
          <div className="hud">
            <b>{status.state}</b>
            <span>кадров {status.frames}</span>
            <span>анализов {status.analyses}</span>
            {status.dropped > 0 && <span>лишних отброшено {status.dropped}</span>}
            {status.lastMs > 0 && <span>{status.lastMs} мс</span>}
          </div>
        )}
      </section>

      <section className="glass feed rise d2" ref={feedRef}>
        {feed.length === 0 && <p className="muted">лента наблюдения пуста — всё появится здесь</p>}
        {feed.map((f) => (
          <article key={f.id} className={`msg ${f.dir} ${f.ok ? "" : "err"}`}>
            <p>{f.text}</p>
            <footer>
              <time>{f.at}</time>
              {f.ms > 0 && <span className="ms">{f.ms} мс</span>}
            </footer>
          </article>
        ))}
        {thinking && <p className="muted thinking">думаю…</p>}
      </section>

      <footer className="glass composer rise d3">
        <div className="modes">
          {MODES.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={mode === value ? "mode picked" : "mode"}
              disabled={running}
              onClick={() => setMode(value)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="line">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ask()}
            placeholder="вопрос к кадру…"
            maxLength={500}
          />
          <button type="button" className="ghost" onClick={ask} disabled={!up || !question.trim()}>
            ✉️
          </button>
          <button type="button" className={running ? "big stop" : "big"} onClick={running ? stopStream : startStream} disabled={!up}>
            {running ? "■ стоп" : "▶ смотреть"}
          </button>
        </div>
      </footer>
    </main>
  );
}
