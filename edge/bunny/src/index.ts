/**
 * Bunny edge script entry point (CONTRACT.md section 7).
 *
 * Responsibilities:
 *   - fail CLOSED when the runtime configuration is missing: without
 *     tokenSecret or originHost every request is answered 500 before any
 *     other logic (an empty secret would make HMAC verification accept any
 *     forged token; an empty origin host would break every canonical fetch),
 *   - honour the AUTHENTICATED origin-fetch bypass: X-VG-Origin-Fetch carries
 *     hex(HMAC_SHA256(tokenSecret, "origin-fetch:" + path)); a present but
 *     invalid value is a 403, never a fall-through to token auth, and a
 *     genuine match passes the request through to Bunny's cache/origin
 *     (`handleRequest` performs that fetch itself for the offline tests),
 *   - handle ONLY *.m3u8 / *.mpd; every other path continues to Bunny's
 *     cache/origin untouched (`handleRequest` performs the origin fetch
 *     itself when used as the standalone handler), and successful
 *     segment/init/thumbnail responses get the immutable cache header,
 *   - verify the short-lived signed playback token (?vg_token=) and BIND it
 *     to the requested job (the path must start with /<claims.job_id>/),
 *   - personalise (HLS per segment + token propagation into the child
 *     playlist URIs of a master; DASH one variant per session),
 *   - manifests respond with Cache-Control: private, no-store, the right
 *     Content-Type for HLS/DASH and CORS headers (configurable corsOrigin,
 *     default "*") so browser players can read them.
 *
 * NO S3 credentials in the edge script: origin reads are signed by Bunny
 * itself (AWSSigningEnabled). The token secret and origin host are injected
 * by Bunny as environment variables/secrets - never baked into the file.
 *
 * `handleClientRequest` / `handleClientResponse` are the Bunny middleware
 * hooks used by `src/middleware.ts` (`servePullZone`). `handleRequest` keeps
 * the full standalone behaviour for the offline tests.
 */

import {
  fetchCanonicalManifest,
  isAuthorisedOriginFetch,
  ORIGIN_FETCH_HEADER,
  originFetchHeaderValue,
  type Fetcher,
} from "./origin.ts";
import { personalizeDash, personalizeHls } from "./manifest.ts";
import { verifyToken } from "./token.ts";

export { ORIGIN_FETCH_HEADER } from "./origin.ts";
export type { Fetcher } from "./origin.ts";
export { personalizeDash, personalizeHls } from "./manifest.ts";
export { verifyToken } from "./token.ts";
export { pattern, patternForCount } from "./ab_pattern.ts";

/** Cache header for segments / thumbnails / init (shared, immutable). */
export const SEGMENT_CACHE_CONTROL = "public, max-age=31536000, immutable";

/** Cache header for personalised manifests (per session, never cached). */
export const MANIFEST_CACHE_CONTROL = "private, no-store";

export interface EdgeDeps {
  /**
   * Absolute URL (scheme + hostname) of the CANONICAL origin: the host that
   * serves the unpersonalised manifests and the rendition objects. MUST be
   * a hostname DISTINCT from the edge's own public hostname. EVERY origin
   * fetch of this script targets `originHost + path` (see fetchFromOrigin)
   * and carries the authenticated bypass header, so a host that pointed
   * back at the edge itself would make each fetch re-enter this script
   * WITH a valid bypass header and recurse without bound. Configure a
   * DIFFERENT hostname.
   *
   * The bypass itself is authenticated: it is honoured only when the header
   * value equals hex(HMAC_SHA256(tokenSecret, "origin-fetch:" + path)) AND
   * the request hostname equals THIS host's hostname.
   */
  originHost: string;
  /** HMAC secret shared with the Go token issuer. */
  tokenSecret: string;
  /** Injectable HTTP fetch (defaults to the runtime fetch). */
  fetcher: Fetcher;
  /** Injectable clock in unix seconds (for offline tests). */
  now?: () => number;
  /**
   * Value of the Access-Control-Allow-Origin header on personalised
   * manifest responses (CORS). Default "*": any web origin may read the
   * manifests. Set a concrete origin (e.g. "https://player.example.com")
   * to restrict who may read them from a browser. An absent or empty
   * value falls back to "*".
   */
  corsOrigin?: string;
}

function isManifestPath(path: string): boolean {
  return path.endsWith(".m3u8") || path.endsWith(".mpd");
}

/**
 * CORS headers on personalised manifest responses: a browser player reads
 * the manifest (and its byte ranges) from a web origin other than the
 * edge's, so the response must allow that explicitly. `Range` must be in
 * Access-Control-Allow-Headers (byte-range requests are not CORS-safelisted)
 * and Content-Length / Content-Range / ETag must be exposed for players
 * that read them.
 */
const CORS_ALLOW_METHODS = "GET, HEAD, OPTIONS";
const CORS_ALLOW_HEADERS = "Range, If-None-Match, If-Modified-Since";
const CORS_EXPOSE_HEADERS = "Content-Length, Content-Range, ETag";

function corsHeaders(allowOrigin: string): Headers {
  const headers = new Headers();
  headers.set("Access-Control-Allow-Origin", allowOrigin);
  headers.set("Access-Control-Allow-Methods", CORS_ALLOW_METHODS);
  headers.set("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS);
  headers.set("Access-Control-Expose-Headers", CORS_EXPOSE_HEADERS);
  headers.set("Vary", "Origin");
  return headers;
}

/** Headers of a personalised manifest response: type, no-store, CORS. */
function manifestHeaders(contentType: string, allowOrigin: string): Headers {
  const headers = corsHeaders(allowOrigin);
  headers.set("Content-Type", contentType);
  headers.set("Cache-Control", MANIFEST_CACHE_CONTROL);
  return headers;
}

/**
 * ALL origin fetches of this script go through here. The target is ALWAYS
 * `deps.originHost + path` - NEVER the public request URL: that URL is the
 * edge's own hostname, and fetching it would make the script re-enter
 * itself (the exact loop the DISTINCT origin host exists to prevent). The
 * fetch always carries the AUTHENTICATED bypass header (the exact HMAC
 * over this path), so a request that reaches the edge again by
 * misconfiguration can prove it is the script's own fetch and not a
 * viewer's. The origin response's status, headers and body are preserved;
 * only Cache-Control is overwritten with the caller's policy.
 */
async function fetchFromOrigin(
  deps: EdgeDeps,
  req: Request,
  path: string,
  cacheControl: string,
): Promise<Response> {
  const host = deps.originHost.endsWith("/")
    ? deps.originHost.slice(0, -1)
    : deps.originHost;
  const target = `${host}${path.startsWith("/") ? path : `/${path}`}`;
  // Preserve the client's own headers (Range, If-None-Match, ...): a player's
  // byte-range/conditional request must reach the origin unchanged. Only the
  // internal bypass header is replaced with this script's authenticated value.
  const requestHeaders = new Headers();
  req.headers.forEach((value, name) => {
    if (name.toLowerCase() !== ORIGIN_FETCH_HEADER.toLowerCase()) {
      requestHeaders.set(name, value);
    }
  });
  requestHeaders.set(
    ORIGIN_FETCH_HEADER,
    originFetchHeaderValue(deps.tokenSecret, path),
  );
  const res = await deps.fetcher.fetch(target, { headers: requestHeaders });
  const headers = new Headers(res.headers);
  headers.set("Cache-Control", cacheControl);
  return new Response(res.body, { status: res.status, headers: headers });
}

function textResponse(status: number, message: string): Response {
  return new Response(`${message}\n`, {
    status: status,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": MANIFEST_CACHE_CONTROL,
    },
  });
}

/**
 * The Bunny middleware `onClientRequest` hook.
 *
 * Returns a Response to short-circuit the request (a personalised manifest
 * or an error), or the original Request to let Bunny continue to the cache
 * and origin (segments, init segments, thumbnails, and the authenticated
 * origin-fetch bypass).
 */
export async function handleClientRequest(
  req: Request,
  deps: EdgeDeps,
): Promise<Request | Response> {
  // CONFIG GUARD (fail closed): nothing may be served without a secret and
  // an origin host. An EMPTY tokenSecret would make HMAC verification accept
  // any forged token, and an empty originHost would turn every canonical
  // fetch into a relative URL that throws. The runtime supplies both from
  // Bunny environment variables/secrets; an unconfigured script MUST answer
  // 500 before any other logic - never open.
  if (!deps.tokenSecret || !deps.originHost) {
    return textResponse(500, "vg-edge: not configured");
  }

  // CORS (manifest responses only): an absent or empty corsOrigin falls
  // back to "*", because an empty Access-Control-Allow-Origin value would
  // be invalid. This only decides which web origins may READ responses -
  // it has no effect on authentication.
  const allowOrigin =
    deps.corsOrigin && deps.corsOrigin.length > 0 ? deps.corsOrigin : "*";

  const url = new URL(req.url);
  const path = url.pathname;

  // RECURSION GUARD (authenticated): a request carrying the bypass header
  // claims to be one of this script's own origin fetches. The claim is only
  // believed when the header value equals the recomputed HMAC for THIS
  // request path AND the request was addressed to the origin host itself.
  // A PRESENT-BUT-INVALID value (a plain "1", a forged or replayed HMAC, or
  // a request aimed at the edge's public hostname) is rejected with 403 and
  // never falls through to the token check. A genuine match passes the
  // request through to Bunny's cache/origin, which ends the loop.
  if (req.headers.get(ORIGIN_FETCH_HEADER) !== null) {
    if (!isAuthorisedOriginFetch(req, deps.tokenSecret, deps.originHost)) {
      return textResponse(403, "vg-edge: invalid origin-fetch header");
    }
    return req;
  }

  if (!isManifestPath(path)) {
    // Segments, init segments, thumbnails: let Bunny serve them from its
    // cache/origin. The script does not touch their bytes.
    return req;
  }

  const token = url.searchParams.get("vg_token");
  if (token === null || token.length === 0) {
    return textResponse(403, "vg-edge: missing vg_token");
  }

  const now = deps.now ?? (() => Math.floor(Date.now() / 1000));
  let claims;
  try {
    claims = await verifyToken(token, deps.tokenSecret, now());
  } catch (err) {
    const reason = err instanceof Error ? err.message : "invalid vg_token";
    return textResponse(403, `vg-edge: ${reason}`);
  }

  // JOB BINDING: the verified token is valid for ONE job. The requested path
  // must live under that job's prefix, or one short-lived token for video A
  // would unlock every other video's canonical manifests. `path` is the URL
  // parser's normalised pathname and the SAME string is what gets fetched
  // from origin, so a plain prefix check binds the token to exactly the
  // objects this request can reach. The fetch happens only AFTER this check.
  if (!path.startsWith(`/${claims.job_id}/`)) {
    return textResponse(403, "vg-edge: token does not cover this job");
  }

  // ORIGIN HOST: `deps.originHost` must be a hostname DISTINCT from this
  // edge's own public hostname (see EdgeDeps): fetchCanonicalManifest below
  // targets `originHost + path` with the authenticated bypass header. If it
  // pointed back at the edge, this canonical fetch would re-enter the script
  // WITH a valid bypass header; the bypass hook above passes it through, so
  // the loop ends at the origin/cache instead of recursing.
  const canonical = await fetchCanonicalManifest(
    deps.originHost,
    path,
    deps.tokenSecret,
    deps.fetcher,
  );

  if (path.endsWith(".m3u8")) {
    // TOKEN PROPAGATION: players resolve the URIs inside a master playlist
    // (the variant URI after each #EXT-X-STREAM-INF and the audio URI in
    // #EXT-X-MEDIA) relative to the master URL and DROP the master's own
    // query string. The verified token must therefore be written INTO the
    // child URIs, or every media/audio playlist request arrives here
    // without vg_token and is rejected (403) right after the first master
    // fetch. Media playlists need no propagation: their segment URIs are
    // rewritten per segment below and the player reuses the token it got
    // with the parent URI.
    const body = await personalizeHls(
      canonical,
      claims.job_id,
      claims.seed,
      token,
    );
    return new Response(body, {
      status: 200,
      headers: manifestHeaders("application/vnd.apple.mpegurl", allowOrigin),
    });
  }

  const body = await personalizeDash(canonical, claims.seed);
  return new Response(body, {
    status: 200,
    headers: manifestHeaders("application/dash+xml", allowOrigin),
  });
}

/**
 * The full request handler retained for offline tests and for a standalone
 * deployment. Injected deps keep it testable: `fetcher` stands in for the
 * runtime origin fetch, `now` for the clock.
 *
 * The Bunny middleware runtime uses `handleClientRequest` instead; this
 * function performs the origin fetch itself when that hook would pass the
 * request through.
 */
export async function handleRequest(
  req: Request,
  deps: EdgeDeps,
): Promise<Response> {
  const result = await handleClientRequest(req, deps);
  if (result instanceof Response) {
    return result;
  }

  const path = new URL(req.url).pathname;
  return await fetchFromOrigin(
    deps,
    req,
    path,
    isManifestPath(path) ? MANIFEST_CACHE_CONTROL : SEGMENT_CACHE_CONTROL,
  );
}

/**
 * The Bunny middleware `onClientResponse` hook: keep successful
 * segment/init/thumbnail responses immutable. Personalised manifests are
 * short-circuited in `onClientRequest`, so they are never touched here,
 * and error responses keep their own cache headers.
 */
export function handleClientResponse(req: Request, res: Response): Response {
  const path = new URL(req.url).pathname;
  if (isManifestPath(path) || (res.status !== 200 && res.status !== 206)) {
    return res;
  }

  const headers = new Headers(res.headers);
  headers.set("Cache-Control", SEGMENT_CACHE_CONTROL);
  return new Response(res.body, {
    status: res.status,
    statusText: res.statusText,
    headers,
  });
}
