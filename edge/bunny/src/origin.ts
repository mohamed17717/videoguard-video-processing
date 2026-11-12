/**
 * Origin fetch for canonical manifests (CONTRACT.md section 7).
 *
 * The edge needs the UNPERSONALISED ("canonical") manifest from origin. Two
 * protections make that safe:
 *
 * 1. The bypass header is AUTHENTICATED. The fetch sends
 *      X-VG-Origin-Fetch: hex(HMAC_SHA256(tokenSecret, "origin-fetch:" + path))
 *    and the incoming-request check recomputes that HMAC and compares it in
 *    constant time (plus, as defence in depth, the request must have been
 *    addressed to the origin host itself). A plain "1" is NOT trusted: any
 *    viewer could otherwise add the header and skip the vg_token check.
 * 2. The fetcher is injectable so tests run fully offline.
 *
 * No S3 credentials ever appear here: the edge fetches through the CDN
 * origin, and Bunny signs the S3 GET with its own AWS identity
 * (AWSSigningEnabled, infra/bunny.tf).
 */

import { hmacSha256 } from "./ab_pattern.ts";
import { safeEqual } from "./token.ts";

/** Bypass header. The VALUE is an HMAC, not a plain flag. */
export const ORIGIN_FETCH_HEADER = "X-VG-Origin-Fetch";

/** Domain-separation prefix for the bypass header HMAC. */
export const ORIGIN_FETCH_PREFIX = "origin-fetch:";

export interface Fetcher {
  fetch(input: string | URL, init?: RequestInit): Promise<Response>;
}

interface HeaderReader {
  headers: { get(name: string): string | null };
}

/** Minimal shape needed to re-derive the expected bypass HMAC for a request. */
interface UrlReader {
  url: string;
}

function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function bytesToHex(bytes: Uint8Array): string {
  let hex = "";
  for (const byte of bytes) {
    hex += byte.toString(16).padStart(2, "0");
  }
  return hex;
}

/**
 * The exact X-VG-Origin-Fetch value the edge's own origin fetch must carry
 * for `path`: hex(HMAC_SHA256(tokenSecret, ORIGIN_FETCH_PREFIX + path)).
 * Only holders of `tokenSecret` (the edge runtime and the token issuer)
 * can produce it.
 */
export function originFetchHeaderValue(
  tokenSecret: string,
  path: string,
): string {
  return bytesToHex(
    hmacSha256(utf8(tokenSecret), utf8(ORIGIN_FETCH_PREFIX + path)),
  );
}

/**
 * True ONLY when an incoming request is one of this script's own origin
 * fetches: the header value matches the recomputed HMAC for the request path
 * (constant-time compare) AND the request was addressed to the origin host
 * itself - so even a leaked value cannot unlock the bypass on the edge's
 * public hostname. A missing, plain ("1") or forged header never bypasses.
 */
export function isAuthorisedOriginFetch(
  req: UrlReader & HeaderReader,
  tokenSecret: string,
  originHost: string,
): boolean {
  const received = req.headers.get(ORIGIN_FETCH_HEADER);
  if (received === null) {
    return false;
  }
  let requestUrl: URL;
  let originUrl: URL;
  try {
    requestUrl = new URL(req.url);
    originUrl = new URL(originHost);
  } catch {
    return false;
  }
  const expected = originFetchHeaderValue(tokenSecret, requestUrl.pathname);
  if (!safeEqual(expected, received)) {
    return false;
  }
  return requestUrl.hostname === originUrl.hostname;
}

/**
 * Fetch the canonical manifest for `path` from `originHost`.
 * The request carries the authenticated bypass header (see above).
 * Throws on any non-2xx response; returns the manifest text.
 */
export async function fetchCanonicalManifest(
  originHost: string,
  path: string,
  tokenSecret: string,
  fetcher: Fetcher,
): Promise<string> {
  const host = originHost.endsWith("/") ? originHost.slice(0, -1) : originHost;
  const url = `${host}${path.startsWith("/") ? path : `/${path}`}`;
  const res = await fetcher.fetch(url, {
    headers: {
      [ORIGIN_FETCH_HEADER]: originFetchHeaderValue(tokenSecret, path),
    },
  });
  if (!res.ok) {
    throw new Error(
      `canonical manifest fetch failed (${res.status}) for ${path}`,
    );
  }
  return await res.text();
}
