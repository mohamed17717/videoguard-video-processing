/**
 * Bunny Edge Scripting runtime entry point (CONTRACT.md section 7).
 *
 * This is the file that gets bundled and published. It uses the documented
 * Bunny middleware API:
 *
 *   BunnySDK.net.http
 *     .servePullZone({ url })
 *     .onClientRequest(...)
 *     .onClientResponse(...)
 *
 * The pull zone must run the script BEFORE cache
 * (`EdgeScriptExecutionPhase: 2`) or `onClientRequest` never runs.
 *
 * Runtime configuration comes from Bunny environment variables/secrets, read
 * with `Deno.env.get`:
 *
 *   VG_EDGE_TOKEN_SECRET   (secret)  HMAC secret shared with the Go issuer
 *   VG_EDGE_ORIGIN_HOST    (variable) absolute URL of the canonical origin
 *   VG_EDGE_CORS_ORIGIN    (optional variable) manifest ACAO, default "*"
 *
 * The pure request logic lives in `index.ts` so the offline Deno tests never
 * import the Bunny SDK (which starts a local HTTP listener on import).
 */

import * as BunnySDK from "npm:@bunny.net/edgescript-sdk@0.13.0";
import {
  type EdgeDeps,
  handleClientRequest,
  handleClientResponse,
} from "./index.ts";

function env(name: string): string {
  return Deno.env.get(name) ?? "";
}

const deps: EdgeDeps = {
  originHost: env("VG_EDGE_ORIGIN_HOST"),
  tokenSecret: env("VG_EDGE_TOKEN_SECRET"),
  corsOrigin: env("VG_EDGE_CORS_ORIGIN"),
  fetcher: {
    fetch: (input: string | URL, init?: RequestInit) =>
      globalThis.fetch(input, init),
  },
};

// `url` is only used by the SDK for local development; in production Bunny
// proxies to the Pull Zone's configured origin. The fallback keeps the module
// loadable when unconfigured - handleClientRequest then fails closed with 500.
BunnySDK.net.http
  .servePullZone({ url: deps.originHost || "https://example.invalid" })
  // The SDK types model the two outcomes as separate Promise types
  // (`Promise<Request> | Promise<Response>`), while our hook returns one
  // Promise of either value. Both are the same at runtime; the cast only
  // satisfies the SDK's declaration.
  .onClientRequest((ctx) =>
    handleClientRequest(ctx.request, deps) as unknown as
      | Promise<Request>
      | Promise<Response>
  )
  .onClientResponse(async (ctx) =>
    handleClientResponse(ctx.request, ctx.response)
  );
