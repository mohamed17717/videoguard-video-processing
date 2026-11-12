/**
 * Origin fetch + edge handler tests (CONTRACT.md section 7):
 *   - the canonical fetch carries the AUTHENTICATED bypass header
 *     X-VG-Origin-Fetch: hex(HMAC_SHA256(secret, "origin-fetch:" + path)),
 *   - incoming requests with a VALID bypass header (HMAC + origin hostname)
 *     are answered with ONE direct origin fetch of originHost + path,
 *   - non-manifest paths are fetched from originHost + path with the SAME
 *     authenticated header (never the edge's own public URL) and served
 *     with the immutable cache header,
 *   - manifests verify the token, personalise and answer no-store,
 *   - missing/tampered tokens are rejected.
 * Everything runs offline: the fetcher and the clock are injected.
 */

import { handleRequest, type EdgeDeps, type Fetcher } from "../src/index.ts";
import {
  fetchCanonicalManifest,
  isAuthorisedOriginFetch,
  ORIGIN_FETCH_HEADER,
  originFetchHeaderValue,
} from "../src/origin.ts";
import { issueTokenForTests, type VgTokenClaims } from "../src/token.ts";

const SECRET = "test-secret";
const NOW = 1_800_000_000;

const GOLDEN_SEED = "vg-test-seed-0001"; // n0=b, n1=b, n2=a

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

function deps(fetcher: Fetcher): EdgeDeps {
  return {
    originHost: "https://canonical.example.net",
    tokenSecret: SECRET,
    fetcher: fetcher,
    now: () => NOW,
  };
}

// ---------------------------------------------------------------- origin --

Deno.test("fetchCanonicalManifest sends the authenticated bypass header and returns the text", async () => {
  const fetcher = new FakeFetcher(() =>
    new Response("#EXTM3U\ncanonical\n", { status: 200 })
  );
  const text = await fetchCanonicalManifest(
    "https://canonical.example.net",
    "/job-1/hls/media_720p.m3u8",
    SECRET,
    fetcher,
  );
  assert(text.includes("canonical"), "manifest text must be returned");
  assert(fetcher.calls.length === 1, "exactly one fetch");
  assert(
    fetcher.calls[0].url ===
      "https://canonical.example.net/job-1/hls/media_720p.m3u8",
    `unexpected URL: ${fetcher.calls[0].url}`,
  );
  const header = bypassHeaderValue(fetcher.calls[0].headers);
  assert(
    header === originFetchHeaderValue(SECRET, "/job-1/hls/media_720p.m3u8"),
    `the bypass header must carry the path HMAC, got ${header}`,
  );
  assert(header !== "1", "a plain \"1\" must never be sent");
});

Deno.test("fetchCanonicalManifest rejects non-2xx responses", async () => {
  const fetcher = new FakeFetcher(() => new Response("nope", { status: 404 }));
  let threw = "";
  try {
    await fetchCanonicalManifest(
      "https://x.example",
      "/job-1/hls/a.m3u8",
      SECRET,
      fetcher,
    );
  } catch (err) {
    threw = err instanceof Error ? err.message : String(err);
  }
  assert(threw.includes("404"), `expected a 404 error, got: ${threw}`);
});

Deno.test("isAuthorisedOriginFetch: true only for the right HMAC on the origin hostname", () => {
  const path = "/job-1/hls/a.m3u8";
  const value = originFetchHeaderValue(SECRET, path);
  const ok = new Request(`https://canonical.example.net${path}`, {
    headers: { [ORIGIN_FETCH_HEADER]: value },
  });
  const plainOne = new Request(`https://canonical.example.net${path}`, {
    headers: { [ORIGIN_FETCH_HEADER]: "1" },
  });
  const missing = new Request(`https://canonical.example.net${path}`);
  const forged = new Request(`https://canonical.example.net${path}`, {
    headers: {
      [ORIGIN_FETCH_HEADER]: originFetchHeaderValue("wrong-secret", path),
    },
  });
  const replayedForOtherPath = new Request(
    `https://canonical.example.net/job-1/hls/other.m3u8`,
    { headers: { [ORIGIN_FETCH_HEADER]: value } },
  );
  const wrongHostname = new Request(`https://edge.example${path}`, {
    headers: { [ORIGIN_FETCH_HEADER]: value },
  });
  assert(isAuthorisedOriginFetch(ok, SECRET, "https://canonical.example.net"),
    "a genuine origin fetch must be detected");
  assert(!isAuthorisedOriginFetch(plainOne, SECRET, "https://canonical.example.net"),
    "a plain \"1\" must never bypass");
  assert(!isAuthorisedOriginFetch(missing, SECRET, "https://canonical.example.net"),
    "a missing header must never bypass");
  assert(!isAuthorisedOriginFetch(forged, SECRET, "https://canonical.example.net"),
    "a forged HMAC must never bypass");
  assert(
    !isAuthorisedOriginFetch(replayedForOtherPath, SECRET, "https://canonical.example.net"),
    "an HMAC computed for another path must never bypass",
  );
  assert(!isAuthorisedOriginFetch(wrongHostname, SECRET, "https://canonical.example.net"),
    "a valid HMAC on the wrong hostname must never bypass");
});

// -------------------------------------------------------------- handler --

Deno.test("non-manifest path: pass-through with the immutable cache header", async () => {
  const fetcher = new FakeFetcher(() =>
    new Response("SEG_BYTES", {
      status: 200,
      headers: { "Content-Type": "video/mp4" },
    })
  );
  const req = new Request("https://edge.example/job-1/a/720p/seg_00001.m4s?vg_token=irrelevant");
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "segments pass through with 200");
  assert(
    (await res.text()) === "SEG_BYTES",
    "segment bytes must not be touched",
  );
  assert(
    res.headers.get("Cache-Control") === "public, max-age=31536000, immutable",
    `segments must be immutable, got ${res.headers.get("Cache-Control")}`,
  );
  // #21: the fetch targets originHost + pathname - NEVER the public request
  // URL (the edge's own hostname) - and carries the authenticated bypass
  // header.
  assert(fetcher.calls.length === 1, "exactly one origin fetch");
  assert(
    fetcher.calls[0].url ===
      "https://canonical.example.net/job-1/a/720p/seg_00001.m4s",
    `the segment fetch must target originHost + path, got ${fetcher.calls[0].url}`,
  );
  const header = bypassHeaderValue(fetcher.calls[0].headers);
  assert(
    header === originFetchHeaderValue(SECRET, "/job-1/a/720p/seg_00001.m4s"),
    `the segment fetch must carry the path HMAC, got ${header}`,
  );
});

Deno.test("manifest without vg_token is rejected (403)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const req = new Request("https://edge.example/job-1/hls/media_720p.m3u8");
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "missing token must be 403");
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    "error responses must not be cacheable",
  );
  assert(fetcher.calls.length === 0, "no origin fetch before the token check");
});

Deno.test("expired token is rejected (403)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const token = await issueTokenForTests(claims({ exp: NOW - 10 }), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "expired token must be 403");
  assert(fetcher.calls.length === 0, "no origin fetch for an expired token");
});

Deno.test("tampered token is rejected (403)", async () => {
  const fetcher = new FakeFetcher(() => new Response("#EXTM3U", { status: 200 }));
  const good = await issueTokenForTests(claims(), SECRET);
  const other = await issueTokenForTests(claims({ job_id: "job-2" }), SECRET);
  const forged = `${good.split(".")[0]}.${other.split(".")[1]}`;
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${forged}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 403, "tampered token must be 403");
});

Deno.test("HLS manifest: personalised with the golden pattern, no-store, right content type", async () => {
  const canonical = [
    "#EXTM3U",
    "#EXT-X-VERSION:6",
    '#EXT-X-MAP:URI="/job-1/init/720p/init.mp4"',
    "#EXTINF:2.00000,",
    "/job-1/__VG_VARIANT__/720p/seg_00001.m4s",
    "#EXTINF:2.00000,",
    "/job-1/__VG_VARIANT__/720p/seg_00002.m4s",
    "#EXT-X-ENDLIST",
    "",
  ].join("\n");
  const fetcher = new FakeFetcher(() => new Response(canonical, { status: 200 }));
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/hls/media_720p.m3u8?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));

  assert(res.status === 200, "valid token must be 200");
  assert(
    res.headers.get("Content-Type") === "application/vnd.apple.mpegurl",
    `HLS content type, got ${res.headers.get("Content-Type")}`,
  );
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    `manifests must be no-store, got ${res.headers.get("Cache-Control")}`,
  );
  const body = await res.text();
  assert(body.includes("/job-1/b/720p/seg_00001.m4s"), `seg1 -> b:\n${body}`);
  assert(body.includes("/job-1/b/720p/seg_00002.m4s"), `seg2 -> b:\n${body}`);
  assert(!body.includes("__VG_VARIANT__"), "token must be gone");
  assert(
    body.includes('#EXT-X-MAP:URI="/job-1/init/720p/init.mp4"'),
    "init URL untouched",
  );

  // The origin fetch carried the authenticated bypass header.
  assert(fetcher.calls.length === 1, "one canonical fetch");
  const header = bypassHeaderValue(fetcher.calls[0].headers);
  assert(
    header === originFetchHeaderValue(SECRET, "/job-1/hls/media_720p.m3u8"),
    `canonical fetch must carry the path HMAC, got ${header}`,
  );
});

Deno.test("DASH manifest: personalised, no-store, right content type", async () => {
  const canonical = [
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">',
    '  <SegmentTemplate initialization="/job-1/init/720p/init.mp4" media="/job-1/__VG_VARIANT__/720p/seg_$Number%05d$.m4s" duration="2"/>',
    "</MPD>",
  ].join("\n");
  const fetcher = new FakeFetcher(() => new Response(canonical, { status: 200 }));
  const token = await issueTokenForTests(claims(), SECRET);
  const req = new Request(
    `https://edge.example/job-1/dash/stream.mpd?vg_token=${token}`,
  );
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "valid token must be 200");
  assert(
    res.headers.get("Content-Type") === "application/dash+xml",
    `DASH content type, got ${res.headers.get("Content-Type")}`,
  );
  const body = await res.text();
  assert(body.includes("/job-1/b/720p/"), `one variant (b):\n${body}`);
  assert(!body.includes("__VG_VARIANT__"), "token must be gone");
  assert(!body.includes("<SegmentList"), "no SegmentList");
});

Deno.test("recursion guard: an authenticated bypass request passes straight through", async () => {
  const canonical = "#EXTM3U\n#EXTINF:2.0,\ncanonical-uri\n";
  const fetcher = new FakeFetcher(() => new Response(canonical, { status: 200 }));
  // NO token in the URL - the request IS the script's own origin fetch:
  // correct HMAC for the path AND the request addressed to the origin host.
  const path = "/job-1/hls/media_720p.m3u8";
  const req = new Request(`https://canonical.example.net${path}`, {
    headers: {
      [ORIGIN_FETCH_HEADER]: originFetchHeaderValue(SECRET, path),
    },
  });
  const res = await handleRequest(req, deps(fetcher));
  assert(res.status === 200, "bypass request passes through");
  const body = await res.text();
  assert(body.includes("canonical-uri"), "canonical text returned untouched");
  assert(!body.includes("/job-1/b/"), "no personalisation on the bypass path");
  assert(
    res.headers.get("Cache-Control") === "private, no-store",
    "canonical manifest responses stay no-store",
  );
  // #21: the bypass branch performs exactly ONE origin fetch of
  // originHost + path (no second self-fetch through the public URL).
  assert(fetcher.calls.length === 1, "exactly one origin fetch - no second self-fetch");
  assert(
    fetcher.calls[0].url === `https://canonical.example.net${path}`,
    `the bypass branch must fetch originHost + path, got ${fetcher.calls[0].url}`,
  );
  assert(
    bypassHeaderValue(fetcher.calls[0].headers) ===
      originFetchHeaderValue(SECRET, path),
    "the bypass branch's origin fetch carries the authenticated header",
  );
});
