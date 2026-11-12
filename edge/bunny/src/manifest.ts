/**
 * Manifest personalisation (CONTRACT.md section 7).
 *
 * Canonical manifests contain the literal token __VG_VARIANT__ in each
 * segment URI, e.g.  /<job_id>/__VG_VARIANT__/720p/seg_00001.m4s
 * The canonical init URL has NO token: /<job_id>/init/720p/init.mp4
 *
 * HLS media playlists: per-segment substitution. Segment index = the count
 * of #EXTINF entries seen so far (first segment -> n=0). The #EXT-X-MAP
 * init URL is left untouched by construction (the canonical manifest has
 * no token there) AND defensively (a MAP line is never rewritten here).
 *
 * HLS master playlists: a master has no __VG_VARIANT__ and no segments, so
 * the substitution pass alone would serve it unchanged. Its child URIs -
 * the variant URI after each #EXT-X-STREAM-INF and the audio URI inside
 * the #EXT-X-MEDIA attribute - MUST carry vg_token themselves: players
 * resolve a child playlist relative to the master URL and DROP the
 * master's own query string, so without propagation every child playlist
 * request arrives without a token and is rejected (403). When a token is
 * supplied, personalizeHls appends it to those child URIs ONLY - never to
 * segment URIs (media playlists are personalised separately) and never to
 * #EXT-X-MAP init URLs.
 *
 * DASH: ONE variant per session, chosen from pattern(seed, 0), substituted
 * into the SegmentTemplate initialization/media paths. No SegmentList is
 * generated at the edge (1800 x 4 = 7200 entries - explicitly forbidden).
 *
 * All functions are pure: new string in, new string out, no I/O.
 */

import { pattern } from "./ab_pattern.ts";

/** The literal token the canonical manifests carry. */
export const VG_VARIANT_TOKEN = "__VG_VARIANT__";

/** Query-parameter name the edge verifies on every manifest request. */
const TOKEN_QUERY_PARAM = "vg_token";

/** The token as a query parameter, URL-encoded (safe for URIs/attributes). */
function tokenQuery(token: string): string {
  return `${TOKEN_QUERY_PARAM}=${encodeURIComponent(token)}`;
}

/**
 * Append the token to a plain URI LINE (the variant URI that follows
 * #EXT-X-STREAM-INF). Keeps an existing query string intact.
 */
function appendTokenToUriLine(uri: string, token: string): string {
  const joiner = uri.includes("?") ? "&" : "?";
  return `${uri}${joiner}${tokenQuery(token)}`;
}

/**
 * #EXT-X-MEDIA carries its child playlist URI as a quoted attribute ON the
 * tag line itself. Append the token inside the URI="..." value. A line
 * without a URI attribute is left byte-identical.
 */
function appendTokenToUriAttribute(line: string, token: string): string {
  const suffix = tokenQuery(token);
  return line.replace(/URI="([^"]*)"/, (_whole: string, uri: string): string => {
    const joiner = uri.includes("?") ? "&" : "?";
    return `URI="${uri}${joiner}${suffix}"`;
  });
}

/**
 * Personalise a canonical HLS playlist.
 *
 * With the 3-argument signature (no token) the behaviour is exactly the
 * per-segment __VG_VARIANT__ substitution for media playlists; a playlist
 * without the token (master, audio) is returned unchanged.
 *
 * With `token` supplied, the token is ALSO propagated into the child
 * playlist URIs of a MASTER playlist (after #EXT-X-STREAM-INF, and inside
 * the #EXT-X-MEDIA URI attribute) so a player's child requests still pass
 * the edge token check. Segment URIs never receive a token and #EXT-X-MAP
 * init URLs are never rewritten. `jobId` is carried for error messages
 * only (the canonical manifest is already job-scoped by its path).
 */
export async function personalizeHls(
  text: string,
  jobId: string,
  seed: string,
  token?: string,
): Promise<string> {
  void jobId;

  // Token to propagate into child playlist URIs, or undefined for the
  // backwards-compatible 3-argument behaviour.
  const propagateToken: string | undefined =
    typeof token === "string" && token.length > 0 ? token : undefined;

  if (text.includes(VG_VARIANT_TOKEN) === false && propagateToken === undefined) {
    // Nothing to personalise and nothing to propagate (e.g. the shared
    // audio playlist): return the text unchanged. Same bytes, no-store.
    return text;
  }

  let extinfCount = 0;
  /** True while the previous line was #EXT-X-STREAM-INF: the variant URI
   * is the non-comment line that follows it. */
  let afterStreamInf = false;
  const out: string[] = [];
  for (const line of text.split("\n")) {
    if (line.startsWith("#EXTINF")) {
      extinfCount += 1;
      out.push(line);
      afterStreamInf = false;
      continue;
    }
    if (line.includes(VG_VARIANT_TOKEN)) {
      if (line.startsWith("#EXT-X-MAP")) {
        // DEFENSIVE: init URLs are canonical and shared; never rewrite a
        // MAP line (a canonical manifest has no token there anyway).
        out.push(line);
        afterStreamInf = false;
        continue;
      }
      // The EXTINF immediately preceding this URI counts it, so the
      // zero-based index is (extinfCount - 1). Clamp guards a malformed
      // playlist where a URI line has no EXTINF before it.
      const n = Math.max(extinfCount - 1, 0);
      const variant = await pattern(seed, n);
      out.push(line.split(VG_VARIANT_TOKEN).join(variant));
      afterStreamInf = false;
      continue;
    }
    if (line.startsWith("#EXT-X-STREAM-INF")) {
      // The variant playlist URI is the NEXT line, not an attribute here.
      out.push(line);
      afterStreamInf = true;
      continue;
    }
    if (line.startsWith("#EXT-X-MEDIA")) {
      // The audio rendition URI is an attribute ON this line.
      out.push(
        propagateToken === undefined
          ? line
          : appendTokenToUriAttribute(line, propagateToken),
      );
      afterStreamInf = false;
      continue;
    }
    if (line.startsWith("#")) {
      out.push(line);
      afterStreamInf = false;
      continue;
    }
    // Non-comment line = a URI. Only the variant URIs that follow
    // #EXT-X-STREAM-INF get the token; segment URIs (after #EXTINF) do
    // not - media playlists are personalised per segment, and the player
    // already carries the token it received with the parent URI.
    if (afterStreamInf && propagateToken !== undefined) {
      out.push(appendTokenToUriLine(line, propagateToken));
    } else {
      out.push(line);
    }
    afterStreamInf = false;
  }
  return out.join("\n");
}

/**
 * Choose ONE variant per session (pattern(seed, 0)) and replace the token
 * in the MPD's SegmentTemplate paths. Pure text substitution: the
 * initialization path has no token in a canonical manifest, and no
 * SegmentList is ever generated here.
 */
export async function personalizeDash(
  text: string,
  seed: string,
): Promise<string> {
  const variant = await pattern(seed, 0);
  return text.split(VG_VARIANT_TOKEN).join(variant);
}
