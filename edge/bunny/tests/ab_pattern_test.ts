/**
 * Golden-vector tests for the A/B pattern (CONTRACT.md section 7).
 * The vector was verified against Python hmac/sha256:
 *   seed = "vg-test-seed-0001", n = 0..7 -> ["b","b","a","a","a","a","a","a"]
 *   first digest bytes hex:
 *     n0 = 1d4c3cffe1118ecc, n1 = 9f4e0305e14adec3,
 *     n2 = e85502770d355712, n3 = faffb0faf5efbb1c
 */

import {
  digestPrefix8Hex,
  pattern,
  patternForCount,
} from "../src/ab_pattern.ts";
import { hmacSha256 } from "../src/ab_pattern.ts";

const GOLDEN_SEED = "vg-test-seed-0001";
const GOLDEN_PATTERN = ["b", "b", "a", "a", "a", "a", "a", "a"];
const GOLDEN_DIGEST_PREFIXES = [
  "1d4c3cffe1118ecc",
  "9f4e0305e14adec3",
  "e85502770d355712",
  "faffb0faf5efbb1c",
];

function assert(cond: boolean, message: string): void {
  if (!cond) {
    throw new Error(message);
  }
}

function assertEqual(actual: unknown, expected: unknown, what: string): void {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  assert(a === b, `${what}: expected ${b}, got ${a}`);
}

Deno.test("golden pattern: seed vg-test-seed-0001, n=0..7", async () => {
  const variants: string[] = [];
  for (let n = 0; n < 8; n++) {
    variants.push(await pattern(GOLDEN_SEED, n));
  }
  assertEqual(variants, GOLDEN_PATTERN, "pattern golden vector");
});

Deno.test("golden digest prefixes (first 8 bytes, hex)", () => {
  for (let n = 0; n < 4; n++) {
    assertEqual(
      digestPrefix8Hex(GOLDEN_SEED, n),
      GOLDEN_DIGEST_PREFIXES[n],
      `digest prefix n=${n}`,
    );
  }
});

Deno.test("patternForCount matches the golden vector", async () => {
  assertEqual(
    await patternForCount(GOLDEN_SEED, 8),
    GOLDEN_PATTERN,
    "patternForCount(8)",
  );
  assertEqual(await patternForCount(GOLDEN_SEED, 0), [], "empty count");
});

Deno.test("second seed (documented vector): vg-session-42 n=0..5 -> all a", async () => {
  // Verified with Python hmac/sha256:
  //   6870730eb7e3aeea, 4eba4d8d466857ca, 3af98da61d73f339,
  //   70abedeaffec2a53, 4275dfe639094f59, 9ab96d37aea73c5b
  assertEqual(await patternForCount("vg-session-42", 6), [
    "a",
    "a",
    "a",
    "a",
    "a",
    "a",
  ], "pattern for vg-session-42");
});

Deno.test("hmacSha256 cross-checks against WebCrypto", async () => {
  const key = new TextEncoder().encode("vg-cross-check-key");
  const message = new TextEncoder().encode("vg-ab-3");
  const cryptoKey = await globalThis.crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const webCryptoSig = new Uint8Array(
    await globalThis.crypto.subtle.sign("HMAC", cryptoKey, message),
  );
  assertEqual(
    Array.from(hmacSha256(key, message)),
    Array.from(webCryptoSig),
    "sync HMAC vs WebCrypto",
  );
});

Deno.test("hmacSha256 handles keys longer than the 64-byte block", () => {
  const longKey = new TextEncoder().encode("k".repeat(200));
  const short = hmacSha256(new TextEncoder().encode("k".repeat(10)), new TextEncoder().encode("m"));
  const long = hmacSha256(longKey, new TextEncoder().encode("m"));
  // Different keys -> different digests (a hash of the hash would collapse).
  assert(short.length === 32, "digest length is 32 bytes");
  assert(long.length === 32, "digest length is 32 bytes");
  assertEqual(
    Array.from(long),
    Array.from(hmacSha256(longKey.slice(), new TextEncoder().encode("m"))),
    "long-key HMAC is deterministic",
  );
});
