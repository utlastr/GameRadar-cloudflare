import { Container } from "@cloudflare/containers";

interface Env {
  RADAR_APP: DurableObjectNamespace<RadarApp>;
  CF_ACCESS_TEAM_DOMAIN?: string;
  CF_ACCESS_AUD?: string;
  R2_ACCOUNT_ID?: string;
  R2_BUCKET?: string;
  R2_ACCESS_KEY_ID?: string;
  R2_SECRET_ACCESS_KEY?: string;
  R2_SEED_KEY?: string;
  R2_SETTINGS_KEY?: string;
}

interface AccessClaims {
  iss?: unknown;
  aud?: unknown;
  exp?: unknown;
  nbf?: unknown;
}

interface AccessJwkSet {
  keys?: JsonWebKey[];
}

const accessKeyCache = new Map<string, { key: CryptoKey; expiresAt: number }>();
let accessJwks: { keys: JsonWebKey[]; expiresAt: number } | undefined;

function decodeBase64Url(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function loadAccessKeys(teamDomain: string): Promise<JsonWebKey[]> {
  const now = Date.now();
  if (accessJwks && accessJwks.expiresAt > now) return accessJwks.keys;

  const response = await fetch(
    `https://${teamDomain}/cdn-cgi/access/certs`,
    { headers: { accept: "application/json" } },
  );
  if (!response.ok) throw new Error("Access signing keys unavailable");
  const payload = await response.json() as AccessJwkSet;
  if (!Array.isArray(payload.keys)) throw new Error("Invalid Access signing keys");

  accessJwks = { keys: payload.keys, expiresAt: now + 5 * 60 * 1000 };
  accessKeyCache.clear();
  return payload.keys;
}

async function verifyAccessRequest(request: Request, env: Env): Promise<boolean> {
  const teamDomain = (env.CF_ACCESS_TEAM_DOMAIN ?? "").trim().replace(/^https?:\/\//, "").replace(/\/+$/, "");
  const audience = (env.CF_ACCESS_AUD ?? "").trim();
  const token = request.headers.get("cf-access-jwt-assertion") ?? "";
  if (
    !teamDomain ||
    !/^[a-zA-Z0-9.-]+$/.test(teamDomain) ||
    !audience ||
    !token ||
    token.length > 16_384
  ) return false;

  try {
    const parts = token.split(".");
    if (parts.length !== 3) return false;
    const header = JSON.parse(new TextDecoder().decode(decodeBase64Url(parts[0]))) as {
      alg?: unknown;
      kid?: unknown;
    };
    if (header.alg !== "RS256" || typeof header.kid !== "string") return false;

    const cacheKey = `${teamDomain}:${header.kid}`;
    let cached = accessKeyCache.get(cacheKey);
    if (!cached || cached.expiresAt <= Date.now()) {
      const keys = await loadAccessKeys(teamDomain);
      const jwk = keys.find((key) => key.kid === header.kid && key.kty === "RSA");
      if (!jwk) return false;
      cached = {
        key: await crypto.subtle.importKey(
          "jwk",
          jwk,
          { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
          false,
          ["verify"],
        ),
        expiresAt: Date.now() + 5 * 60 * 1000,
      };
      accessKeyCache.set(cacheKey, cached);
    }

    const valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      cached.key,
      decodeBase64Url(parts[2]),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
    );
    if (!valid) return false;

    const claims = JSON.parse(new TextDecoder().decode(decodeBase64Url(parts[1]))) as AccessClaims;
    const nowSeconds = Math.floor(Date.now() / 1000);
    const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    return claims.iss === `https://${teamDomain}` &&
      audiences.includes(audience) &&
      typeof claims.exp === "number" &&
      claims.exp > nowSeconds &&
      (claims.nbf === undefined || (typeof claims.nbf === "number" && claims.nbf <= nowSeconds));
  } catch {
    return false;
  }
}

function unauthorized(): Response {
  return new Response("Cloudflare Access authentication required", {
    status: 401,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

export class RadarApp extends Container {
  defaultPort = 8765;
  pingEndpoint = "127.0.0.1/api/health";
  sleepAfter = "2m";

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.envVars = {
      RADAR_DATA_DIR: "/var/lib/gameradar",
      RADAR_DB_PATH: "/var/lib/gameradar/radar.db",
      RADAR_SERVER_HOST: "0.0.0.0",
      RADAR_SERVER_PORT: "8765",
      RADAR_SQLITE_JOURNAL_MODE: "WAL",
      R2_ACCOUNT_ID: env.R2_ACCOUNT_ID ?? "",
      R2_BUCKET: env.R2_BUCKET ?? "",
      R2_ACCESS_KEY_ID: env.R2_ACCESS_KEY_ID ?? "",
      R2_SECRET_ACCESS_KEY: env.R2_SECRET_ACCESS_KEY ?? "",
      R2_SEED_KEY: env.R2_SEED_KEY ?? "seed/radar.db",
      R2_SETTINGS_KEY: env.R2_SETTINGS_KEY ?? "seed/settings.json",
    };
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!await verifyAccessRequest(request, env)) return unauthorized();
    return env.RADAR_APP.getByName("primary").fetch(request);
  },

  scheduled(
    _controller: ScheduledController,
    env: Env,
    ctx: ExecutionContext,
  ): void {
    ctx.waitUntil(env.RADAR_APP.getByName("primary").fetch(
      new Request("http://radar.internal/api/health"),
    ));
  },
};
