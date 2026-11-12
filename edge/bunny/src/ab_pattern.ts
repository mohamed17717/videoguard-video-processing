/**
 * A/B variant pattern (CONTRACT.md section 7) + the small HMAC-SHA256 core
 * it needs.
 *
 * Exact algorithm from the contract:
 *   digest  = HMAC_SHA256(seed.encode(), f"vg-ab-{n}".encode())
 *   variant = "a" if digest[0] & 1 == 0 else "b"
 *
 * The HMAC is a small synchronous implementation (WebCrypto is async and the
 * per-segment HLS substitution would chain many awaits). It is offline, has
 * no dependencies, and the Deno tests cross-check it against WebCrypto.
 *
 * GOLDEN VECTOR (verified with Python hmac/sha256; asserted in
 * tests/ab_pattern_test.ts):
 *   seed = "vg-test-seed-0001", n = 0..7 ->
 *     ["b","b","a","a","a","a","a","a"]
 *   first digest bytes (hex):
 *     n0 = 1d4c3cffe1118ecc
 *     n1 = 9f4e0305e14adec3
 *     n2 = e85502770d355712
 *     n3 = faffb0faf5efbb1c
 */

/** One SHA-256 round constant per round (FIPS 180-4). */
const SHA_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotr(x: number, n: number): number {
  return (x >>> n) | (x << (32 - n));
}

/** SHA-256 of `data` (FIPS 180-4). Input stays under 2^29 bytes here. */
export function sha256(data: Uint8Array): Uint8Array {
  const len = data.length;
  // padded length: message + 0x80 + zeros + 8-byte length, multiple of 64.
  const totalLen = (len + 9 + 63) & ~63;
  const padded = new Uint8Array(totalLen);
  padded.set(data);
  padded[len] = 0x80;
  const bitLen = len * 8;
  const dv = new DataView(padded.buffer);
  dv.setUint32(totalLen - 8, Math.floor(bitLen / 4294967296), false);
  dv.setUint32(totalLen - 4, bitLen % 4294967296, false);

  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
  let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;

  const w = new Uint32Array(64);
  for (let off = 0; off < totalLen; off += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = dv.getUint32(off + i * 4, false);
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }

    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + SHA_K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + t1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) >>> 0;
    }

    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  const out = new Uint8Array(32);
  const odv = new DataView(out.buffer);
  odv.setUint32(0, h0, false);
  odv.setUint32(4, h1, false);
  odv.setUint32(8, h2, false);
  odv.setUint32(12, h3, false);
  odv.setUint32(16, h4, false);
  odv.setUint32(20, h5, false);
  odv.setUint32(24, h6, false);
  odv.setUint32(28, h7, false);
  return out;
}

/** HMAC-SHA256 (RFC 2104): H((key ^ opad) || H((key ^ ipad) || message)). */
export function hmacSha256(key: Uint8Array, message: Uint8Array): Uint8Array {
  const BLOCK = 64;
  const k = key.length > BLOCK ? sha256(key) : key;
  const ipadKey = new Uint8Array(BLOCK);
  const opadKey = new Uint8Array(BLOCK);
  ipadKey.set(k);
  opadKey.set(k);
  for (let i = 0; i < BLOCK; i++) {
    ipadKey[i] ^= 0x36;
    opadKey[i] ^= 0x5c;
  }
  const inner = new Uint8Array(BLOCK + message.length);
  inner.set(ipadKey);
  inner.set(message, BLOCK);
  const innerHash = sha256(inner);
  const outer = new Uint8Array(BLOCK + 32);
  outer.set(opadKey);
  outer.set(innerHash, BLOCK);
  return sha256(outer);
}

function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/** First 8 digest bytes as hex - test helper for the golden vector. */
export function digestPrefix8Hex(seed: string, n: number): string {
  const digest = hmacSha256(utf8(seed), utf8(`vg-ab-${n}`));
  let hex = "";
  for (let i = 0; i < 8; i++) {
    hex += digest[i].toString(16).padStart(2, "0");
  }
  return hex;
}

/**
 * The variant for segment index `n` of a session with `seed`.
 * Contract-exact: HMAC_SHA256(seed, "vg-ab-" + n), first byte, LSB.
 */
export async function pattern(seed: string, n: number): Promise<string> {
  const digest = hmacSha256(utf8(seed), utf8(`vg-ab-${n}`));
  return (digest[0] & 1) === 0 ? "a" : "b";
}

/** The variants for `count` segment indexes: n = 0 .. count-1. */
export async function patternForCount(
  seed: string,
  count: number,
): Promise<string[]> {
  const out: string[] = [];
  for (let n = 0; n < count; n++) {
    out.push(await pattern(seed, n));
  }
  return out;
}
