import { DurableObject } from "cloudflare:workers";
import { ConfigError, Mailbox, identify, parseConfig } from "./mailbox.js";
import PostalMime from "postal-mime";
import { splitRecipient } from "./email.js";
import { MAIL } from "./emailbox.js";

const MAX_MCP_BYTES = 1024 * 1024;
const MAX_SYNC_BYTES = 8 * 1024 * 1024;

let cached = { raw: null, config: null, error: "" };

const MAX_EMAIL_PARTS = 8;

function loadConfig(env) {
  let emailRaw = "";
  const parts = Math.min(MAX_EMAIL_PARTS, Number.parseInt(env.MAILBOX_EMAIL_PARTS, 10) || 0);
  for (let n = 1; n <= parts; n++) emailRaw += env[`MAILBOX_EMAIL_${n}`] || "";
  const raw = `${env.MAILBOX_CONFIG || ""}\u0000${emailRaw}`;
  if (cached.raw !== raw) {
    cached = { raw, config: null, error: "" };
    try {
      if (!env.MAILBOX_CONFIG) throw new ConfigError("MAILBOX_CONFIG is not set");
      let data;
      try {
        data = JSON.parse(env.MAILBOX_CONFIG);
        if (emailRaw) data.email = JSON.parse(emailRaw);
      } catch {
        throw new ConfigError("MAILBOX_CONFIG or MAILBOX_EMAIL is not valid JSON");
      }
      cached.config = parseConfig(data);
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
    const isPiece = path === "/pc/attachment";
    if (!isSync && !isPiece && path !== "/mcp") return json({ error: "not_found" }, 404);
    const config = loadConfig(env);
    if (!config) return json({ error: "not_configured" }, 503);
    const who = await identify(config, isSync || isPiece ? "machines" : "accounts", bearer(request));
    if (!who) return json({ error: "unauthorized" }, 401, { "www-authenticate": "Bearer" });
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { allow: "POST" } });
    const body = await readJson(request, isSync ? MAX_SYNC_BYTES : MAX_MCP_BYTES);
    if (body.status) return json({ error: body.status === 413 ? "too_large" : "bad_json" }, body.status);
    const box = env.MAILBOX.get(env.MAILBOX.idFromName("mailbox"));
    if (isPiece) {
      const want = body.value || {};
      const bytes = await box.piece(who, want.id, want.n, want.piece);
      if (!bytes) return json({ error: "not_found" }, 404);
      return new Response(bytes, { headers: { "content-type": "application/octet-stream", "cache-control": "no-store" } });
    }
    if (isSync) {
      const out = await box.sync(who, body.value);
      return json(out.body, out.status);
    }
    const reply = await box.rpc(who, body.value);
    return reply === null ? new Response(null, { status: 202 }) : json(reply);
  },

  async email(message, env) {
    try {
      const config = loadConfig(env);
      if (!config || !splitRecipient(message.to, config.email.addresses)) {
        message.setReject("no such address");
        return;
      }
      if (!(message.rawSize <= MAIL.rawBytes)) {
        const refused = await env.MAILBOX.get(env.MAILBOX.idFromName("mailbox")).receive({ to: message.to, rawSize: message.rawSize });
        message.setReject(refused.reject || "the message is too large for this mailbox");
        return;
      }
      const parsed = await PostalMime.parse(await new Response(message.raw).arrayBuffer());
      const result = await env.MAILBOX.get(env.MAILBOX.idFromName("mailbox")).receive({
        to: message.to,
        rawSize: message.rawSize,
        headers: parsed.headers.map((h) => ({ key: h.key, value: String(h.value) })),
        parsedFrom: parsed.from && parsed.from.address,
        subject: parsed.subject,
        text: parsed.text,
        html: parsed.html,
        attachments: (parsed.attachments || []).map((a) => ({
          name: a.filename, type: a.mimeType,
          bytes: a.content instanceof ArrayBuffer ? a.content
            : ArrayBuffer.isView(a.content) ? a.content.buffer.slice(a.content.byteOffset, a.content.byteOffset + a.content.byteLength)
              : new TextEncoder().encode(String(a.content ?? "")).buffer,
        })),
      });
      if (result.reject) message.setReject(result.reject);
    } catch (e) {
      console.error(`email handler failed: ${String(e && e.message).slice(0, 200)}`);
      message.setReject("this mailbox could not take the message; try again later");
    }
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

  async receive(input) {
    const box = this.box();
    return box ? box.email.receive(input) : { reject: "this mailbox is not configured" };
  }

  async piece(machine, id, n, piece) {
    const box = this.box();
    return box ? box.email.piece(machine, id, n, piece) : null;
  }
}
