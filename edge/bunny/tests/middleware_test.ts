/**
 * Bunny middleware hook tests (CONTRACT.md section 7).
 *
 * Bunny's Pull Zone middleware API has two hooks:
 *   - onClientRequest: return a Response to short-circuit, or the original
 *     Request to continue to cache/origin.
 *   - onClientResponse: adjust a response from cache/origin.
 *
 * These tests exercise the pure hook functions without importing the Bunny
 * SDK (which starts a local HTTP listener on import).
 */

import {
  type EdgeDeps,
  type Fetcher,
  handleClientRequest,
  handleClientResponse,
} from "../src/index.ts";
import {
  ORIGIN_FETCH_HEADER,
  originFetchHeaderValue,
} from "../src/origin.ts";
import { issueTokenForTests, type VgTokenClaims } from "../src/token.ts";

const SECRET = "test-secret";
const NOW = 1_800_000_000;
const ORIGIN_HOST = "https://canonical.example.net";
const SEED = "vg-test-seed-0001";

function claims(overrides: Partial<VgTokenClaims> = {}): VgTokenClaims {
  return {
    job_id: "job-1",
    video_public_id: "video-1",
    session_id: "sess-1",
    seed: SEED,
    exp: NOW + 3600,
    ...overrides,
  };
}

class FakeFetcher implements Fetcher {
  calls: number = 0;

  constructor(private readonly body: string) {}

  fetch(_input: string | URL, _init?: RequestInit): Promise<Response> {
    this.calls += 1;
    return Promise.resolve(new Response(this.body, { status: 200 }));
  }
}

function deps(fetcher: Fetcher, overrides: Partial<EdgeDeps> = {}): EdgeDeps {
  return {
    originHost: ORIGIN_HOST,
    tokenSecret: SECRET,
    fetcher,
    now: () => NOW,
    ...overrides,
  };
}

function assert(cond: boolean, message: string): asserts cond {
  if (!cond) {
    throw new Error(message);
  }
}

Deno.test("middleware: unconfigured edge returns 500 before anything else", async () => {
  const fetcher = new FakeFetcher("#EXTM3U\n");
  const result = await handleClientRequest(
    new Request("https://edge.example/job-1/hls/master.m3u8"),
    deps(fetcher, { tokenSecret: "" }),
  );
  assert(result instanceof Response, "must be a short-circuit Response");
  assert(result.status === 500, "unconfigured must be 500");
  assert(fetcher.calls === 0, "no origin fetch when unconfigured");
});

Deno.test("middleware: non-manifest requests pass through to Bunny", async () => {
  const fetcher = new FakeFetcher("SEG");
  const req = new Request(
    "https://edge.example/job-1/a/720p/seg_00001.m4s",
  );
  const result = await handleClientRequest(req, deps(fetcher));
  assert(result instanceof Request, "segments must continue to cache/origin");
  assert(result === req, "the original Request must be returned unchanged");
  assert(fetcher.calls === 0, "the script must not fetch segments itself");
});

Deno.test("middleware: a valid origin-fetch bypass passes through", async () => {
  const fetcher = new FakeFetcher("#EXTM3U\n");
  const path = "/job-1/hls/master.m3u8";
  const req = new Request(`https://canonical.example.net${path}`, {
    headers: { [ORIGIN_FETCH_HEADER]: originFetchHeaderValue(SECRET, path) },
  });
  const result = await handleClientRequest(req, deps(fetcher));
  assert(result instanceof Request, "a genuine self-fetch must pass through");
  assert(result === req, "the Request must be returned unchanged");
  assert(fetcher.calls === 0, "Bunny, not the script, fetches the canonical object");
});

Deno.test("middleware: an invalid origin-fetch header is rejected", async () => {
  const fetcher = new FakeFetcher("#EXTM3U\n");
  const req = new Request("https://canonical.example.net/job-1/hls/master.m3u8", {
    headers: { [ORIGIN_FETCH_HEADER]: "1" },
  });
  const result = await handleClientRequest(req, deps(fetcher));
  assert(result instanceof Response, "a forged bypass must short-circuit");
  assert(result.status === 403, "a forged bypass must be 403");
  assert(fetcher.calls === 0, "no origin fetch for a forged bypass");
});

Deno.test("middleware: a valid token returns a personalised manifest", async () => {
  const canonical = [
    "#EXTM3U",
    "#EXTINF:2.00000,",
    "/job-1/__VG_VARIANT__/720p/seg_00001.m4s",
    "",
  ].join("\n");
  const fetcher = new FakeFetcher(canonical);
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const result = await handleClientRequest(req, deps(fetcher));
  assert(result instanceof Response, "a manifest must short-circuit");
  assert(result.status === 200, "valid token must be 200");
  assert(fetcher.calls === 1, "the script fetches the canonical manifest once");
  const body = await result.text();
  assert(body.includes("/job-1/b/720p/seg_00001.m4s"), "golden seed -> b");
  assert(!body.includes("__VG_VARIANT__"), "placeholder must be replaced");
});

Deno.test("middleware: segment responses get the immutable cache header", () => {
  const res = new Response("SEG", { status: 200 });
  const out = handleClientResponse(
    new Request("https://edge.example/job-1/a/720p/seg_00001.m4s"),
    res,
  );
  assert(
    out.headers.get("Cache-Control") ===
      "public, max-age=31536000, immutable",
    "segments must be immutable",
  );
});

Deno.test("middleware: manifest and error responses keep their headers", () => {
  const manifest = new Response("#EXTM3U", {
    status: 200,
    headers: { "Cache-Control": "private, no-store" },
  });
  const manifestOut = handleClientResponse(
    new Request("https://edge.example/job-1/hls/media_720p.m3u8"),
    manifest,
  );
  assert(
    manifestOut.headers.get("Cache-Control") === "private, no-store",
    "manifests must stay no-store",
  );

  const missing = new Response("nope", { status: 404 });
  const missingOut = handleClientResponse(
    new Request("https://edge.example/job-1/a/720p/seg_00001.m4s"),
    missing,
  );
  assert(
    missingOut.headers.get("Cache-Control") === null,
    "error responses must not be marked immutable",
  );
});
