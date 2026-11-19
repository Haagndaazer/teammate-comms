import { DurableObject } from "cloudflare:workers";
import { ConfigError, Mailbox, identify, parseConfig } from "./mailbox.js";

const MAX_MCP_BYTES = 1024 * 1024;
const MAX_SYNC_BYTES = 8 * 1024 * 1024;

let cached = { raw: null, config: null, error: "" };

function loadConfig(env) {
  const raw = env.MAILBOX_CONFIG;
  if (cached.raw !== raw) {
    cached = { raw, config: null, error: "" };
    try {
      if (!raw) throw new ConfigError("MAILBOX_CONFIG is not set");
      cached.config = parseConfig(raw);
    } catch (e) {
      cached.error = e instanceof ConfigError ? e.message : "MAILBOX_CONFIG could not be read";
      console.error(`mailbox config rejected: ${cached.error}`);
    }
  }
  return cached.config;
}

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...headers },
  });
}

function bearer(request) {
  const header = request.headers.get("authorization") || "";
  return header.toLowerCase().startsWith("bearer ") ? header.slice(7).trim() : "";
}

async function readJson(request, maxBytes) {
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > maxBytes) return { status: 413 };
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > maxBytes) return { status: 413 };
  try {
    return { value: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) };
  } catch {
    return { status: 400 };
  }
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    const isSync = path === "/pc/sync";
    if (!isSync && path !== "/mcp") return json({ error: "not_found" }, 404);
    const config = loadConfig(env);
    if (!config) return json({ error: "not_configured" }, 503);
    const who = await identify(config, isSync ? "machines" : "bots", bearer(request));
    if (!who) return json({ error: "unauthorized" }, 401, { "www-authenticate": "Bearer" });
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { allow: "POST" } });
    const body = await readJson(request, isSync ? MAX_SYNC_BYTES : MAX_MCP_BYTES);
    if (body.status) return json({ error: body.status === 413 ? "too_large" : "bad_json" }, body.status);
    const box = env.MAILBOX.get(env.MAILBOX.idFromName("mailbox"));
    if (isSync) {
      const out = await box.sync(who, body.value);
      return json(out.body, out.status);
    }
    const reply = await box.rpc(who, body.value);
    return reply === null ? new Response(null, { status: 202 }) : json(reply);
  },
};

export class MailboxObject extends DurableObject {
  box() {
    const config = loadConfig(this.env);
    if (!config) return null;
    return new Mailbox({
      storage: this.ctx.storage,
      config,
      fetch: (url, init) => fetch(url, init),
      setAlarm: (ms) => this.ctx.storage.setAlarm(ms),
      doorbellMinSeconds: Number(this.env.DOORBELL_MIN_SECONDS),
      doorbellMaxPerDay: Number(this.env.DOORBELL_MAX_PER_DAY),
    });
  }

  async sync(machine, body) {
    const box = this.box();
    return box ? box.sync(machine, body) : { status: 503, body: { error: "not_configured" } };
  }

  async rpc(bot, body) {
    const box = this.box();
    return box ? box.rpc(bot, body) : null;
  }

  async alarm() {
    const box = this.box();
    if (box) await box.alarm();
  }
}
