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

interface ImportTable {
  name?: unknown;
  sql?: unknown;
  columns?: unknown;
}

interface ImportBatch {
  batchId?: unknown;
  table?: unknown;
  columns?: unknown;
  rows?: unknown;
}

interface Env {}

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]*$/;
const MAX_SHARD_BYTES = 400 * 1024 * 1024;

export class RadarShard extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS entities (
        source TEXT,
        entity TEXT,
        name TEXT,
        first_seen INTEGER,
        last_seen INTEGER,
        extra TEXT,
        PRIMARY KEY (source, entity)
      );
      CREATE TABLE IF NOT EXISTS metrics (
        ts INTEGER,
        source TEXT,
        entity TEXT,
        metric TEXT,
        value REAL,
        PRIMARY KEY (source, entity, metric, ts)
      );
      CREATE INDEX IF NOT EXISTS idx_metrics_source_metric_ts
        ON metrics(source, metric, ts);
      CREATE TABLE IF NOT EXISTS kv (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS _radar_import_tables (
        table_name TEXT PRIMARY KEY,
        columns_json TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS _radar_import_batches (
        table_name TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        row_count INTEGER NOT NULL,
        PRIMARY KEY (table_name, batch_id)
      );
    `);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/import/schema" && request.method === "POST") {
      return this.importSchema(request);
    }
    if (url.pathname === "/import/batch" && request.method === "POST") {
      return this.importBatch(request);
    }
    if (url.pathname === "/import/status" && request.method === "GET") {
      return this.importStatus();
    }
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

  private async importSchema(request: Request): Promise<Response> {
    let input: { tables?: unknown; indexes?: unknown };
    try {
      input = await request.json();
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }

    const tables = Array.isArray(input.tables) ? input.tables as ImportTable[] : [];
    const indexes = Array.isArray(input.indexes) ? input.indexes as unknown[] : [];
    if (tables.length > 120 || indexes.length > 500) {
      return Response.json({ error: "schema_limit_exceeded" }, { status: 413 });
    }

    try {
      for (const table of tables) {
        if (
          typeof table.name !== "string" ||
          !IDENTIFIER.test(table.name) ||
          table.name.startsWith("_radar_") ||
          typeof table.sql !== "string" ||
          table.sql.length > 100_000 ||
          !new RegExp(`^CREATE\\s+TABLE(?:\\s+IF\\s+NOT\\s+EXISTS)?\\s+["'\`\\[]?${table.name}["'\`\\]]?\\s*\\(`, "i").test(table.sql) ||
          table.sql.includes(";") ||
          !Array.isArray(table.columns) ||
          table.columns.length > 100 ||
          !table.columns.every(
            (column) =>
              typeof column === "string" &&
              (IDENTIFIER.test(column) || ["rowid", "_rowid_", "oid"].includes(column)),
          )
        ) {
          return Response.json({ error: "invalid_table_schema" }, { status: 400 });
        }

        const createTableSql = table.sql.replace(
          /^(CREATE\s+TABLE\s+)(?!IF\s+NOT\s+EXISTS\b)/i,
          "$1IF NOT EXISTS ",
        );
        this.ctx.storage.sql.exec(createTableSql);
        this.ctx.storage.sql.exec(
          `INSERT INTO _radar_import_tables(table_name, columns_json)
           VALUES (?, ?)
           ON CONFLICT(table_name) DO UPDATE SET columns_json = excluded.columns_json`,
          table.name,
          JSON.stringify(table.columns),
        );
        this.enforceStorageLimit();
      }

      for (const raw of indexes) {
        if (
          typeof raw !== "string" ||
          raw.length > 100_000 ||
          !/^CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+/i.test(raw) ||
          raw.includes(";")
        ) {
          return Response.json({ error: "invalid_index_schema" }, { status: 400 });
        }
        const createIndexSql = raw.replace(
          /^(CREATE\s+(?:UNIQUE\s+)?INDEX\s+)(?!IF\s+NOT\s+EXISTS\b)/i,
          "$1IF NOT EXISTS ",
        );
        this.ctx.storage.sql.exec(createIndexSql);
        this.enforceStorageLimit();
      }
    } catch (error) {
      return Response.json(
        { error: "schema_apply_failed", message: String(error).slice(0, 300) },
        { status: 400 },
      );
    }

    return Response.json({ ok: true, tables: tables.length, indexes: indexes.length });
  }

  private async importBatch(request: Request): Promise<Response> {
    let input: ImportBatch;
    try {
      input = await request.json();
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }

    if (
      typeof input.table !== "string" ||
      !IDENTIFIER.test(input.table) ||
      typeof input.batchId !== "string" ||
      input.batchId.length > 180 ||
      !Array.isArray(input.columns) ||
      !Array.isArray(input.rows) ||
      input.rows.length < 1 ||
      input.rows.length > 2_000
    ) {
      return Response.json({ error: "invalid_import_batch" }, { status: 400 });
    }

    const manifest = this.ctx.storage.sql
      .exec(
        "SELECT columns_json FROM _radar_import_tables WHERE table_name = ?",
        input.table,
      )
      .toArray()[0] as { columns_json?: string } | undefined;
    if (!manifest || manifest.columns_json !== JSON.stringify(input.columns)) {
      return Response.json({ error: "import_schema_missing" }, { status: 409 });
    }

    const columns = input.columns as string[];
    if (
      columns.length < 1 ||
      columns.length > 100 ||
      !columns.every(
        (column) =>
          typeof column === "string" &&
          (IDENTIFIER.test(column) || ["rowid", "_rowid_", "oid"].includes(column)),
      ) ||
      !input.rows.every(
        (row) => Array.isArray(row) && row.length === columns.length,
      )
    ) {
      return Response.json({ error: "invalid_import_rows" }, { status: 400 });
    }

    const quotedTable = `"${input.table}"`;
    const quotedColumns = columns.map((column) => `"${column}"`).join(", ");
    const values = columns
      .map((_, index) => `json_extract(item.value, '$[${index}]')`)
      .join(", ");
    const statement =
      `INSERT OR REPLACE INTO ${quotedTable} (${quotedColumns}) ` +
      `SELECT ${values} FROM json_each(?) AS item`;
    const serializedRows = JSON.stringify(input.rows);
    if (new TextEncoder().encode(serializedRows).byteLength > 1_400_000) {
      return Response.json({ error: "import_batch_too_large" }, { status: 413 });
    }

    let alreadyImported = false;
    try {
      this.ctx.storage.transactionSync(() => {
        const prior = this.ctx.storage.sql
          .exec(
            `SELECT 1 FROM _radar_import_batches
             WHERE table_name = ? AND batch_id = ?`,
            input.table as string,
            input.batchId as string,
          )
          .toArray();
        if (prior.length) {
          alreadyImported = true;
          return;
        }

        this.ctx.storage.sql.exec(statement, serializedRows);
        this.ctx.storage.sql.exec(
          `INSERT INTO _radar_import_batches(table_name, batch_id, row_count)
           VALUES (?, ?, ?)`,
          input.table as string,
          input.batchId as string,
          input.rows!.length,
        );
        this.enforceStorageLimit();
      });
    } catch (error) {
      return Response.json(
        { error: "import_write_failed", message: String(error).slice(0, 300) },
        { status: 507 },
      );
    }

    return Response.json({
      ok: true,
      duplicate: alreadyImported,
      rows: alreadyImported ? 0 : input.rows.length,
      storageBytes: this.storageBytes(),
    });
  }

  private importStatus(): Response {
    const tables = this.ctx.storage.sql
      .exec("SELECT table_name, columns_json FROM _radar_import_tables ORDER BY table_name")
      .toArray() as Array<{ table_name: string; columns_json: string }>;
    const counts: Record<string, number> = {};
    for (const table of tables) {
      const rows = this.ctx.storage.sql
        .exec(`SELECT count(*) AS n FROM "${table.table_name}"`)
        .toArray()[0] as { n?: number } | undefined;
      counts[table.table_name] = Number(rows?.n ?? 0);
    }
    const pages = this.ctx.storage.sql.exec("PRAGMA page_count").toArray()[0] as
      | { page_count?: number }
      | undefined;
    const pageSize = this.ctx.storage.sql.exec("PRAGMA page_size").toArray()[0] as
      | { page_size?: number }
      | undefined;
    return Response.json({
      ok: true,
      storageBytes: this.storageBytes(),
      tables: counts,
      importedTableCount: tables.length,
      importedBatchCount: Number(
        this.ctx.storage.sql
          .exec("SELECT count(*) AS n FROM _radar_import_batches")
          .toArray()[0]?.n ?? 0,
      ),
      pageCount: Number(pages?.page_count ?? 0),
      pageSize: Number(pageSize?.page_size ?? 0),
    });
  }

  private storageBytes(): number {
    const pageCount = this.ctx.storage.sql
      .exec("PRAGMA page_count")
      .toArray()[0] as { page_count?: number } | undefined;
    const pageSize = this.ctx.storage.sql
      .exec("PRAGMA page_size")
      .toArray()[0] as { page_size?: number } | undefined;
    return Number(pageCount?.page_count ?? 0) * Number(pageSize?.page_size ?? 0);
  }

  private enforceStorageLimit(): void {
    const bytes = this.storageBytes();
    if (bytes > MAX_SHARD_BYTES) {
      throw new Error(`shard storage guard exceeded: ${bytes}`);
    }
  }
}
