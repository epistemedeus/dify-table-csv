// Disposable loopback boundary in front of the pinned free reader.
// It forwards no caller headers and installs no beforeFetch hook.
import { createServer } from "node:http";
import { pathToFileURL } from "node:url";
import path from "node:path";

const bindHost = process.env.READER_BIND || "127.0.0.1";
const admitted = process.env.DIFY_TABLE_ADMIT === "1" || process.env.H64314_ADMIT_DIFY === "1";
if (bindHost !== "127.0.0.1" && !admitted) {
  console.error("refusing non-loopback bind without DIFY_TABLE_ADMIT=1 or H64314_ADMIT_DIFY=1");
  process.exit(2);
}

const readerCore = process.env.READER_CORE;
if (!readerCore) {
  console.error("READER_CORE is required");
  process.exit(2);
}

const { readMarkdown } = await import(pathToFileURL(path.join(readerCore, "extract.mjs")).href);

const REFUSAL = new Set([
  "invalid_url",
  "invalid_protocol",
  "ssrf_blocked",
  "invalid_markdown_limit",
  "invalid_excerpt_limit",
  "redirect_error",
  "unsupported_encoding",
  "invalid_response",
]);
const RETRYABLE = new Set([
  "ENOTFOUND",
  "ECONNRESET",
  "ECONNREFUSED",
  "ETIMEDOUT",
  "EAI_AGAIN",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_SOCKET",
]);

const port = Number(process.env.PORT || 8765);
let requestCount = 0;

function send(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function asLimit(value) {
  if (typeof value === "number" && Number.isFinite(value) && Math.trunc(value) === value) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && /^-?\d+$/.test(value)) return Number(value);
  return value;
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://127.0.0.1");
  if (req.method === "GET" && url.pathname === "/health") {
    send(res, 200, { ok: true, reader: "readMarkdown", requestCount });
    return;
  }
  if (req.method !== "POST" || url.pathname !== "/v1/read") {
    send(res, 404, { ok: false, error: { code: "not_found" } });
    return;
  }
  const chunks = [];
  let size = 0;
  try {
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 16384) {
        send(res, 400, { ok: false, error: { code: "body_too_large" } });
        return;
      }
      chunks.push(chunk);
    }
  } catch (err) {
    console.error(JSON.stringify({ event: "body_error", message: String(err && err.message) }));
    return;
  }
  let payload;
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    send(res, 400, { ok: false, error: { code: "malformed_json" } });
    return;
  }
  requestCount += 1;
  console.error(JSON.stringify({ event: "read", n: requestCount, bytes: size }));
  const limit = asLimit(payload.markdownLimitChars);
  try {
    const record = await readMarkdown(payload.url, limit);
    send(res, 200, record);
  } catch (err) {
    const code = err && err.code;
    const name = err && err.name;
    const message = String((err && err.message) || "reader error");
    if (REFUSAL.has(code)) {
      send(res, 422, { ok: false, error: { code, message } });
      return;
    }
    const retryable = RETRYABLE.has(code) || name === "AbortError" || err instanceof TypeError;
    send(res, 502, {
      ok: false,
      error: { code: code || name || "transport", message, retryable },
    });
  }
});

server.requestTimeout = 20000;
server.headersTimeout = 5000;
server.listen(port, bindHost, () => {
  const addr = server.address();
  console.log("PORT " + addr.port);
});
