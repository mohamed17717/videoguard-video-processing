# Bunny edge script — playback personalisation

TypeScript that runs at the CDN edge (Bunny Edge Scripting) and turns a
canonical A/B manifest into a per-session manifest. Written and tested with
Deno; the exact algorithms come from `../../CONTRACT.md` section 7.

## What it does

1. Handles ONLY `*.m3u8` and `*.mpd`. Everything else (segments, init
   segments, thumbnails) is a pass-through with
   `Cache-Control: public, max-age=31536000, immutable` — segments are shared
   between variants and cacheable forever.
2. Verifies the short-lived signed playback token (`?vg_token=`). Claims:
   `job_id`, `video_public_id`, `session_id`, `seed`, `exp`. HMAC-SHA256 over
   the payload; expired / tampered / missing-claim tokens are rejected with
   403. The verified token is then BOUND to the request path: a manifest is
   served only when the path starts with `/<claims.job_id>/`. A valid token
   for job A therefore cannot fetch job B's `master.m3u8` or `stream.mpd` —
   one leaked session token does not unlock every video (403 before any
   origin fetch).
3. Fetches the **canonical** (unpersonalised) manifest through the bypass
   header `X-VG-Origin-Fetch`, whose value is AUTHENTICATED:
   `hex(HMAC_SHA256(tokenSecret, "origin-fetch:" + path))`. If that fetch
   re-enters the edge script, the script recomputes the HMAC for the incoming
   path, compares it in constant time, and — additionally requiring that the
   request was addressed to the origin host itself — passes the request
   straight through: that is the recursion guard. A PRESENT-BUT-INVALID
   header (a plain `"1"`, a forged or replayed HMAC, or a valid HMAC on the
   edge's public hostname instead of the origin host) is rejected with 403
   and never falls through to token auth; a MISSING header gets the normal
   `vg_token` flow. The origin host MUST be a hostname distinct from the
   edge's own public hostname (see "Deployment to Bunny").
4. Personalises:
   - **HLS media playlists**: per-segment substitution — the literal
     `__VG_VARIANT__` token in each media-playlist segment URI becomes the
     variant for that segment index (`#EXTINF` count; first segment = index
     0). The `#EXT-X-MAP` init URL is untouched (one canonical init per
     quality, shared by A and B).
   - **HLS master playlists (token propagation)**: a master has no
     `__VG_VARIANT__`, so the only change is that the edge writes the
     verified `vg_token` INTO its child playlist URIs — the variant URI on
     the line after each `#EXT-X-STREAM-INF`, and the audio `URI="..."`
     attribute of `#EXT-X-MEDIA`. This is required because players resolve
     those URIs relative to the master URL and DROP the master's own query
     string: without propagation every child playlist request arrives with
     no token and is rejected with 403. Segment URIs never get a token
     (media playlists are personalised separately) and `#EXT-X-MAP` init
     URLs are never rewritten. Without a token the master is returned
     unchanged (the old 3-argument `personalizeHls` behaviour is kept).
   - **DASH**: ONE variant per session, chosen from `pattern(seed, 0)`,
     substituted into the `SegmentTemplate` paths. No `SegmentList` is ever
     generated at the edge (that would be thousands of entries).
5. Manifest responses: `Cache-Control: private, no-store` and the right
   Content-Type (`application/vnd.apple.mpegurl` for HLS,
   `application/dash+xml` for DASH).

No S3 credentials exist in this script: origin reads are AWS-signed by Bunny
itself (`AWSSigningEnabled`, see `../../infra/bunny.tf`). The token secret
and origin host are injected by the runtime, never baked into the file.

## Runtime configuration (Bunny environment variables/secrets)

The published script reads its configuration with `Deno.env.get` in
`src/middleware.ts`:

- `VG_EDGE_TOKEN_SECRET` (**Bunny secret**, required) — the HMAC secret shared
  with the Go token issuer. An empty value would make token verification
  accept ANY forged signature, so the script fails closed instead.
- `VG_EDGE_ORIGIN_HOST` (**Bunny environment variable**, required) — an
  absolute URL (`scheme://hostname`, no trailing slash needed) that serves the
  canonical manifests, and whose hostname is DISTINCT from the edge's own
  public hostname.
- `VG_EDGE_CORS_ORIGIN` (optional environment variable) — value of
  `Access-Control-Allow-Origin` on personalised manifests; empty/missing
  defaults to `"*"`.

If either required value is missing or empty, `handleClientRequest` answers
**500 `vg-edge: not configured`** to EVERY request before any other logic —
segments included. That is deliberate (fail closed): a half-configured edge
must never silently serve unverified or broken playback.

**CI** (`.github/workflows/aws-pipeline.yml`, job `edge`) is the SOLE owner of
the edge script. It reads the `EDGE_TOKEN_SECRET` secret and the
`EDGE_ORIGIN_HOST` repo variable, writes them through the documented Bunny
API (`PUT /compute/script/{id}/variables` and
`PUT /compute/script/{id}/secrets`), and fails the deploy step with a
`::error::` annotation when either is empty. Terraform deliberately does NOT
manage the edge script (`infra/bunny.tf` owns only the pull zone + Cloudflare
DNS).

- `VG_EDGE_TOKEN_SECRET` MUST equal the `PLAYBACK_EDGE_TOKEN_SECRET`
  environment variable set on Railway: the Go `IssueSessionEdgeToken` issuer
  signs with that same secret, so any mismatch makes every token fail
  verification (403).
- `VG_EDGE_ORIGIN_HOST` MUST be a hostname DISTINCT from the edge's own public
  hostname, pointing at the same pull zone (see "Deployment to Bunny" below
  for why).

This follows Bunny's published API/environment-variable documentation, but a
real Bunny API call and the live parameter injection remain a CONTRACT.md
section 11 DEFERRED GATE. Only Deno unit tests, the additive parse test in
`../../tests/infra/test_hcl_parse.py`, and the local `deno bundle` build are
done here.

## The A/B pattern (contract-exact)

```
digest  = HMAC_SHA256(seed.encode(), f"vg-ab-{n}".encode())
variant = "a" if digest[0] & 1 == 0 else "b"
```

GOLDEN VECTOR (verified with Python `hmac`/`hashlib.sha256`, asserted in
`tests/ab_pattern_test.ts` and hard-coded here on purpose):

- seed `"vg-test-seed-0001"`, n = 0..7 → `["b","b","a","a","a","a","a","a"]`
- first digest bytes (hex): n0 `1d4c3cffe1118ecc`, n1 `9f4e0305e14adec3`,
  n2 `e85502770d355712`, n3 `faffb0faf5efbb1c`

The HMAC/SHA-256 is a small synchronous implementation in
`src/ab_pattern.ts` (WebCrypto is async; per-segment substitution would chain
one await per segment). The test suite cross-checks it against WebCrypto, so
the two cannot drift apart.

## Files

| File | Purpose |
|---|---|
| `src/ab_pattern.ts` | SHA-256 + HMAC-SHA256 + `pattern()` / `patternForCount()` |
| `src/token.ts` | `verifyToken()` / `issueTokenForTests()` (issue exists only for tests; production tokens come from the Go backend) |
| `src/manifest.ts` | `personalizeHls()` (per-segment substitution + `vg_token` propagation into master child URIs) / `personalizeDash()` — pure functions |
| `src/origin.ts` | `fetchCanonicalManifest()` + the authenticated bypass header (`originFetchHeaderValue()` / `isAuthorisedOriginFetch()`) |
| `src/index.ts` | `handleClientRequest()` / `handleClientResponse()` middleware hooks + `handleRequest()` for the offline tests |
| `src/middleware.ts` | Bunny runtime entry: `BunnySDK.net.http.servePullZone(...).onClientRequest(...).onClientResponse(...)`; reads `Deno.env` |
| `deno.json` | `deno task test` / `deno task check` / `deno task bundle` |
| `tests/` | Offline unit tests (no network, no env vars) |
| `deploy/entry.js` | The BUILT plain-JS bundle for the Bunny runtime (generated, tracked) |

## Run the tests

```bash
deno test --allow-read edge/bunny/tests/   # from the repo root
# or
cd edge/bunny && deno task test
deno check edge/bunny/src/*.ts
```

Tests are deterministic: the fetcher and the clock are injectable, no
network, no environment variables.

## Build the bundle

Bunny's Edge Scripting runtime does not run TypeScript; it wants ONE plain
JavaScript entry file. Build the middleware entry (`src/middleware.ts`, which
imports the Bunny SDK) with Deno's bundler (Deno 2.9.7, verified):

```bash
# from this directory
deno task bundle

# equivalent, from the repository root
deno bundle --platform browser \
  -o edge/bunny/deploy/entry.js \
  edge/bunny/src/middleware.ts
```

The output `deploy/entry.js` is a **tracked build artifact**: CI
(`.github/workflows/aws-pipeline.yml`, job `edge`) rebuilds it with the same
command before deploying, so a source edit is always accompanied by a fresh
bundle. Terraform does NOT touch the bundle or the edge script any more
(`infra/bunny.tf` manages only the pull zone). After building, sanity-check
it: the file must be non-empty and contain the literals `__VG_VARIANT__`,
`vg_token` and `origin-fetch:`.

## Deployment to Bunny (deferred gate)

The published entry is `src/middleware.ts`, using the documented Bunny
middleware API (`BunnySDK.net.http.servePullZone(...).onClientRequest(...)`).
Bunny's runtime is a customized Deno, and its live behaviour (proxy semantics
of `fetch()`, where the bypass header lands, environment-variable injection)
cannot be verified without a real account (CONTRACT.md section 11). What is
deferred:

- **`VG_EDGE_TOKEN_SECRET` and `VG_EDGE_ORIGIN_HOST` must be set.** Without
  them the edge answers 500 to everything (fail closed, see above). CI writes
  them through `PUT /compute/script/{id}/secrets` and
  `PUT /compute/script/{id}/variables`; Terraform has no part in it. The real
  API call and runtime injection are still not verified locally.
- **The origin host MUST differ from the edge's own public hostname.** The
  edge fetches the canonical manifest from `VG_EDGE_ORIGIN_HOST`. If that host
  were the edge's own public hostname, the fetch would re-enter the edge
  script, and the only thing stopping a loop would be the
  `X-VG-Origin-Fetch` bypass header surviving Bunny's proxy round trip. That
  header behaviour is NOT verified here: if the runtime strips it, the
  re-entering request looks like a plain manifest request with NO `vg_token`
  and is rejected with 403 (or recurses). Configure `VG_EDGE_ORIGIN_HOST` as a
  DIFFERENT hostname that serves the canonical manifests, so the edge's own
  fetch never comes back to itself. Note also that the bypass is honoured only
  for requests whose hostname equals the origin host's hostname — a request
  arriving on the edge's public hostname is never a bypass, even with a
  correctly signed header.

- deploying via the documented Bunny compute API (CI job `edge`, using the
  `BUNNY_API_KEY` secret, enabled by the repo variable
  `EDGE_DEPLOY_ENABLED='true'`) — the BUILD step itself is not deferred (see
  above),
- real edge runtime behaviour: bypass-header round trip, before-cache
  execution, custom-hostname TLS, and the live `servePullZone` hooks.

CI creates/updates the script with a deterministic name
(`vars.EDGE_SCRIPT_NAME`, defaulting to `vg-edge-prod`; Terraform creates
nothing — `infra/bunny.tf` owns only the pull zone). It uploads the bundle via
`POST /compute/script/{id}/code`, publishes via
`POST /compute/script/{id}/publish`, and attaches it to the pull zone with
`MiddlewareScriptId` + `EdgeScriptExecutionPhase: 2` (run before cache). The
token secret travels only inside the one-shot API request — never into CI
logs, Terraform state or a developer's machine.

## Note on CONTRACT.md section 7 wording

CONTRACT.md section 7 now describes the strengthened design exactly as
implemented: the bypass value is an HMAC over `"origin-fetch:" + path` keyed
with the token secret. That is a deliberate strengthening of the original
draft (which described a plain `X-VG-Origin-Fetch: 1` header): a plain `1`
could be added by any viewer to skip the `vg_token` check entirely. The
header NAME and its purpose (loop break) are unchanged; CONTRACT.md is the
single source of truth and is amended by the orchestrator, not here.
