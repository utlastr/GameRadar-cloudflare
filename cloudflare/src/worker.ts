import { RadarShard } from "./shard";

export { RadarShard };

const SHARD_COUNT = 10;
const API_MIGRATION_STATE = {
  phase: "storage-foundation",
  completed: [
    "worker-static-assets",
    "health-and-migration-status",
    "authenticated-sharded-metric-prototype",
  ],
  pending: [
    "collection-scheduler",
    "complete-local-storage-schema-and-api-compatible-queries",
    "all-existing-dashboard-read-and-write-apis",
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

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function shardFor(source: string, entity: string): number {
  const value = `${source}:${entity}`;
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) % SHARD_COUNT;
}

async function handleApi(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
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
