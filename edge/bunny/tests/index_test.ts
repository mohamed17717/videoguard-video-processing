/**
 * Edge handler security tests (CONTRACT.md section 7), for the three
 * agreed security fixes:
 *
 * B6 - fail closed: without tokenSecret or originHost every request is a
 *      500 "not configured" BEFORE any other logic (no origin fetch, no
 *      pass-through). An empty secret would otherwise make token
 *      verification accept any forgery, and an empty origin host would
 *      break every canonical fetch.
 *
 * B5 - the origin-fetch bypass header is AUTHENTICATED. The value must be
 *      hex(HMAC_SHA256(tokenSecret, "origin-fetch:" + path)) AND the request
 *      must be addressed to the origin host itself. A plain "1", a forged
 *      HMAC, or a valid HMAC on the wrong hostname is a 403 - never a
 *      fall-through to token auth. A MISSING header falls through to the
 *      normal vg_token flow.
 *
 * B4 - the verified token's job_id claim is BOUND to the request path: a
 *      token for job A cannot fetch job B's manifests. The check runs after
 *      verification and BEFORE the canonical origin fetch.
 *
 * #21 - EVERY origin fetch (segments and the bypass branch included)
 *      targets originHost + path - never the edge's own public request
 *      URL - and carries the authenticated X-VG-Origin-Fetch header. The
 *      bypass branch performs exactly ONE origin fetch (no second
 *      self-fetch) and preserves the origin response's status/headers/body.
 *
 * #23 - personalised manifests carry CORS headers; Access-Control-Allow-
 *      Origin comes from deps.corsOrigin and defaults to "*".
 *
 * Everything runs offline: the fetcher and the clock are injected.
 */

import { handleRequest, type EdgeDeps, type Fetcher } from "../src/index.ts";
import {
  ORIGIN_FETCH_HEADER,
  originFetchHeaderValue,
} from "../src/origin.ts";
import { issueTokenForTests, type VgTokenClaims } from "../src/token.ts";

const SECRET = "test-secret";
const NOW = 1_800_000_000;
const ORIGIN_HOST = "https://canonical.example.net";
const GOLDEN_SEED = "vg-test-seed-0001";

function claims(overrides: Partial<VgTokenClaims> = {}): VgTokenClaims {
  return {
    job_id: "job-1",
    video_public_id: "video-1",
    session_id: "sess-1",
    seed: GOLDEN_SEED,
    exp: NOW + 3600,
    ...overrides,
  };
}

class FakeFetcher implements Fetcher {
  calls: Array<{ url: string; headers: Record<string, string> }> = [];

  constructor(
    private readonly respond: (url: string) => Response,
  ) {}

  fetch(input: string | URL, init?: RequestInit): Promise<Response> {
    const headers: Record<string, string> = {};
    if (init && init.headers) {
      const h = new Headers(init.headers);
      h.forEach((value, name) => {
        headers[name] = value;
      });
    }
    this.calls.push({ url: String(input), headers });
    return Promise.resolve(this.respond(String(input)));
  }
}

function assert(cond: boolean, message: string): void {
  if (!cond) {
    throw new Error(message);
  }
}

/** Read the bypass header value case-insensitively. */
function bypassHeaderValue(headers: Record<string, string>): string | null {
  for (const name of Object.keys(headers)) {
    if (name.toLowerCase() === ORIGIN_FETCH_HEADER.toLowerCase()) {
      return headers[name];
    }
  }
  return null;
}

function deps(
  fetcher: Fetcher,
  overrides: Partial<EdgeDeps> = {},
): EdgeDeps {
  return {
    originHost: ORIGIN_HOST,
    tokenSecret: SECRET,
    fetcher: fetcher,
    now: () => NOW,
    ...overrides,
  };
}

// ------------------------------------------------------------------- B6 --

Deno.test("B6: missing tokenSecret fails closed (500, no origin fetch)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher, { tokenSecret: "" }));
  assert(res.status === 500, "an unconfigured edge must answer 500");
  assert(
    (await res.text()).includes("not configured"),
    "the error must say the edge is not configured",
  );
  assert(fetcher.calls.length === 0, "no fetch may happen when unconfigured");
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    "the error must not be cacheable",
  );
});

Deno.test("B6: missing originHost fails closed (500, no origin fetch)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher, { originHost: "" }));
  assert(res.status === 500, "an unconfigured edge must answer 500");
  assert(fetcher.calls.length === 0, "no fetch may happen when unconfigured");
});

Deno.test("B6: an unconfigured edge answers 500 even for segment paths", async () => {
  const fetcher = new FakeFetcher(() => new Response("SEG", { status: 200 }));
  const req = new Request("https://edge.example/job-1/a/720p/seg_00001.m4s");
  const res = await handleRequest(
    req,
    deps(fetcher, { tokenSecret: "", originHost: "" }),
  );
  assert(res.status === 500, "the guard runs before any other logic");
  assert(fetcher.calls.length === 0, "no pass-through when unconfigured");
});

// ------------------------------------------------------------------- B5 --

Deno.test("B5: a plain \"1\" bypass header is rejected (403), never token auth", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  // Without a token: 403 (not a pass-through).
  const req = new Request("https://canonical.example.net/job-1/hls/master.m3u8", {
    headers: { [ORIGIN_FETCH_HEADER]: "1" },
  });
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "a plain \"1\" must never unlock the bypass");
  assert(fetcher.calls.length === 0, "no origin fetch for a bogus header");

  // Even WITH a valid token: still 403 - a present-but-invalid header must
  // not fall through to token auth.
  const token = await issueTokenForTests(claims(), SECRET);
  const reqWithToken = new Request(
    `https://canonical.example.net/job-1/hls/master.m3u8?vg_token=${token}`,
    { headers: { [ORIGIN_FETCH_HEADER]: "1" } },
  );
  const resWithToken = await handleRequest(reqWithToken, deps(fetcher));
  assert(
    resWithToken.status === 403,
    "an invalid bypass header must not fall through to token auth",
  );
  assert(fetcher.calls.length === 0, "still no origin fetch");
});

Deno.test("B5: a forged bypass HMAC is rejected (403, no origin fetch)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const path = "/job-1/hls/master.m3u8";
  // Right shape (64 hex chars) but signed with the WRONG secret.
  const forged = originFetchHeaderValue("attacker-secret", path);
  assert(forged.length === 64, "the forged value looks like a real HMAC");
  const req = new Request(`https://canonical.example.net${path}`, {
    headers: { [ORIGIN_FETCH_HEADER]: forged },
  });
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "a forged HMAC must be rejected");
  assert(fetcher.calls.length === 0, "no origin fetch for a forged HMAC");
});

Deno.test("B5: the correct bypass HMAC passes the canonical manifest through without a token", async () => {
  const canonical = "#EXTM3U\n#EXTINF:2.0,\ncanonical-uri\n";
  const fetcher = new FakeFetcher(() => new Response(canonical, { status: 200 }));
  const path = "/job-1/hls/master.m3u8";
  const req = new Request(`https://canonical.example.net${path}`, {
    headers: {
      [ORIGIN_FETCH_HEADER]: originFetchHeaderValue(SECRET, path),
    },
  });
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "the edge's own fetch must be recognised");
  const body = await res.text();
  assert(body.includes("canonical-uri"), "canonical text returned untouched");
  assert(!body.includes("vg_token="), "no personalisation, no token needed");
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    "canonical manifest responses stay no-store",
  );
  assert(fetcher.calls.length === 1, "one origin fetch - no second self-fetch");
  const call = fetcher.calls[0];
  assert(
    call.url === `${ORIGIN_HOST}${path}`,
    `the bypass branch must fetch originHost + path, got ${call.url}`,
  );
  // The edge fetches origin with its OWN authenticated header (the exact
  // HMAC for this path), never a forwarded copy and never the public URL.
  const header = bypassHeaderValue(call.headers);
  assert(
    header === originFetchHeaderValue(SECRET, path),
    `the origin fetch must carry the authenticated bypass header, got ${header}`,
  );
});

Deno.test("B5: a missing bypass header falls through to normal token auth", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const req = new Request("https://edge.example/job-1/hls/master.m3u8");
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "without the header the vg_token check applies");
  assert(fetcher.calls.length === 0, "no origin fetch before the token check");
});

Deno.test("B5: a valid bypass HMAC on the WRONG hostname is rejected (403)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const path = "/job-1/hls/master.m3u8";
  // Defence in depth: even a genuinely signed HMAC must not unlock the
  // bypass when the request is aimed at the edge's PUBLIC hostname instead
  // of the origin host.
  const req = new Request(`https://edge.example${path}`, {
    headers: {
      [ORIGIN_FETCH_HEADER]: originFetchHeaderValue(SECRET, path),
    },
  });
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "the bypass is bound to the origin hostname");
  assert(fetcher.calls.length === 0, "no origin fetch on the wrong hostname");
});

// ------------------------------------------------------------------- B4 --

Deno.test("B4: a token for job-1 cannot fetch job-2's HLS manifest (403, no origin fetch)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const token = await issueTokenForTests(
    claims({ job_id: "job-1" }),
    SECRET,
  );
  const req = new Request(
    `https://edge.example/job-2/hls/master.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "a cross-job manifest request must be rejected");
  assert(
    (await res.text()).includes("does not cover this job"),
    "the error must name the job binding",
  );
  assert(
    fetcher.calls.length === 0,
    "the canonical fetch must NOT happen for another job's manifest",
  );
});

Deno.test("B4: a token for job-1 cannot fetch job-2's DASH manifest (403, no origin fetch)", async () => {
  const fetcher = new FakeFetcher(() => new Response("<MPD/>", { status: 200 }));
  const token = await issueTokenForTests(
    claims({ job_id: "job-1" }),
    SECRET,
  );
  const req = new Request(
    `https://edge.example/job-2/dash/stream.mpd?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "a cross-job DASH request must be rejected");
  assert(fetcher.calls.length === 0, "no origin fetch for another job");
});

Deno.test("B4: a token for job-1 DOES fetch job-1's manifest (positive control)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U\ncanonical\n", {
    status: 200,
  }));
  const token = await issueTokenForTests(claims({ job_id: "job-1" }), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "the token's own job must still be served");
  assert(fetcher.calls.length === 1, "one canonical fetch");
  assert(
    fetcher.calls[0].url === `${ORIGIN_HOST}/job-1/hls/media_720p.m3u8`,
    `unexpected fetch URL: ${fetcher.calls[0].url}`,
  );
});

Deno.test("B4: the master-playlist flow keeps working with the job binding (token propagated)", async () => {
  const canonical = [
    "#EXTM3U",
    "#EXT-X-VERSION:7",
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="en",DEFAULT=YES,URI="/job-1/hls/audio.m3u8"',
    '#EXT-X-STREAM-INF:BANDWIDTH=3800000,RESOLUTION=1280x720',
    "/job-1/hls/media_720p.m3u8",
    "",
  ].join("\n");
  const fetcher = new FakeFetcher(() => new Response(canonical, { status: 200 }));
  const token = await issueTokenForTests(claims({ job_id: "job-1" }), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/master.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "a matching job must be served");
  const body = await res.text();
  assert(
    body.includes("/job-1/hls/media_720p.m3u8?vg_token=") &&
      body.includes('URI="/job-1/hls/audio.m3u8?vg_token='),
    `the verified token must still be propagated into child URIs:\n${body}`,
  );
});

// ------------------------------------------------------------------- #21 --

Deno.test("#21: segment requests are fetched from originHost + path, never the public edge URL", async () => {
  const fetcher = new FakeFetcher(() =>
    new Response("SEG_BYTES", {
      status: 200,
      headers: { "Content-Type": "video/mp4" },
    })
  );
  // The REQUEST is addressed to the edge's public hostname; the FETCH must
  // not be (it would re-enter this script).
  const req = new Request(
    "https://edge.example/job-1/a/720p/seg_00001.m4s?vg_token=irrelevant",
    { headers: { Range: "bytes=0-99" } },
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "segments still pass through with 200");
  assert(fetcher.calls.length === 1, "exactly one origin fetch");
  const call = fetcher.calls[0];
  assert(
    call.url === `${ORIGIN_HOST}/job-1/a/720p/seg_00001.m4s`,
    `the segment fetch must target originHost + pathname (query dropped), got ${call.url}`,
  );
  assert(
    !call.url.startsWith("https://edge.example/"),
    `the fetch must NEVER target the edge's own public hostname: ${call.url}`,
  );
  const header = bypassHeaderValue(call.headers);
  assert(
    header === originFetchHeaderValue(SECRET, "/job-1/a/720p/seg_00001.m4s"),
    `the origin fetch must carry the authenticated bypass header, got ${header}`,
  );
  assert(header !== "1", "a plain \"1\" must never be sent");
  assert(
    call.headers["range"] === "bytes=0-99",
    "the client's Range header must be forwarded to the origin unchanged",
  );
});

Deno.test("#21: the bypass branch answers with ONE origin fetch that preserves the origin response", async () => {
  const fetcher = new FakeFetcher(() =>
    new Response("origin-body", {
      status: 404,
      headers: { "Content-Type": "application/xml" },
    })
  );
  const path = "/job-1/hls/master.m3u8";
  const req = new Request(`https://canonical.example.net${path}`, {
    headers: {
      [ORIGIN_FETCH_HEADER]: originFetchHeaderValue(SECRET, path),
    },
  });
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 404, "the origin status must be preserved verbatim");
  assert(
    (await res.text()) === "origin-body",
    "the origin body must be preserved verbatim",
  );
  assert(
    res.headers.get("Content-Type") === "application/xml",
    "the origin headers must be preserved",
  );
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    "only Cache-Control is overwritten (manifests stay no-store)",
  );
  assert(fetcher.calls.length === 1, "exactly one origin fetch - no second self-fetch");
});

// ------------------------------------------------------------------- #23 --

Deno.test("#23: personalised HLS manifests carry the default CORS headers", async () => {
  const canonical = [
    "#EXTM3U",
    "#EXTINF:2.00000,",
    "/job-1/__VG_VARIANT__/720p/seg_00001.m4s",
    "",
  ].join("\n");
  const fetcher = new FakeFetcher(() => new Response(canonical, { status: 200 }));
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "the manifest must still be served");
  assert(
    res.headers.get("Access-Control-Allow-Origin") === "*",
    `the default corsOrigin is "*", got ${res.headers.get("Access-Control-Allow-Origin")}`,
  );
  assert(
    res.headers.get("Access-Control-Allow-Methods") === "GET, HEAD, OPTIONS",
    `unexpected allow-methods: ${res.headers.get("Access-Control-Allow-Methods")}`,
  );
  const allowHeaders = res.headers.get("Access-Control-Allow-Headers") ?? "";
  assert(
    allowHeaders.split(",").map((part) => part.trim()).includes("Range"),
    `Range must be in Access-Control-Allow-Headers, got "${allowHeaders}"`,
  );
  const expose = res.headers.get("Access-Control-Expose-Headers") ?? "";
  for (const name of ["Content-Length", "Content-Range", "ETag"]) {
    assert(
      expose.split(",").map((part) => part.trim()).includes(name),
      `${name} must be exposed, got "${expose}"`,
    );
  }
  const vary = res.headers.get("Vary") ?? "";
  assert(
    vary.split(",").map((part) => part.trim()).includes("Origin"),
    `Vary must include Origin, got "${vary}"`,
  );
  // Cache-control semantics unchanged by the CORS fix.
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    `manifests must stay no-store, got ${res.headers.get("Cache-Control")}`,
  );
});

Deno.test("#23: corsOrigin overrides Access-Control-Allow-Origin on DASH manifests too", async () => {
  const canonical = [
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">',
    '  <SegmentTemplate media="/job-1/__VG_VARIANT__/720p/seg_$Number%05d$.m4s" duration="2"/>',
    "</MPD>",
  ].join("\n");
  const fetcher = new FakeFetcher(() => new Response(canonical, { status: 200 }));
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/dash/stream.mpd?vg_token=${token}`,
  );
  const res = await handleRequest(
    req,
    deps(fetcher, { corsOrigin: "https://player.example" }),
  );
  assert(res.status === 200, "the manifest must still be served");
  assert(
    res.headers.get("Access-Control-Allow-Origin") === "https://player.example",
    `the configured corsOrigin must be returned verbatim, got ${res.headers.get("Access-Control-Allow-Origin")}`,
  );
  assert(
    res.headers.get("Content-Type") === "application/dash+xml",
    `DASH content type unchanged, got ${res.headers.get("Content-Type")}`,
  );
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    `manifests must stay no-store, got ${res.headers.get("Cache-Control")}`,
  );
});

Deno.test("#23: an empty corsOrigin falls back to \"*\"", async () => {
  const fetcher = new FakeFetcher(() =>
    new Response("#EXTM3U\n", { status: 200 })
  );
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher, { corsOrigin: "" }));
  assert(res.status === 200, "the manifest must still be served");
  assert(
    res.headers.get("Access-Control-Allow-Origin") === "*",
    `an empty corsOrigin must fall back to "*", got ${res.headers.get("Access-Control-Allow-Origin")}`,
  );
});
