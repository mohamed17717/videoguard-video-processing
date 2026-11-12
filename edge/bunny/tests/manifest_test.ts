/**
 * Manifest personalisation tests (CONTRACT.md section 7).
 *
 * HLS: per-segment substitution with the GOLDEN pattern
 *      seed "vg-test-seed-0001", n=0..7 -> b,b,a,a,a,a,a,a.
 *      The #EXT-X-MAP init URL is untouched.
 *      Master playlists: the verified vg_token is propagated into the
 *      child playlist URIs (#EXT-X-STREAM-INF variant URI line and the
 *      #EXT-X-MEDIA URI attribute) - players drop the master's query
 *      string when resolving child URIs.
 * DASH: ONE variant per session from pattern(seed, 0); no SegmentList.
 */

import { personalizeDash, personalizeHls, VG_VARIANT_TOKEN } from "../src/manifest.ts";

const GOLDEN_SEED = "vg-test-seed-0001"; // pattern: n0=b, n1=b, n2=a, ...
const ALL_A_SEED = "vg-session-42"; // pattern: n0..n5 = a (documented vector)

const CANONICAL_MEDIA_PLAYLIST = [
  "#EXTM3U",
  "#EXT-X-VERSION:6",
  "#EXT-X-TARGETDURATION:2",
  '#EXT-X-MAP:URI="/job-1/init/720p/init.mp4"',
  "#EXTINF:2.00000,",
  "/job-1/__VG_VARIANT__/720p/seg_00001.m4s",
  "#EXTINF:2.00000,",
  "/job-1/__VG_VARIANT__/720p/seg_00002.m4s",
  "#EXTINF:2.00000,",
  "/job-1/__VG_VARIANT__/720p/seg_00003.m4s",
  "#EXT-X-ENDLIST",
  "",
].join("\n");

function assert(cond: boolean, message: string): void {
  if (!cond) {
    throw new Error(message);
  }
}

Deno.test("HLS: each segment URI gets the RIGHT variant from the golden pattern", async () => {
  const out = await personalizeHls(CANONICAL_MEDIA_PLAYLIST, "job-1", GOLDEN_SEED);

  // Segment index 0 -> b, 1 -> b, 2 -> a (golden vector).
  assert(
    out.includes("/job-1/b/720p/seg_00001.m4s"),
    `seg 1 must be variant b:\n${out}`,
  );
  assert(
    out.includes("/job-1/b/720p/seg_00002.m4s"),
    `seg 2 must be variant b:\n${out}`,
  );
  assert(
    out.includes("/job-1/a/720p/seg_00003.m4s"),
    `seg 3 must be variant a:\n${out}`,
  );
  assert(
    !out.includes("/job-1/a/720p/seg_00001.m4s") &&
      !out.includes("/job-1/a/720p/seg_00002.m4s") &&
      !out.includes("/job-1/b/720p/seg_00003.m4s"),
    "no segment may carry the wrong variant",
  );
});

Deno.test("HLS: token fully removed", async () => {
  const out = await personalizeHls(CANONICAL_MEDIA_PLAYLIST, "job-1", GOLDEN_SEED);
  assert(!out.includes(VG_VARIANT_TOKEN), "token must be gone");
});

Deno.test("HLS: init URL (#EXT-X-MAP) untouched", async () => {
  const out = await personalizeHls(CANONICAL_MEDIA_PLAYLIST, "job-1", GOLDEN_SEED);
  assert(
    out.includes('#EXT-X-MAP:URI="/job-1/init/720p/init.mp4"'),
    "init URL must be unchanged",
  );
  // And defensively: a MAP line that CARRIES the token is left untouched
  // (a canonical manifest never has one; the edge must not invent a rewrite).
  const defensive = [
    "#EXTM3U",
    '#EXT-X-MAP:URI="/job-1/init/720p/init.mp4"',
    "#EXTINF:2.00000,",
    "/job-1/__VG_VARIANT__/720p/seg_00001.m4s",
  ].join("\n");
  const defensiveOut = await personalizeHls(defensive, "job-1", GOLDEN_SEED);
  assert(
    defensiveOut.includes('#EXT-X-MAP:URI="/job-1/init/720p/init.mp4"'),
    "MAP line stays byte-identical",
  );
});

Deno.test("HLS: playlist without tokens (audio tree) is returned unchanged", async () => {
  const audio = [
    "#EXTM3U",
    "#EXT-X-VERSION:6",
    "#EXT-X-TARGETDURATION:2",
    "#EXTINF:2.00000,",
    "/job-1/audio/seg_00001.m4s",
    "#EXT-X-ENDLIST",
    "",
  ].join("\n");
  const out = await personalizeHls(audio, "job-1", GOLDEN_SEED);
  assert(out === audio, "no-token playlist must be byte-identical");
});

Deno.test("HLS: later segments continue the golden pattern (n=7 -> a)", async () => {
  // 9 segments: indexes 0..8 -> b,b,a,a,a,a,a,a,a (n=8 is past the golden
  // vector, but n<=7 is asserted exactly).
  const lines: string[] = ["#EXTM3U", "#EXT-X-VERSION:6"];
  for (let i = 1; i <= 9; i++) {
    lines.push("#EXTINF:2.00000,");
    lines.push(`/job-1/__VG_VARIANT__/720p/seg_${String(i).padStart(5, "0")}.m4s`);
  }
  const out = await personalizeHls(lines.join("\n"), "job-1", GOLDEN_SEED);
  const expectedVariant = (n: number) => (n < 8 ? (n < 2 ? "b" : "a") : "a");
  for (let n = 0; n < 9; n++) {
    const seg = `seg_${String(n + 1).padStart(5, "0")}.m4s`;
    const wrong = expectedVariant(n) === "a" ? "b" : "a";
    assert(
      out.includes(`/job-1/${expectedVariant(n)}/720p/${seg}`),
      `segment index ${n} must be ${expectedVariant(n)}`,
    );
    assert(
      !out.includes(`/job-1/${wrong}/720p/${seg}`),
      `segment index ${n} must NOT be ${wrong}`,
    );
  }
});

// ---------------------------------------------------------------------------
// MASTER playlists: token propagation into child playlist URIs.
// Shape mirrors the canonical master written by the worker (artifacts.py):
// the audio URI is an attribute ON the #EXT-X-MEDIA line, each variant URI
// is the plain line AFTER its #EXT-X-STREAM-INF line, LF endings.
// ---------------------------------------------------------------------------

const CANONICAL_MASTER_PLAYLIST = [
  "#EXTM3U",
  "#EXT-X-VERSION:7",
  '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="en",DEFAULT=YES,AUTOSELECT=YES,URI="/job-1/hls/audio.m3u8"',
  '#EXT-X-STREAM-INF:BANDWIDTH=3800000,AVERAGE-BANDWIDTH=2800000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio"',
  "/job-1/hls/media_720p.m3u8",
  '#EXT-X-STREAM-INF:BANDWIDTH=2200000,AVERAGE-BANDWIDTH=1400000,RESOLUTION=854x480,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio"',
  "/job-1/hls/media_480p.m3u8",
  "",
].join("\n");

Deno.test("HLS master: STREAM-INF variant URI and EXT-X-MEDIA audio URI both get vg_token", async () => {
  const out = await personalizeHls(
    CANONICAL_MASTER_PLAYLIST,
    "job-1",
    GOLDEN_SEED,
    "tok-1",
  );
  assert(
    out.includes("/job-1/hls/media_720p.m3u8?vg_token=tok-1"),
    `720p variant URI must carry the token:\n${out}`,
  );
  assert(
    out.includes("/job-1/hls/media_480p.m3u8?vg_token=tok-1"),
    `480p variant URI must carry the token:\n${out}`,
  );
  assert(
    out.includes('URI="/job-1/hls/audio.m3u8?vg_token=tok-1"'),
    `audio URI attribute must carry the token:\n${out}`,
  );
  // The tag lines themselves stay byte-identical.
  assert(
    out.includes(
      '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="en",DEFAULT=YES,AUTOSELECT=YES,',
    ),
    "EXT-X-MEDIA tag must be unchanged apart from the URI value",
  );
  assert(
    out.includes(
      '#EXT-X-STREAM-INF:BANDWIDTH=3800000,AVERAGE-BANDWIDTH=2800000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="audio"',
    ),
    "EXT-X-STREAM-INF tag must be unchanged",
  );
  assert(!out.includes(VG_VARIANT_TOKEN), "no variant token in a master");
});

Deno.test("HLS master: a child URI that already has a query gets &vg_token", async () => {
  const withQuery = [
    "#EXTM3U",
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="en",URI="/job-1/hls/audio.m3u8?already=1"',
    "#EXT-X-STREAM-INF:BANDWIDTH=3800000",
    "/job-1/hls/media_720p.m3u8?already=1",
    "",
  ].join("\n");
  const out = await personalizeHls(withQuery, "job-1", GOLDEN_SEED, "tok-1");
  assert(
    out.includes("/job-1/hls/media_720p.m3u8?already=1&vg_token=tok-1"),
    `existing query must be kept, token appended with &:\n${out}`,
  );
  assert(
    out.includes('URI="/job-1/hls/audio.m3u8?already=1&vg_token=tok-1"'),
    `audio URI attribute: existing query kept, & appended:\n${out}`,
  );
});

Deno.test("HLS master: the propagated token is URL-encoded", async () => {
  // Token with characters that MUST be escaped in a query value:
  // encodeURIComponent("a b/c?d=e&f") === "a%20b%2Fc%3Fd%3De%26f".
  const token = "a b/c?d=e&f";
  const out = await personalizeHls(
    CANONICAL_MASTER_PLAYLIST,
    "job-1",
    GOLDEN_SEED,
    token,
  );
  assert(
    out.includes(
      "/job-1/hls/media_720p.m3u8?vg_token=a%20b%2Fc%3Fd%3De%26f",
    ),
    `token must be URL-encoded:\n${out}`,
  );
  assert(
    out.includes('URI="/job-1/hls/audio.m3u8?vg_token=a%20b%2Fc%3Fd%3De%26f"'),
    `audio URI attribute must be URL-encoded too:\n${out}`,
  );
  assert(
    !out.includes("vg_token=a b") && !out.includes("vg_token=a%20b/c?"),
    "no raw, unencoded token may appear in any URI",
  );
});

Deno.test("HLS master: WITHOUT a token the master is returned unchanged (backwards compatible)", async () => {
  const out = await personalizeHls(CANONICAL_MASTER_PLAYLIST, "job-1", GOLDEN_SEED);
  assert(
    out === CANONICAL_MASTER_PLAYLIST,
    `3-argument call must be byte-identical:\n${out}`,
  );
});

Deno.test("HLS media: golden substitution still exact when a token IS supplied, init untouched", async () => {
  const out = await personalizeHls(
    CANONICAL_MEDIA_PLAYLIST,
    "job-1",
    GOLDEN_SEED,
    "tok-1",
  );
  // Golden pattern for vg-test-seed-0001: n0=b, n1=b, n2=a.
  assert(
    out.includes("/job-1/b/720p/seg_00001.m4s") &&
      out.includes("/job-1/b/720p/seg_00002.m4s") &&
      out.includes("/job-1/a/720p/seg_00003.m4s"),
    `golden sequence must be preserved:\n${out}`,
  );
  assert(
    out.includes('#EXT-X-MAP:URI="/job-1/init/720p/init.mp4"'),
    "init URL must be untouched",
  );
  assert(!out.includes(VG_VARIANT_TOKEN), "token must be gone");
});

Deno.test("HLS media: segment URIs are NOT given a token (personalised separately)", async () => {
  const out = await personalizeHls(
    CANONICAL_MEDIA_PLAYLIST,
    "job-1",
    GOLDEN_SEED,
    "tok-1",
  );
  assert(
    !out.includes("seg_00001.m4s?vg_token") &&
      !out.includes("seg_00002.m4s?vg_token") &&
      !out.includes("seg_00003.m4s?vg_token"),
    `segment URIs must not carry a token:\n${out}`,
  );
  assert(
    !out.includes("init.mp4?vg_token"),
    `the init URL must not carry a token:\n${out}`,
  );
  assert(
    !out.includes("vg_token="),
    `a media playlist has no child playlist URIs: no token anywhere:\n${out}`,
  );
});

const CANONICAL_MPD = [
  '<?xml version="1.0" encoding="utf-8"?>',
  '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">',
  "  <Period>",
  "    <AdaptationSet>",
  '      <Representation id="720p" codecs="avc1.64001f" width="1280" height="720">',
  '        <SegmentTemplate initialization="/job-1/init/720p/init.mp4" media="/job-1/__VG_VARIANT__/720p/seg_$Number%05d$.m4s" duration="2" startNumber="1"/>',
  "      </Representation>",
  "    </AdaptationSet>",
  "  </Period>",
  "</MPD>",
].join("\n");

Deno.test("DASH: ONE variant chosen from pattern(seed,0), init untouched, no SegmentList", async () => {
  // Golden seed -> pattern(seed,0) = "b".
  const out = await personalizeDash(CANONICAL_MPD, GOLDEN_SEED);
  assert(!out.includes(VG_VARIANT_TOKEN), "token must be gone");
  assert(
    out.includes('/job-1/b/720p/seg_$Number%05d$.m4s'),
    `media path must use variant b:\n${out}`,
  );
  assert(
    !out.includes("/job-1/a/720p/"),
    `the other variant must not appear:\n${out}`,
  );
  assert(
    out.includes('initialization="/job-1/init/720p/init.mp4"'),
    "initialization path must be untouched",
  );
  assert(!out.includes("<SegmentList"), "no SegmentList may be generated");
});

Deno.test("DASH: second seed picks the other variant", async () => {
  // vg-session-42 -> pattern(seed,0) = "a" (documented vector).
  const out = await personalizeDash(CANONICAL_MPD, ALL_A_SEED);
  assert(!out.includes(VG_VARIANT_TOKEN), "token must be gone");
  assert(
    out.includes('/job-1/a/720p/seg_$Number%05d$.m4s'),
    `media path must use variant a:\n${out}`,
  );
  assert(
    !out.includes("/job-1/b/720p/"),
    `the other variant must not appear:\n${out}`,
  );
  assert(!out.includes("<SegmentList"), "no SegmentList may be generated");
});

Deno.test("DASH: personalisation is pure (same input, same output)", async () => {
  const one = await personalizeDash(CANONICAL_MPD, GOLDEN_SEED);
  const two = await personalizeDash(CANONICAL_MPD, GOLDEN_SEED);
  assert(one === two, "repeated calls must return identical strings");
  assert(CANONICAL_MPD.includes(VG_VARIANT_TOKEN), "input must be unchanged");
});
