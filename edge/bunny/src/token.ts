/**
 * Short-lived signed playback token (CONTRACT.md section 7).
 *
 * Shape:  base64url(payloadJSON) + "." + base64url(HMAC_SHA256(secret, payloadPart))
 * Claims: job_id, video_public_id, session_id, seed, exp (unix seconds).
 *
 * verifyToken rejects: malformed tokens, bad signatures (tampered payload or
 * signature), payloads that are not JSON, missing/empty claims, non-numeric
 * exp, and expired tokens (exp <= now).
 *
 * No network, no environment access: fully offline-testable.
 */

import { hmacSha256 } from "./ab_pattern.ts";

export interface VgTokenClaims {
  job_id: string;
  video_public_id: string;
  session_id: string;
  seed: string;
  exp: number;
}

export class TokenError extends Error {}

const REQUIRED_STRING_CLAIMS = [
  "job_id",
  "video_public_id",
  "session_id",
  "seed",
] as const;

const TOKEN_PARAM = "vg_token";

export function tokenParamName(): string {
  return TOKEN_PARAM;
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlToBytes(text: string): Uint8Array {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}

function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/**
 * Constant-time string compare (XOR accumulator, false early only on a
 * length difference - lengths are not secret). Used for token signatures
 * and re-exported for the authenticated origin-fetch header check.
 */
export function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function signPayload(payloadPart: string, secret: string): string {
  const sig = hmacSha256(utf8(secret), utf8(payloadPart));
  return bytesToBase64Url(sig);
}

/** Serialize claims with a FIXED key order so signatures are reproducible. */
function serializeClaims(claims: VgTokenClaims): string {
  return JSON.stringify({
    job_id: claims.job_id,
    video_public_id: claims.video_public_id,
    session_id: claims.session_id,
    seed: claims.seed,
    exp: claims.exp,
  });
}

/**
 * Issue a signed token. Name says "ForTests" because production tokens are
 * issued by the Go backend (internal/drm / playback session handler); the
 * edge only verifies.
 */
export async function issueTokenForTests(
  claims: VgTokenClaims,
  secret: string,
): Promise<string> {
  const payloadPart = bytesToBase64Url(utf8(serializeClaims(claims)));
  return `${payloadPart}.${signPayload(payloadPart, secret)}`;
}

/**
 * Verify a playback token and return its claims.
 * `nowSeconds` is injectable so expired-token tests are deterministic.
 */
export async function verifyToken(
  token: string,
  secret: string,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): Promise<VgTokenClaims> {
  const parts = token.split(".");
  if (parts.length !== 2 || parts[0].length === 0 || parts[1].length === 0) {
    throw new TokenError("malformed token");
  }
  const [payloadPart, sigPart] = parts;

  if (!safeEqual(signPayload(payloadPart, secret), sigPart)) {
    throw new TokenError("bad signature");
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(base64UrlToBytes(payloadPart)));
  } catch {
    throw new TokenError("payload is not JSON");
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new TokenError("payload is not a JSON object");
  }
  const raw = parsed as Record<string, unknown>;

  for (const claim of REQUIRED_STRING_CLAIMS) {
    const value = raw[claim];
    if (typeof value !== "string" || value.length === 0) {
      throw new TokenError(`missing claim ${claim}`);
    }
  }
  const exp = raw["exp"];
  if (typeof exp !== "number" || !Number.isFinite(exp)) {
    throw new TokenError("missing claim exp");
  }
  if (exp <= nowSeconds) {
    throw new TokenError("expired");
  }

  return {
    job_id: raw["job_id"] as string,
    video_public_id: raw["video_public_id"] as string,
    session_id: raw["session_id"] as string,
    seed: raw["seed"] as string,
    exp: exp,
  };
}
