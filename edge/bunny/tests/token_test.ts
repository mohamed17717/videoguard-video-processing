/**
 * Token tests (CONTRACT.md section 7): valid roundtrip, expired, tampered,
 * missing claims, wrong secret, malformed. All offline via the injectable
 * clock in verifyToken.
 */

import {
  issueTokenForTests,
  TokenError,
  verifyToken,
  type VgTokenClaims,
} from "../src/token.ts";

const SECRET = "test-secret";
const NOW = 1_800_000_000;

function claims(overrides: Partial<VgTokenClaims> = {}): VgTokenClaims {
  return {
    job_id: "job-1",
    video_public_id: "video-1",
    session_id: "sess-1",
    seed: "vg-test-seed-0001",
    exp: NOW + 3600,
    ...overrides,
  };
}

async function assertTokenError(
  fn: () => Promise<unknown>,
  expectedMessage: string,
): Promise<void> {
  try {
    await fn();
  } catch (err) {
    if (!(err instanceof TokenError)) {
      throw new Error(`expected TokenError, got ${String(err)}`);
    }
    if (!err.message.includes(expectedMessage)) {
      throw new Error(
        `expected message containing "${expectedMessage}", got "${err.message}"`,
      );
    }
    return;
  }
  throw new Error(`expected TokenError("${expectedMessage}") but verify passed`);
}

Deno.test("valid token roundtrip", async () => {
  const token = await issueTokenForTests(claims(), SECRET);
  const verified = await verifyToken(token, SECRET, NOW);
  const expected = claims();
  if (verified.job_id !== expected.job_id ||
    verified.video_public_id !== expected.video_public_id ||
    verified.session_id !== expected.session_id ||
    verified.seed !== expected.seed ||
    verified.exp !== expected.exp) {
    throw new Error(`claims mismatch: ${JSON.stringify(verified)}`);
  }
});

Deno.test("expired token is rejected", async () => {
  const token = await issueTokenForTests(claims({ exp: NOW - 1 }), SECRET);
  await assertTokenError(() => verifyToken(token, SECRET, NOW), "expired");
});

Deno.test("exp exactly equal to now is rejected (exp is exclusive)", async () => {
  const token = await issueTokenForTests(claims({ exp: NOW }), SECRET);
  await assertTokenError(() => verifyToken(token, SECRET, NOW), "expired");
});

Deno.test("tampered payload is rejected", async () => {
  const a = await issueTokenForTests(claims({ job_id: "job-A" }), SECRET);
  const b = await issueTokenForTests(claims({ job_id: "job-B" }), SECRET);
  const forged = `${a.split(".")[0]}.${b.split(".")[1]}`;
  await assertTokenError(() => verifyToken(forged, SECRET, NOW), "bad signature");
});

Deno.test("tampered signature is rejected", async () => {
  const token = await issueTokenForTests(claims(), SECRET);
  const forged = `${token.split(".")[0]}.${token.split(".")[1]}x`;
  await assertTokenError(() => verifyToken(forged, SECRET, NOW), "bad signature");
});

Deno.test("wrong secret is rejected", async () => {
  const token = await issueTokenForTests(claims(), SECRET);
  await assertTokenError(
    () => verifyToken(token, "other-secret", NOW),
    "bad signature",
  );
});

Deno.test("missing claim (seed) is rejected", async () => {
  // seed: undefined is dropped by JSON.stringify -> the claim is missing.
  const token = await issueTokenForTests(
    { ...claims(), seed: undefined } as unknown as VgTokenClaims,
    SECRET,
  );
  await assertTokenError(
    () => verifyToken(token, SECRET, NOW),
    "missing claim seed",
  );
});

Deno.test("missing exp is rejected", async () => {
  const token = await issueTokenForTests(
    { ...claims(), exp: undefined } as unknown as VgTokenClaims,
    SECRET,
  );
  await assertTokenError(
    () => verifyToken(token, SECRET, NOW),
    "missing claim exp",
  );
});

Deno.test("non-JSON payload is rejected", async () => {
  // A structurally valid token pair whose payload is not JSON.
  const token = await issueTokenForTests(claims(), SECRET);
  const [payload, sig] = token.split(".");
  // Overwrite the payload with base64url("not json") but keep it unsigned:
  // the signature check must fire first, so expect "bad signature".
  const notJson = btoa("not json").replace(/\+/g, "-").replace(/\//g, "_")
    .replace(/=+$/, "");
  await assertTokenError(
    () => verifyToken(`${notJson}.${sig}`, SECRET, NOW),
    "bad signature",
  );
  void payload;
});

Deno.test("malformed token (wrong shape) is rejected", async () => {
  await assertTokenError(
    () => verifyToken("only-one-part", SECRET, NOW),
    "malformed token",
  );
  await assertTokenError(
    () => verifyToken("a.b.c", SECRET, NOW),
    "malformed token",
  );
});
