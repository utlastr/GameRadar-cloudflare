import { DurableObject } from "cloudflare:workers";

interface MetricRecord {
  metric?: unknown;
  ts?: unknown;
  value?: unknown;
}

interface MetricBatch {
  source?: unknown;
  entity?: unknown;
  records?: unknown;
}

interface Env {}

export class RadarShard extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS entities (
        source TEXT NOT NULL,
        entity TEXT NOT NULL,
        name TEXT NOT NULL,
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        extra TEXT,
        PRIMARY KEY (source, entity)
      );
      CREATE TABLE IF NOT EXISTS metrics (
        ts INTEGER NOT NULL,
        source TEXT NOT NULL,
        entity TEXT NOT NULL,
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        PRIMARY KEY (source, entity, metric, ts)
      );
      CREATE INDEX IF NOT EXISTS idx_metrics_source_metric_ts
        ON metrics(source, metric, ts);
      CREATE TABLE IF NOT EXISTS kv (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
      );
    `);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/metrics") {
      return Response.json({ error: "not_found" }, { status: 404 });
    }

    if (request.method === "GET") {
      const source = url.searchParams.get("source");
      const entity = url.searchParams.get("entity");
      if (!source || !entity) {
        return Response.json({ error: "source and entity are required" }, { status: 400 });
      }
      const rows = this.ctx.storage.sql
        .exec(
          `SELECT ts, metric, value FROM metrics
           WHERE source = ? AND entity = ? ORDER BY ts DESC LIMIT 500`,
          source,
          entity,
        )
        .toArray();
      return Response.json({ source, entity, records: rows });
    }

    if (request.method !== "POST") {
      return Response.json({ error: "method_not_allowed" }, { status: 405 });
    }

    let batch: MetricBatch;
    try {
      batch = await request.json();
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }
    if (
      typeof batch.source !== "string" ||
      typeof batch.entity !== "string" ||
      !Array.isArray(batch.records) ||
      batch.records.length > 90
    ) {
      return Response.json({ error: "invalid_batch" }, { status: 400 });
    }

    const now = Math.floor(Date.now() / 1000);
    for (const raw of batch.records) {
      if (!raw || typeof raw !== "object") {
        return Response.json({ error: "invalid_record" }, { status: 400 });
      }
      const record = raw as MetricRecord;
      if (
        typeof record.metric !== "string" ||
        !Number.isSafeInteger(record.ts) ||
        typeof record.value !== "number" ||
        !Number.isFinite(record.value)
      ) {
        return Response.json({ error: "invalid_record" }, { status: 400 });
      }
    }

    const entity = String(batch.entity);
    const source = String(batch.source);
    this.ctx.storage.sql.exec(
      `INSERT INTO entities(source, entity, name, first_seen, last_seen)
       VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(source, entity) DO UPDATE SET
         last_seen = MAX(entities.last_seen, excluded.last_seen)`,
      source,
      entity,
      entity,
      now,
      now,
    );

    for (const raw of batch.records as MetricRecord[]) {
      this.ctx.storage.sql.exec(
        `INSERT INTO metrics(ts, source, entity, metric, value)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(source, entity, metric, ts) DO UPDATE SET value = excluded.value`,
        raw.ts as number,
        source,
        entity,
        raw.metric as string,
        raw.value as number,
      );
    }

    return Response.json({ ok: true, written: batch.records.length });
  }
}
