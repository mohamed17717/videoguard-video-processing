// ../../../../../Library/Caches/deno/npm/registry.npmjs.org/@bunny.net/edgescript-sdk/0.13.0/esm/lib.mjs
var __defProp = Object.defineProperty;
var __export = (target, all) => {
  for (var name in all) __defProp(target, name, {
    get: all[name],
    enumerable: true
  });
};
var net_exports = {};
__export(net_exports, {
  http: () => serve_exports,
  ip: () => ip_exports,
  socketAddr: () => socket_addr_exports,
  tcp: () => tcp_exports
});
var tcp_exports = {};
__export(tcp_exports, {
  bind: () => bind,
  isTcpListener: () => isTcpListener,
  toString: () => toString2,
  unstable_local_addr: () => unstable_local_addr,
  unstable_new: () => unstable_new
});
var normalizePlatform = (platform) => {
  platform = platform.toLowerCase();
  if (platform.includes("ios")) return "iOS";
  if (platform === "android") return "Android";
  if (platform === "darwin") return "MacOS";
  if (platform === "win32") return "Windows";
  if (platform === "freebsd") return "FreeBSD";
  if (platform === "openbsd") return "OpenBSD";
  if (platform === "linux") return "Linux";
  if (platform) return `Other:${platform}`;
  return "Unknown";
};
var internal_getPlatform = () => {
  if (typeof Bunny !== "undefined") {
    return {
      _tag: "_Platform",
      version: "0.13.0",
      name: "Unknown",
      runtime: "bunny",
      runtime_version: "unknown"
    };
  }
  if (typeof Deno !== "undefined" && Deno.build != null) {
    return {
      _tag: "_Platform",
      version: "0.13.0",
      name: normalizePlatform(Deno.build.os),
      runtime: "deno",
      runtime_version: typeof Deno.version === "string" ? Deno.version : Deno.version?.deno ?? "unknown"
    };
  }
  const process = globalThis.process;
  if (Object.prototype.toString.call(typeof process !== "undefined" ? process : 0) === "[object process]") {
    return {
      _tag: "_Platform",
      version: "0.13.0",
      name: normalizePlatform(process.platform),
      runtime: "node",
      runtime_version: process.version
    };
  }
  return {
    _tag: "_Platform",
    version: "0.13.0",
    name: "Unknown",
    runtime: "unknown",
    runtime_version: "unknown"
  };
};
var socket_addr_exports = {};
__export(socket_addr_exports, {
  InvalidAddr: () => InvalidAddr,
  isV4: () => isV4,
  v4: () => v4_exports
});
var v4_exports = {};
__export(v4_exports, {
  ip: () => ip,
  port: () => port,
  tryFromString: () => tryFromString
});
var ip_exports = {};
__export(ip_exports, {
  toString: () => toString,
  tryParseFromString: () => tryParseFromString
});
function toString(ip2) {
  return `${ip2[0]}.${ip2[1]}.${ip2[2]}.${ip2[3]}`;
}
function tryParseFromString(ip2) {
  const parts = ip2.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => isNaN(part) || part < 0 || part > 255)) {
    return new SyntaxError("Invalid IP address");
  }
  return [
    parts[0],
    parts[1],
    parts[2],
    parts[3]
  ];
}
function port(addr) {
  return addr.port;
}
function ip(addr) {
  return addr.ip;
}
function tryFromString(value) {
  const parts = value.split(":");
  if (parts.length !== 2) {
    return new SyntaxError("Invalid SocketAddrV4 address");
  }
  const ip2 = tryParseFromString(parts[0]);
  if (ip2 instanceof SyntaxError) {
    return ip2;
  }
  const port2 = Number(parts[1]);
  if (isNaN(port2) || port2 < 0 || port2 > 65535) {
    return new SyntaxError("Invalid Port");
  }
  return {
    _tag: "SocketAddrV4",
    port: port2,
    ip: ip2
  };
}
function isV4(value) {
  return value._tag === "SocketAddrV4";
}
var addr_symbol = Symbol("invalidAddr");
var InvalidAddr = class extends Error {
  _guard = addr_symbol;
  constructor(message) {
    super(message);
    this.name = "InvalidAddr";
  }
  toString() {
    return `${this.name}: ${this.message}`;
  }
};
function isTcpListener(value) {
  return value instanceof Object && value["_tag"] === "TcpListener";
}
function unstable_local_addr(tcp) {
  return tcp.addr;
}
function toString2(tcp) {
  switch (tcp.addr._tag) {
    case "SocketAddrV4":
      return `${v4_exports.ip(tcp.addr).join(".")}:${v4_exports.port(tcp.addr)}`;
    case "NoAddr":
      return "No addr associated to this listener. You should be running in a controlled environment.";
  }
}
function unstable_new() {
  const platform = internal_getPlatform();
  switch (platform.runtime) {
    case "bunny": {
      return {
        _tag: "TcpListener",
        addr: {
          _tag: "NoAddr"
        }
      };
    }
    default:
      return {
        _tag: "TcpListener",
        addr: {
          _tag: "SocketAddrV4",
          port: 8080,
          ip: [
            127,
            0,
            0,
            1
          ]
        }
      };
  }
}
function bind(addr) {
  return {
    _tag: "TcpListener",
    addr
  };
}
var serve_exports = {};
__export(serve_exports, {
  internal_buildPullZoneHandler: () => internal_buildPullZoneHandler,
  serve: () => serve,
  servePullZone: () => servePullZone
});
var handler = {
  get: () => void 0
};
var proxy = new Proxy({}, handler);
function isResponse(value) {
  return value instanceof Response;
}
function isRequest(value) {
  return value instanceof Request;
}
function internal_buildPullZoneHandler(originUrl, middlewares, runtime = "node") {
  const rebuildForNode = async (source, headers) => {
    const init = {
      status: source.status,
      statusText: source.statusText,
      headers
    };
    if (runtime === "node" && headers.get("content-type") === "text/html" && source.body !== null) {
      const body = await source.text();
      headers.delete("content-encoding");
      return new Response(body, init);
    }
    return new Response(source.body, init);
  };
  return async (req) => {
    const url = new URL(req.url);
    const origin_url = new URL(originUrl);
    url.protocol = origin_url.protocol;
    url.hostname = origin_url.hostname;
    url.port = origin_url.port;
    let mutableRequest = new Request(url, req);
    for (const mid of middlewares.onClientRequest) {
      const reqOrResponse = await mid({
        request: mutableRequest
      });
      if (isResponse(reqOrResponse)) {
        return reqOrResponse;
      }
      if (isRequest(reqOrResponse)) {
        mutableRequest = reqOrResponse;
      }
    }
    let originResponse;
    let originRequestShortCircuited = false;
    for (const mid of middlewares.onOriginRequest) {
      const reqOrResponse = await mid({
        request: mutableRequest
      });
      if (isResponse(reqOrResponse)) {
        originResponse = reqOrResponse;
        originRequestShortCircuited = true;
        break;
      }
      if (isRequest(reqOrResponse)) {
        mutableRequest = reqOrResponse;
      }
    }
    if (originResponse === void 0) {
      originResponse = await fetch(mutableRequest);
    }
    const headers = new Headers();
    for (const [key, value] of originResponse.headers.entries()) {
      headers.set(key, value);
    }
    let newResponse = await rebuildForNode(originResponse, headers);
    if (!originRequestShortCircuited) {
      for (const mid of middlewares.onOriginResponse) {
        const reqOrResponse = await mid({
          request: mutableRequest,
          response: newResponse
        });
        if (isResponse(reqOrResponse)) {
          newResponse = reqOrResponse;
        }
      }
    }
    for (const mid of middlewares.onClientResponse) {
      const reqOrResponse = await mid({
        request: mutableRequest,
        response: newResponse
      });
      if (isResponse(reqOrResponse)) {
        newResponse = reqOrResponse;
      }
    }
    return newResponse;
  };
}
function is_port_and_hostname(value) {
  if (typeof value === "object" && value !== null) {
    const port2 = value["port"];
    return port2 !== void 0 && typeof port2 === "number" && value["hostname"] !== void 0;
  }
  return false;
}
function serve(listener, handler2) {
  let raw_handler;
  let raw_listener;
  if (is_port_and_hostname(listener)) {
    const addr = v4_exports.tryFromString(`${listener.hostname}:${listener.port}`);
    if (addr instanceof Error) {
      throw addr;
    }
    raw_listener = bind(addr);
    raw_handler = handler2;
  } else if (isTcpListener(listener)) {
    raw_handler = handler2;
    raw_listener = listener;
  } else {
    raw_handler = listener;
    raw_listener = unstable_new();
  }
  if (raw_handler === void 0) {
    throw new Error("An issue happened.");
  }
  const platform = internal_getPlatform();
  switch (platform.runtime) {
    case "bunny": {
      Bunny.v1.serve(raw_handler);
      return {};
    }
    case "node": {
      return (void 0)(raw_listener, raw_handler);
    }
    case "deno": {
      const addr = unstable_local_addr(raw_listener);
      if (!isV4(addr)) {
        throw new Error("An issue happened with the addr.");
      }
      const port2 = v4_exports.port(addr);
      const hostname = toString(v4_exports.ip(addr));
      Deno.serve({
        port: port2,
        hostname
      }, raw_handler);
      return {};
    }
    case "unknown": {
      return {};
    }
  }
}
function servePullZone(listener, options) {
  let raw_listener;
  let raw_options = {
    url: "https://bunny.net"
  };
  if (options) {
    raw_options = options;
  }
  if (is_port_and_hostname(listener)) {
    const addr = v4_exports.tryFromString(`${listener.hostname}:${listener.port}`);
    if (addr instanceof Error) {
      throw addr;
    }
    raw_listener = bind(addr);
  } else if (isTcpListener(listener)) {
    raw_listener = listener;
  } else {
    if (listener) {
      raw_options = listener;
    }
    raw_listener = unstable_new();
  }
  const onOriginRequestMiddleware = [];
  const onOriginResponseMiddleware = [];
  const onClientRequestMiddleware = [];
  const onClientResponseMiddleware = [];
  const platform = internal_getPlatform();
  switch (platform.runtime) {
    case "bunny": {
      Bunny.v1.registerMiddlewares({
        onOriginRequest: onOriginRequestMiddleware,
        onOriginResponse: onOriginResponseMiddleware,
        onClientRequest: onClientRequestMiddleware,
        onClientResponse: onClientResponseMiddleware
      });
      break;
    }
    default: {
      const middlewareHandler = internal_buildPullZoneHandler(raw_options.url, {
        onClientRequest: onClientRequestMiddleware,
        onOriginRequest: onOriginRequestMiddleware,
        onOriginResponse: onOriginResponseMiddleware,
        onClientResponse: onClientResponseMiddleware
      }, platform.runtime === "node" ? "node" : "deno");
      serve(raw_listener, middlewareHandler);
    }
  }
  const pullzoneHandler = {};
  pullzoneHandler.onOriginResponse = (middleware) => {
    onOriginResponseMiddleware.push(middleware);
    return pullzoneHandler;
  };
  pullzoneHandler.onOriginRequest = (middleware) => {
    onOriginRequestMiddleware.push(middleware);
    return pullzoneHandler;
  };
  pullzoneHandler.onClientResponse = (middleware) => {
    onClientResponseMiddleware.push(middleware);
    return pullzoneHandler;
  };
  pullzoneHandler.onClientRequest = (middleware) => {
    onClientRequestMiddleware.push(middleware);
    return pullzoneHandler;
  };
  return pullzoneHandler;
}

// src/ab_pattern.ts
var SHA_K = new Uint32Array([
  1116352408,
  1899447441,
  3049323471,
  3921009573,
  961987163,
  1508970993,
  2453635748,
  2870763221,
  3624381080,
  310598401,
  607225278,
  1426881987,
  1925078388,
  2162078206,
  2614888103,
  3248222580,
  3835390401,
  4022224774,
  264347078,
  604807628,
  770255983,
  1249150122,
  1555081692,
  1996064986,
  2554220882,
  2821834349,
  2952996808,
  3210313671,
  3336571891,
  3584528711,
  113926993,
  338241895,
  666307205,
  773529912,
  1294757372,
  1396182291,
  1695183700,
  1986661051,
  2177026350,
  2456956037,
  2730485921,
  2820302411,
  3259730800,
  3345764771,
  3516065817,
  3600352804,
  4094571909,
  275423344,
  430227734,
  506948616,
  659060556,
  883997877,
  958139571,
  1322822218,
  1537002063,
  1747873779,
  1955562222,
  2024104815,
  2227730452,
  2361852424,
  2428436474,
  2756734187,
  3204031479,
  3329325298
]);
function rotr(x, n) {
  return x >>> n | x << 32 - n;
}
function sha256(data) {
  const len = data.length;
  const totalLen = len + 9 + 63 & ~63;
  const padded = new Uint8Array(totalLen);
  padded.set(data);
  padded[len] = 128;
  const bitLen = len * 8;
  const dv = new DataView(padded.buffer);
  dv.setUint32(totalLen - 8, Math.floor(bitLen / 4294967296), false);
  dv.setUint32(totalLen - 4, bitLen % 4294967296, false);
  let h0 = 1779033703, h1 = 3144134277, h2 = 1013904242, h3 = 2773480762;
  let h4 = 1359893119, h5 = 2600822924, h6 = 528734635, h7 = 1541459225;
  const w = new Uint32Array(64);
  for (let off = 0; off < totalLen; off += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = dv.getUint32(off + i * 4, false);
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ w[i - 15] >>> 3;
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ w[i - 2] >>> 10;
      w[i] = w[i - 16] + s0 + w[i - 7] + s1 >>> 0;
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = e & f ^ ~e & g;
      const t1 = h + S1 + ch + SHA_K[i] + w[i] >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = a & b ^ a & c ^ b & c;
      const t2 = S0 + maj >>> 0;
      h = g;
      g = f;
      f = e;
      e = d + t1 >>> 0;
      d = c;
      c = b;
      b = a;
      a = t1 + t2 >>> 0;
    }
    h0 = h0 + a >>> 0;
    h1 = h1 + b >>> 0;
    h2 = h2 + c >>> 0;
    h3 = h3 + d >>> 0;
    h4 = h4 + e >>> 0;
    h5 = h5 + f >>> 0;
    h6 = h6 + g >>> 0;
    h7 = h7 + h >>> 0;
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
function hmacSha256(key, message) {
  const BLOCK = 64;
  const k = key.length > BLOCK ? sha256(key) : key;
  const ipadKey = new Uint8Array(BLOCK);
  const opadKey = new Uint8Array(BLOCK);
  ipadKey.set(k);
  opadKey.set(k);
  for (let i = 0; i < BLOCK; i++) {
    ipadKey[i] ^= 54;
    opadKey[i] ^= 92;
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
function utf8(text) {
  return new TextEncoder().encode(text);
}
async function pattern(seed, n) {
  const digest = hmacSha256(utf8(seed), utf8(`vg-ab-${n}`));
  return (digest[0] & 1) === 0 ? "a" : "b";
}

// src/token.ts
var TokenError = class extends Error {
};
var REQUIRED_STRING_CLAIMS = [
  "job_id",
  "video_public_id",
  "session_id",
  "seed"
];
function bytesToBase64Url(bytes) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function base64UrlToBytes(text) {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - b64.length % 4) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}
function utf82(text) {
  return new TextEncoder().encode(text);
}
function safeEqual(a, b) {
  if (a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}
function signPayload(payloadPart, secret) {
  const sig = hmacSha256(utf82(secret), utf82(payloadPart));
  return bytesToBase64Url(sig);
}
async function verifyToken(token, secret, nowSeconds = Math.floor(Date.now() / 1e3)) {
  const parts = token.split(".");
  if (parts.length !== 2 || parts[0].length === 0 || parts[1].length === 0) {
    throw new TokenError("malformed token");
  }
  const [payloadPart, sigPart] = parts;
  if (!safeEqual(signPayload(payloadPart, secret), sigPart)) {
    throw new TokenError("bad signature");
  }
  let parsed;
  try {
    parsed = JSON.parse(new TextDecoder().decode(base64UrlToBytes(payloadPart)));
  } catch {
    throw new TokenError("payload is not JSON");
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new TokenError("payload is not a JSON object");
  }
  const raw = parsed;
  for (const claim of REQUIRED_STRING_CLAIMS) {
    const value = raw[claim];
    if (typeof value !== "string" || value.length === 0) {
      throw new TokenError(`missing claim ${claim}`);
    }
  }
  const exp = raw["exp"];
  if (typeof exp !== "number" || !Number.isFinite(exp)) {
    throw new TokenError("missing claim exp");
  }
  if (exp <= nowSeconds) {
    throw new TokenError("expired");
  }
  return {
    job_id: raw["job_id"],
    video_public_id: raw["video_public_id"],
    session_id: raw["session_id"],
    seed: raw["seed"],
    exp
  };
}

// src/origin.ts
var ORIGIN_FETCH_HEADER = "X-VG-Origin-Fetch";
var ORIGIN_FETCH_PREFIX = "origin-fetch:";
function utf83(text) {
  return new TextEncoder().encode(text);
}
function bytesToHex(bytes) {
  let hex = "";
  for (const byte of bytes) {
    hex += byte.toString(16).padStart(2, "0");
  }
  return hex;
}
function originFetchHeaderValue(tokenSecret, path) {
  return bytesToHex(hmacSha256(utf83(tokenSecret), utf83(ORIGIN_FETCH_PREFIX + path)));
}
function isAuthorisedOriginFetch(req, tokenSecret, originHost) {
  const received = req.headers.get(ORIGIN_FETCH_HEADER);
  if (received === null) {
    return false;
  }
  let requestUrl;
  let originUrl;
  try {
    requestUrl = new URL(req.url);
    originUrl = new URL(originHost);
  } catch {
    return false;
  }
  const expected = originFetchHeaderValue(tokenSecret, requestUrl.pathname);
  if (!safeEqual(expected, received)) {
    return false;
  }
  return requestUrl.hostname === originUrl.hostname;
}
async function fetchCanonicalManifest(originHost, path, tokenSecret, fetcher) {
  const host = originHost.endsWith("/") ? originHost.slice(0, -1) : originHost;
  const url = `${host}${path.startsWith("/") ? path : `/${path}`}`;
  const res = await fetcher.fetch(url, {
    headers: {
      [ORIGIN_FETCH_HEADER]: originFetchHeaderValue(tokenSecret, path)
    }
  });
  if (!res.ok) {
    throw new Error(`canonical manifest fetch failed (${res.status}) for ${path}`);
  }
  return await res.text();
}

// src/manifest.ts
var VG_VARIANT_TOKEN = "__VG_VARIANT__";
var TOKEN_QUERY_PARAM = "vg_token";
function tokenQuery(token) {
  return `${TOKEN_QUERY_PARAM}=${encodeURIComponent(token)}`;
}
function appendTokenToUriLine(uri, token) {
  const joiner = uri.includes("?") ? "&" : "?";
  return `${uri}${joiner}${tokenQuery(token)}`;
}
function appendTokenToUriAttribute(line, token) {
  const suffix = tokenQuery(token);
  return line.replace(/URI="([^"]*)"/, (_whole, uri) => {
    const joiner = uri.includes("?") ? "&" : "?";
    return `URI="${uri}${joiner}${suffix}"`;
  });
}
async function personalizeHls(text, jobId, seed, token) {
  void jobId;
  const propagateToken = typeof token === "string" && token.length > 0 ? token : void 0;
  if (text.includes(VG_VARIANT_TOKEN) === false && propagateToken === void 0) {
    return text;
  }
  let extinfCount = 0;
  let afterStreamInf = false;
  const out = [];
  for (const line of text.split("\n")) {
    if (line.startsWith("#EXTINF")) {
      extinfCount += 1;
      out.push(line);
      afterStreamInf = false;
      continue;
    }
    if (line.includes(VG_VARIANT_TOKEN)) {
      if (line.startsWith("#EXT-X-MAP")) {
        out.push(line);
        afterStreamInf = false;
        continue;
      }
      const n = Math.max(extinfCount - 1, 0);
      const variant = await pattern(seed, n);
      out.push(line.split(VG_VARIANT_TOKEN).join(variant));
      afterStreamInf = false;
      continue;
    }
    if (line.startsWith("#EXT-X-STREAM-INF")) {
      out.push(line);
      afterStreamInf = true;
      continue;
    }
    if (line.startsWith("#EXT-X-MEDIA")) {
      out.push(propagateToken === void 0 ? line : appendTokenToUriAttribute(line, propagateToken));
      afterStreamInf = false;
      continue;
    }
    if (line.startsWith("#")) {
      out.push(line);
      afterStreamInf = false;
      continue;
    }
    if (afterStreamInf && propagateToken !== void 0) {
      out.push(appendTokenToUriLine(line, propagateToken));
    } else {
      out.push(line);
    }
    afterStreamInf = false;
  }
  return out.join("\n");
}
async function personalizeDash(text, seed) {
  const variant = await pattern(seed, 0);
  return text.split(VG_VARIANT_TOKEN).join(variant);
}

// src/index.ts
var SEGMENT_CACHE_CONTROL = "public, max-age=31536000, immutable";
var MANIFEST_CACHE_CONTROL = "private, no-store";
function isManifestPath(path) {
  return path.endsWith(".m3u8") || path.endsWith(".mpd");
}
var CORS_ALLOW_METHODS = "GET, HEAD, OPTIONS";
var CORS_ALLOW_HEADERS = "Range, If-None-Match, If-Modified-Since";
var CORS_EXPOSE_HEADERS = "Content-Length, Content-Range, ETag";
function corsHeaders(allowOrigin) {
  const headers = new Headers();
  headers.set("Access-Control-Allow-Origin", allowOrigin);
  headers.set("Access-Control-Allow-Methods", CORS_ALLOW_METHODS);
  headers.set("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS);
  headers.set("Access-Control-Expose-Headers", CORS_EXPOSE_HEADERS);
  headers.set("Vary", "Origin");
  return headers;
}
function manifestHeaders(contentType, allowOrigin) {
  const headers = corsHeaders(allowOrigin);
  headers.set("Content-Type", contentType);
  headers.set("Cache-Control", MANIFEST_CACHE_CONTROL);
  return headers;
}
function textResponse(status, message) {
  return new Response(`${message}
`, {
    status,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": MANIFEST_CACHE_CONTROL
    }
  });
}
async function handleClientRequest(req, deps2) {
  if (!deps2.tokenSecret || !deps2.originHost) {
    return textResponse(500, "vg-edge: not configured");
  }
  const allowOrigin = deps2.corsOrigin && deps2.corsOrigin.length > 0 ? deps2.corsOrigin : "*";
  const url = new URL(req.url);
  const path = url.pathname;
  if (req.headers.get(ORIGIN_FETCH_HEADER) !== null) {
    if (!isAuthorisedOriginFetch(req, deps2.tokenSecret, deps2.originHost)) {
      return textResponse(403, "vg-edge: invalid origin-fetch header");
    }
    return req;
  }
  if (!isManifestPath(path)) {
    return req;
  }
  const token = url.searchParams.get("vg_token");
  if (token === null || token.length === 0) {
    return textResponse(403, "vg-edge: missing vg_token");
  }
  const now = deps2.now ?? (() => Math.floor(Date.now() / 1e3));
  let claims;
  try {
    claims = await verifyToken(token, deps2.tokenSecret, now());
  } catch (err) {
    const reason = err instanceof Error ? err.message : "invalid vg_token";
    return textResponse(403, `vg-edge: ${reason}`);
  }
  if (!path.startsWith(`/${claims.job_id}/`)) {
    return textResponse(403, "vg-edge: token does not cover this job");
  }
  const canonical = await fetchCanonicalManifest(deps2.originHost, path, deps2.tokenSecret, deps2.fetcher);
  if (path.endsWith(".m3u8")) {
    const body2 = await personalizeHls(canonical, claims.job_id, claims.seed, token);
    return new Response(body2, {
      status: 200,
      headers: manifestHeaders("application/vnd.apple.mpegurl", allowOrigin)
    });
  }
  const body = await personalizeDash(canonical, claims.seed);
  return new Response(body, {
    status: 200,
    headers: manifestHeaders("application/dash+xml", allowOrigin)
  });
}
function handleClientResponse(req, res) {
  const path = new URL(req.url).pathname;
  if (isManifestPath(path) || res.status !== 200 && res.status !== 206) {
    return res;
  }
  const headers = new Headers(res.headers);
  headers.set("Cache-Control", SEGMENT_CACHE_CONTROL);
  return new Response(res.body, {
    status: res.status,
    statusText: res.statusText,
    headers
  });
}

// src/middleware.ts
function env(name) {
  return Deno.env.get(name) ?? "";
}
var deps = {
  originHost: env("VG_EDGE_ORIGIN_HOST"),
  tokenSecret: env("VG_EDGE_TOKEN_SECRET"),
  corsOrigin: env("VG_EDGE_CORS_ORIGIN"),
  fetcher: {
    fetch: (input, init) => globalThis.fetch(input, init)
  }
};
net_exports.http.servePullZone({
  url: deps.originHost || "https://example.invalid"
}).onClientRequest((ctx) => handleClientRequest(ctx.request, deps)).onClientResponse(async (ctx) => handleClientResponse(ctx.request, ctx.response));
