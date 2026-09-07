/**
 * Следы в БД. DDL принадлежает python (alembic 0014) — здесь только вставки в
 * vision.sessions / vision.events. Падение БД не должно ронять анализ: всё через
 * `safe`, ошибки — в консоль, счётчик в статусе экрана.
 */

import type { Pool } from "pg";

// Pool импортируется типом; рантайм-модуль pg подгружается лениво в init(),
// чтобы юниты чистой логики не тянули драйвер.
import type { SessionMode } from "./protocol.ts";

export class VisionStore {
  private pool: Pool | null = null;
  broken = false;
  private readonly databaseUrl: string;

  constructor(databaseUrl: string) {
    this.databaseUrl = databaseUrl;
  }

  get active(): boolean {
    return Boolean(this.databaseUrl) && !this.broken;
  }

  async init(): Promise<void> {
    if (!this.databaseUrl) return;
    try {
      const { Pool } = await import("pg");
      this.pool = new Pool({ connectionString: this.databaseUrl, max: 4 });
      await this.pool.query("SELECT 1 FROM vision.sessions LIMIT 1");
    } catch (err) {
      this.broken = true;
      console.warn(`[store] БД недоступна — работаем без следа в базе: ${(err as Error).message}`);
      this.pool = null;
    }
  }

  async openSession(tgUserId: number, mode: SessionMode, engine: string): Promise<string | null> {
    const row = await this.safe(
      this.pool?.query(
        "INSERT INTO vision.sessions (tg_user_id, mode, engine) VALUES ($1, $2, $3) RETURNING id::text",
        [tgUserId, mode, engine.slice(0, 60) || null],
      ),
    );
    const id = (row?.rows as { id: string }[] | undefined)?.[0]?.id;
    return id ?? null;
  }

  async event(
    sessionId: string | null,
    kind: "frame" | "analysis" | "question" | "answer" | "error" | "note",
    text: string | null,
    ok: boolean,
    ms: number | null,
  ): Promise<void> {
    if (!sessionId) return;
    await this.safe(
      this.pool?.query(
        "INSERT INTO vision.events (session_id, kind, text, ok, ms) VALUES ($1, $2, $3, $4, $5)",
        [sessionId, kind, text, ok, ms],
      ),
    );
    if (kind === "frame") {
      await this.safe(
        this.pool?.query("UPDATE vision.sessions SET frames = frames + 1 WHERE id = $1::uuid", [sessionId]),
      );
    } else if (kind === "analysis" || kind === "answer") {
      await this.safe(
        this.pool?.query(
          "UPDATE vision.sessions SET analyses = analyses + 1 WHERE id = $1::uuid",
          [sessionId],
        ),
      );
    }
  }

  async close(sessionId: string | null, note: string): Promise<void> {
    if (!sessionId) return;
    await this.safe(
      this.pool?.query(
        "UPDATE vision.sessions SET ended_at = now(), note = $2 WHERE id = $1::uuid",
        [sessionId, note.slice(0, 400)],
      ),
    );
  }

  async recentAnalyses(sessionId: string, limit = 2): Promise<string[]> {
    const rows = await this.safe(
      this.pool?.query(
        "SELECT text FROM vision.events WHERE session_id = $1::uuid AND kind = 'analysis' AND ok"
          + " ORDER BY id DESC LIMIT $2",
        [sessionId, limit],
      ),
    );
    const list = (rows?.rows as { text: string }[] | undefined) ?? [];
    return list.map((r) => r.text).reverse();
  }

  private async safe<T>(p: Promise<T> | undefined): Promise<T | undefined> {
    if (!p) return undefined;
    try {
      return await p;
    } catch (err) {
      console.warn(`[store] пропущено: ${(err as Error).message}`);
      return undefined;
    }
  }

  async dispose(): Promise<void> {
    await this.safe(this.pool?.end());
    this.pool = null;
  }
}
