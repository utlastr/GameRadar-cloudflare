import { RadarShard } from "./shard";

export { RadarShard };

const SHARD_COUNT = 10;
const MAX_IMPORT_BODY_BYTES = 5_000_000;
const API_MIGRATION_STATE = {
  phase: "read-api-compatibility",
  completed: [
    "worker-static-assets",
    "health-and-migration-status",
    "authenticated-sharded-metric-prototype",
    "dashboard-state-snapshot-read",
    "entity-series-read",
    "entity-detail-and-label-read",
  ],
  pending: [
    "collection-scheduler",
    "remaining-local-dashboard-read-and-write-apis",
    "bilibili-youtube-and-other-platform-collectors",
    "historical-data-import",
    "access-policy-and-production-deployment",
  ],
} as const;

interface Env {
  ASSETS: Fetcher;
  RADAR_SHARDS: DurableObjectNamespace<RadarShard>;
  IMPORT_TOKEN?: string;
}

interface ImportShardBatch {
  shard?: unknown;
  batchId?: unknown;
  rows?: unknown;
}

interface ImportBatchRequest {
  table?: unknown;
  columns?: unknown;
  batches?: unknown;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

async function callShard(
  env: Env,
  shard: number,
  path: string,
  method: "GET" | "POST",
  body?: unknown,
): Promise<Response> {
  const id = env.RADAR_SHARDS.idFromName(String(shard));
  return env.RADAR_SHARDS.get(id).fetch(`https://shard.internal${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function handleImport(request: Request, env: Env): Promise<Response> {
  if (!isAuthorized(request, env.IMPORT_TOKEN)) {
    return json({ error: "unauthorized" }, 401);
  }

  const url = new URL(request.url);
  if (url.pathname === "/api/cloudflare/import/status" && request.method === "GET") {
    const results = await Promise.all(
      Array.from({ length: SHARD_COUNT }, async (_, shard) => {
        const response = await callShard(env, shard, "/import/status", "GET");
        return { shard, response };
      }),
    );
    const failures = results.filter((item) => !item.response.ok);
    if (failures.length) {
      return json(
        {
          error: "shard_status_failed",
          shards: failures.map(({ shard, response }) => ({ shard, status: response.status })),
        },
        502,
      );
    }

    const shards = await Promise.all(
      results.map(async ({ shard, response }) => ({
        shard,
        ...(await response.json() as Record<string, unknown>),
      })),
    );
    const tableCounts: Record<string, number> = {};
    for (const shard of shards) {
      const tables = shard.tables as Record<string, number> | undefined;
      for (const [name, count] of Object.entries(tables ?? {})) {
        tableCounts[name] = (tableCounts[name] ?? 0) + Number(count);
      }
    }
    return json({
      ok: true,
      shardCount: SHARD_COUNT,
      storageBytes: shards.reduce((sum, shard) => sum + Number(shard.storageBytes ?? 0), 0),
      importedBatchCount: shards.reduce(
        (sum, shard) => sum + Number(shard.importedBatchCount ?? 0),
        0,
      ),
      tables: tableCounts,
      shards,
    });
  }

  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (contentLength > MAX_IMPORT_BODY_BYTES) {
    return json({ error: "import_request_too_large" }, 413);
  }

  let input: unknown;
  try {
    input = await request.json();
  } catch {
    return json({ error: "invalid_json" }, 400);
  }

  if (url.pathname === "/api/cloudflare/import/schema" && request.method === "POST") {
    const body = input as { tables?: unknown; indexes?: unknown };
    if (
      (body.tables !== undefined && !Array.isArray(body.tables)) ||
      (body.indexes !== undefined && !Array.isArray(body.indexes))
    ) {
      return json({ error: "invalid_schema" }, 400);
    }
    const results = await Promise.all(
      Array.from({ length: SHARD_COUNT }, async (_, shard) => {
        const response = await callShard(env, shard, "/import/schema", "POST", body);
        return { shard, response };
      }),
    );
    const replies = await Promise.all(
      results.map(async ({ shard, response }) => ({
        shard,
        status: response.status,
        body: await response.json().catch(() => ({})),
      })),
    );
    const failed = replies.filter((reply) => reply.status < 200 || reply.status >= 300);
    return json(
      { ok: failed.length === 0, shards: replies },
      failed.length ? 502 : 200,
    );
  }

  if (url.pathname === "/api/cloudflare/import/batch" && request.method === "POST") {
    const body = input as ImportBatchRequest;
    if (
      typeof body.table !== "string" ||
      !Array.isArray(body.columns) ||
      !Array.isArray(body.batches) ||
      body.batches.length < 1 ||
      body.batches.length > SHARD_COUNT
    ) {
      return json({ error: "invalid_import_batch" }, 400);
    }

    const seen = new Set<number>();
    let totalRows = 0;
    for (const batch of body.batches as ImportShardBatch[]) {
      if (
        !Number.isInteger(batch.shard) ||
        Number(batch.shard) < 0 ||
        Number(batch.shard) >= SHARD_COUNT ||
        seen.has(Number(batch.shard)) ||
        typeof batch.batchId !== "string" ||
        !Array.isArray(batch.rows)
      ) {
        return json({ error: "invalid_import_shard_batch" }, 400);
      }
      seen.add(Number(batch.shard));
      totalRows += batch.rows.length;
    }
    if (totalRows > 20_000 || totalRows < 1) {
      return json({ error: "import_row_limit_exceeded" }, 413);
    }

    const replies = await Promise.all(
      (body.batches as ImportShardBatch[]).map(async (batch) => {
        const shard = Number(batch.shard);
        const response = await callShard(env, shard, "/import/batch", "POST", {
          table: body.table,
          columns: body.columns,
          batchId: batch.batchId,
          rows: batch.rows,
        });
        return {
          shard,
          status: response.status,
          body: await response.json().catch(() => ({})),
        };
      }),
    );
    const failed = replies.filter((reply) => reply.status < 200 || reply.status >= 300);
    return json(
      { ok: failed.length === 0, rows: totalRows, shards: replies },
      failed.length ? 502 : 200,
    );
  }

  return json({ error: "not_found" }, 404);
}

function shardFor(source: string, entity: string): number {
  const value = `${source}:${entity}`;
  return hashShard(value);
}

function tableShard(table: string, key: unknown[]): number {
  return hashShard(`${table}:${JSON.stringify(key)}`);
}

function hashShard(value: string): number {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % SHARD_COUNT;
}

async function readShard(
  env: Env,
  shard: number,
  path: string,
): Promise<Record<string, unknown> | null> {
  const response = await callShard(env, shard, path, "GET");
  if (!response.ok) return null;
  return response.json() as Promise<Record<string, unknown>>;
}

async function readKv(env: Env, key: string): Promise<unknown> {
  const shard = tableShard("kv", [key]);
  const result = await readShard(env, shard, `/read/kv?key=${encodeURIComponent(key)}`);
  if (!result?.found || typeof result.value !== "string") return undefined;
  try {
    return JSON.parse(result.value);
  } catch {
    return undefined;
  }
}

function imageUrl(source: string, entity: string, extra: Record<string, unknown>): string {
  const keys = ["image", "icon", "cover", "banner", "thumb", "thumbnail", "box_art"];
  const nestedKeys = ["small_url", "url", "src", "medium_url", "original_url", "contentUrl", "imageUrl", "responsive"];
  const find = (value: unknown, depth = 0): string => {
    if (depth > 3 || value == null) return "";
    if (typeof value === "string") {
      const normalized = value.trim().startsWith("//")
        ? `https:${value.trim()}`
        : value.trim();
      return /^https?:\/\//i.test(normalized) ? normalized : "";
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = find(item, depth + 1);
        if (found) return found;
      }
    } else if (typeof value === "object") {
      const record = value as Record<string, unknown>;
      for (const key of nestedKeys) {
        const found = find(record[key], depth + 1);
        if (found) return found;
      }
    }
    return "";
  };
  for (const key of keys) {
    const found = find(extra[key]);
    if (found) return found;
  }
  return source === "steam" && /^\d+$/.test(entity)
    ? `https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/${entity}/header.jpg`
    : "";
}

function parsedJson(value: unknown, fallback: unknown): unknown {
  if (typeof value !== "string" || !value) return fallback;
  try {
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

async function handleApi(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname.startsWith("/api/cloudflare/import/")) {
    return handleImport(request, env);
  }
  if (url.pathname === "/api/health") {
    return json({ ok: true, runtime: "cloudflare-workers", storage: "durable-objects-sqlite" });
  }
  if (url.pathname === "/api/cloudflare/status") {
    return json({
      ok: true,
      deployed: false,
      shardCount: SHARD_COUNT,
      migration: API_MIGRATION_STATE,
    });
  }

  if (url.pathname === "/api/state" && request.method === "GET") {
    const dashboard = await readKv(env, "dashboard");
    if (!dashboard || typeof dashboard !== "object" || Array.isArray(dashboard)) {
      return json({ error: "dashboard_snapshot_missing" }, 503);
    }
    const state = dashboard as Record<string, unknown>;
    const [sources, releaseDiscovery, analyzeError] = await Promise.all([
      readKv(env, "source_status"),
      readKv(env, "release_discovery"),
      readKv(env, "analyze_error"),
    ]);
    return json({
      ...state,
      sources: sources && typeof sources === "object" ? sources : state.sources ?? {},
      release_discovery: releaseDiscovery ?? {},
      analyze_error: analyzeError ?? null,
      health: {
        alerts: [{
          title: "云端采集尚未启用",
          detail: "当前展示的是导入快照，数据不会自动更新。",
        }],
      },
      cloudflare: { snapshot: true, liveCollection: false },
    });
  }

  if (url.pathname === "/api/series" && request.method === "GET") {
    const keys = (url.searchParams.get("keys") ?? "").split(",").slice(0, 30);
    const rawDays = Number(url.searchParams.get("days") ?? 7);
    const days = Number.isFinite(rawDays) ? Math.max(1, Math.min(90, Math.trunc(rawDays))) : 7;
    const entries = await Promise.all(keys.map(async (key) => {
      const separator = key.indexOf(":");
      if (separator < 1 || separator === key.length - 1) return null;
      const source = key.slice(0, separator);
      const entity = key.slice(separator + 1);
      const shard = shardFor(source, entity);
      const path = `/read/series?source=${encodeURIComponent(source)}` +
        `&entity=${encodeURIComponent(entity)}&days=${days}`;
      const series = await readShard(env, shard, path);
      return series ? [key, series] as const : null;
    }));
    if (entries.some((entry) => entry === null)) {
      return json({ error: "shard_read_failed" }, 502);
    }
    return json(Object.fromEntries(
      entries.filter((entry): entry is readonly [string, Record<string, unknown>] => entry !== null),
    ));
  }

  if (url.pathname === "/api/entity" && request.method === "GET") {
    const key = url.searchParams.get("key") ?? "";
    const separator = key.indexOf(":");
    if (separator < 1 || separator === key.length - 1) return json({});
    const source = key.slice(0, separator);
    const entity = key.slice(separator + 1);
    const result = await readShard(
      env,
      shardFor(source, entity),
      `/read/entity?source=${encodeURIComponent(source)}&entity=${encodeURIComponent(entity)}`,
    );
    if (!result) return json({ error: "shard_read_failed" }, 502);
    const row = result?.entity as Record<string, unknown> | null | undefined;
    if (!row) return json({});
    const extra = recordOf(parsedJson(row.extra, {}));
    const keep = [
      "url", "image", "developers", "publishers", "genre", "tags", "lists", "release_ts",
      "released_time", "coming_soon", "preorder", "full_name", "name_cn", "price_usd",
      "discount", "score", "top_title", "top_nick", "stars", "stat_date", "labels",
    ];
    return json({
      name: row.name,
      first_seen: row.first_seen,
      last_seen: row.last_seen,
      extra: Object.fromEntries(keep.filter((field) => field in extra).map((field) => [field, extra[field]])),
    });
  }

  if (url.pathname === "/api/detail" && request.method === "GET") {
    const key = url.searchParams.get("key") ?? "";
    const separator = key.indexOf(":");
    if (separator < 1 || separator === key.length - 1) return json({});
    const source = key.slice(0, separator);
    const entity = key.slice(separator + 1);
    const shard = shardFor(source, entity);
    const [result, labelResult] = await Promise.all([
      readShard(
        env,
        shard,
        `/read/entity?source=${encodeURIComponent(source)}&entity=${encodeURIComponent(entity)}`,
      ),
      readShard(
        env,
        tableShard("labels", [key]),
        `/read/label?member=${encodeURIComponent(key)}`,
      ),
    ]);
    if (!result || !labelResult) return json({ error: "shard_read_failed" }, 502);
    const row = result?.entity as Record<string, unknown> | null | undefined;
    const extra = recordOf(parsedJson(row?.extra, {}));
    const detailExtra: Record<string, unknown> = {};
    for (const [field, value] of Object.entries(extra)) {
      if (field.startsWith("_")) continue;
      let encoded: string;
      try {
        encoded = JSON.stringify(value);
      } catch {
        continue;
      }
      if (encoded.length <= 1500) detailExtra[field] = value;
    }
    if (typeof extra.desc === "string") detailExtra.desc = extra.desc.slice(0, 300);

    const rawLabel = labelResult?.label as Record<string, unknown> | null | undefined;
    const label = rawLabel ? {
      stars: rawLabel.stars,
      blocked: Boolean(rawLabel.blocked),
      note: rawLabel.note ?? "",
      aliases: parsedJson(rawLabel.aliases, []),
      keywords: parsedJson(rawLabel.keywords, []),
      dismissed: rawLabel.dismissed ?? 0,
      updated: rawLabel.updated ?? 0,
    } : null;
    const output: Record<string, unknown> = {
      key,
      source,
      extra: detailExtra,
      image: imageUrl(source, entity, extra),
      label,
    };
    if (row) {
      output.name = row.name;
      output.first_seen = row.first_seen;
      output.last_seen = row.last_seen;
    }
    const review = result?.review as Record<string, unknown> | null | undefined;
    if (review && review.value !== undefined) output.reviews = review.value;
    return json(output);
  }

  if (url.pathname === "/api/cloudflare/shard" && request.method === "GET") {
    if (!isAuthorized(request, env.IMPORT_TOKEN)) {
      return json({ error: "unauthorized" }, 401);
    }
    const source = url.searchParams.get("source");
    const entity = url.searchParams.get("entity");
    if (!source || !entity) {
      return json({ error: "source and entity are required" }, 400);
    }
    const id = env.RADAR_SHARDS.idFromName(String(shardFor(source, entity)));
    const shardUrl = new URL("https://shard.internal/metrics");
    shardUrl.searchParams.set("source", source);
    shardUrl.searchParams.set("entity", entity);
    return env.RADAR_SHARDS.get(id).fetch(shardUrl);
  }

  if (url.pathname === "/api/cloudflare/shard" && request.method === "POST") {
    if (!isAuthorized(request, env.IMPORT_TOKEN)) {
      return json({ error: "unauthorized" }, 401);
    }
    let input: { source?: unknown; entity?: unknown; records?: unknown };
    try {
      input = await request.json();
    } catch {
      return json({ error: "invalid_json" }, 400);
    }
    if (
      typeof input.source !== "string" ||
      typeof input.entity !== "string" ||
      !Array.isArray(input.records) ||
      input.records.length > 90
    ) {
      return json({ error: "source, entity, and at most 90 records are required" }, 400);
    }

    const id = env.RADAR_SHARDS.idFromName(String(shardFor(input.source, input.entity)));
    return env.RADAR_SHARDS.get(id).fetch("https://shard.internal/metrics", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(input),
    });
  }

  return json(
    {
      error: "api_not_migrated",
      message: "This local Game Radar API has not been ported yet; no local data is being served.",
      migration: API_MIGRATION_STATE,
    },
    503,
  );
}

function isAuthorized(request: Request, token?: string): boolean {
  if (!token) return false;
  return request.headers.get("authorization") === `Bearer ${token}`;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) {
      return handleApi(request, env);
    }
    if (url.pathname === "/map") {
      const mapUrl = new URL("/map.html", request.url);
      return env.ASSETS.fetch(new Request(mapUrl, request));
    }
    return env.ASSETS.fetch(request);
  },
};
