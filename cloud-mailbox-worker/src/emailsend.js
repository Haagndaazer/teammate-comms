// Sending email from an agent address: upload tickets for attachments, limits, delivery.
import { attachmentLimit, cleanLine, parseAddress } from "./email.js";
import { MAIL } from "./emailbox.js";

const MIB = 1024 * 1024;
export const SEND = {
  ticketSeconds: 900,
  tickets: 10,
  flightBytes: 50 * MIB,
  fileBytes: 20 * MIB,
  files: 10,
  outsideBytes: 15 * MIB,
  mailboxPerDay: 200,
  subjectChars: 200,
  bodyChars: 48000,
  nameChars: 200,
  busySeconds: 3600,
};

const ID_RE = /^[0-9a-f]{32}$/;
const SHA_RE = /^[0-9a-f]{64}$/;
export const TICKET_RE = /^[0-9a-f]{64}$/;

function hex(bytes) {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256(data) {
  return hex(await crypto.subtle.digest("SHA-256", typeof data === "string" ? new TextEncoder().encode(data) : data));
}

function pieceKey(id, piece) {
  return `ma:${id}:0:${String(piece).padStart(4, "0")}`;
}

export class SendBox {
  // caller: { kind: "bot", bot, by } or { kind: "machine", machine, agent, by }
  constructor(box, email, sendMail) {
    this.box = box;
    this.storage = box.storage;
    this.email = email;
    this.sendMail = sendMail || null;
    this.queue = Promise.resolve();
  }

  owns(address, caller) {
    const entry = this.email.addresses[address];
    if (!entry || !caller) return false;
    const owner = entry.owner;
    return owner.kind === "bot" ? caller.kind === "bot" && caller.bot === owner.bot
      : caller.kind === "machine" && caller.machine === owner.machine && caller.agent === owner.agent;
  }

  addressesFor(caller) {
    return Object.entries(this.email.addresses).filter(([address]) => this.owns(address, caller))
      .map(([address, entry]) => ({ address, can_send_to: entry.sendTo, sends_per_day: entry.sendPerDay }));
  }

  sender(caller, from) {
    const parsed = parseAddress(typeof from === "string" ? from.trim() : "");
    if (!parsed || !this.owns(parsed.address, caller)) {
      const mine = this.addressesFor(caller).map((a) => a.address).join(", ");
      return { error: `from must be one of your own addresses${mine ? `: ${mine}` : "; you have none. Your owner adds one in the mailbox settings"}` };
    }
    const entry = this.email.addresses[parsed.address];
    if (!entry.sendTo.length) {
      return { error: `${parsed.address} may not send. Your owner lists who it can write to in the mailbox settings (send_to)` };
    }
    return { address: parsed.address, entry };
  }

  async attach(caller, args) {
    const who = this.sender(caller, args.from);
    if (who.error) return who;
    const name = cleanLine(args.name, SEND.nameChars);
    if (!name) return { error: "name is required: the file's name" };
    if (!Number.isInteger(args.size) || args.size < 1 || args.size > SEND.fileBytes) {
      return { error: `size must be the file's exact size in bytes, from 1 to ${SEND.fileBytes}` };
    }
    if (typeof args.sha256 !== "string" || !SHA_RE.test(args.sha256.trim().toLowerCase())) {
      return { error: "sha256 must be the file's SHA-256 as 64 hex characters (for example from: sha256sum <file>)" };
    }
    await this.purge();
    if ((await this.box.count(`upn:${who.address}`)) >= SEND.tickets
      || (await this.box.count(`upb:${who.address}`)) + args.size > SEND.flightBytes) {
      return { error: "too many attachments are waiting to be sent from this address. Send the email they belong to, or wait 15 minutes for them to expire" };
    }
    const ticket = hex(crypto.getRandomValues(new Uint8Array(32)));
    const id = crypto.randomUUID().replace(/-/g, "");
    const key = `up:${await sha256(ticket)}`;
    const seq = await this.box.nextSeq();
    const now = this.box.seconds();
    await this.storage.put(key, {
      id, address: who.address, name, size: args.size, sha256: args.sha256.trim().toLowerCase(),
      state: "open", pieces: 0, seq, at: now,
    });
    await this.storage.put(`ui:${id}`, key);
    await this.storage.put(`uq:${seq}`, { key, at: now });
    await this.box.bump(`upn:${who.address}`, 1);
    await this.box.bump(`upb:${who.address}`, args.size);
    await this.box.scheduleRings();
    return { value: { attachment: id, ticket, expires_in_seconds: SEND.ticketSeconds } };
  }

  // Claim a ticket for its one upload. null for anything that is not an open, unexpired ticket.
  async uploadOpen(ticket) {
    if (typeof ticket !== "string" || !TICKET_RE.test(ticket)) return null;
    const key = `up:${await sha256(ticket)}`;
    const record = await this.storage.get(key);
    if (!record || record.state !== "open" || this.box.seconds() - record.at > SEND.ticketSeconds) return null;
    await this.storage.put(key, { ...record, state: "busy" });
    return { key, ...record };
  }

  // false when the ticket is no longer this upload's to write: it expired and was removed.
  async stillOpen(upload) {
    const record = await this.storage.get(upload.key);
    return Boolean(record && record.id === upload.id && record.state === "busy");
  }

  async uploadPiece(upload, piece, bytes) {
    await this.storage.put(pieceKey(upload.id, piece), bytes);
  }

  async dropPieces(id, pieces) {
    for (let p = 0; p < pieces; p++) await this.storage.delete(pieceKey(id, p));
  }

  async readFile(id, pieces, size) {
    const out = new Uint8Array(size);
    let at = 0;
    for (let p = 0; p < pieces; p++) {
      const part = await this.storage.get(pieceKey(id, p));
      if (!part || at + part.byteLength > size) return null;
      out.set(part instanceof Uint8Array ? part : new Uint8Array(part), at);
      at += part.byteLength;
    }
    return at === size ? out.buffer : null;
  }

  async uploadClose(upload, total, pieces) {
    const refused = { error: "the upload did not match the size and SHA-256 given for it, or its ticket expired, so it was discarded. Ask for a new ticket" };
    if (!(await this.stillOpen(upload))) {
      await this.dropPieces(upload.id, Math.max(pieces, Math.ceil(upload.size / MAIL.pieceBytes)));
      return refused;
    }
    const whole = total === upload.size ? await this.readFile(upload.id, pieces, upload.size) : null;
    if (!whole || (await sha256(whole)) !== upload.sha256) {
      await this.discard(upload.key, pieces);
      return refused;
    }
    const { key, ...record } = upload;
    await this.storage.put(key, { ...record, state: "ready", pieces });
    return { value: { attachment: upload.id, size: total, sha256: upload.sha256 } };
  }

  async discard(key, piecesWritten) {
    const record = await this.storage.get(key);
    if (!record) return;
    await this.dropPieces(record.id, Math.max(record.pieces || 0, piecesWritten || 0, Math.ceil(record.size / MAIL.pieceBytes)));
    await this.storage.delete(key);
    await this.storage.delete(`ui:${record.id}`);
    await this.storage.delete(`uq:${record.seq}`);
    await this.box.bump(`upn:${record.address}`, -1);
    await this.box.bump(`upb:${record.address}`, -record.size);
  }

  send(caller, args) {
    const run = this.queue.then(() => this.sendOne(caller, args));
    this.queue = run.catch(() => {});
    return run;
  }

  async sendOne(caller, args) {
    const who = this.sender(caller, args.from);
    if (who.error) return who;
    const to = parseAddress(typeof args.to === "string" ? args.to.trim() : "");
    if (!to) return { error: "to must be one full email address" };
    const plus = to.local.indexOf("+");
    const base = `${plus < 0 ? to.local : to.local.slice(0, plus)}@${to.domain}`;
    const internal = Object.hasOwn(this.email.addresses, base);
    if (!who.entry.sendTo.includes(internal ? base : to.address)) {
      return { error: `${who.address} may only write to: ${who.entry.sendTo.join(", ")}. Nothing was sent` };
    }
    const subject = cleanLine(args.subject, SEND.subjectChars);
    if (!subject) return { error: "subject is required" };
    if (typeof args.body !== "string" || !args.body.trim() || args.body.length > SEND.bodyChars) {
      return { error: `body is required, plain text of at most ${SEND.bodyChars} characters` };
    }
    const ids = args.attachments === undefined ? [] : args.attachments;
    if (!Array.isArray(ids) || ids.length > SEND.files || ids.some((id) => typeof id !== "string" || !ID_RE.test(id))
      || new Set(ids).size !== ids.length) {
      return { error: `attachments must be a list of at most ${SEND.files} attachment ids from email_attach` };
    }
    const now = this.box.seconds();
    let requestKey = "";
    let print = "";
    if (args.request_id !== undefined) {
      if (typeof args.request_id !== "string" || !/^[A-Za-z0-9._-]{1,64}$/.test(args.request_id)) {
        return { error: "request_id must be 1 to 64 letters, digits, dots, dashes or underscores" };
      }
      requestKey = `msr:${await sha256(`${who.address}\n${args.request_id}`)}`;
      print = await sha256(JSON.stringify([to.address, subject, args.body, ids.length]));
      const before = await this.storage.get(requestKey);
      if (before && before.print !== print) {
        return { error: "that request_id was already used for a different email. Use a new request_id. Nothing was sent" };
      }
      if (before && before.result) return { value: { ...before.result, already_sent: true } };
      if (before) return { error: "an email with that request_id is being sent right now. Check again in a minute; do not send it again" };
    }
    const day = (list) => (Array.isArray(list) ? list.filter((t) => t > now - 86400) : []);
    const mine = day(await this.storage.get(`ms:${who.address}`));
    const all = day(await this.storage.get("ms"));
    if (mine.length >= who.entry.sendPerDay || all.length >= SEND.mailboxPerDay) {
      return { error: "the daily sending limit is reached. Try again tomorrow, or ask your owner to raise send_per_day" };
    }
    const uploads = [];
    for (const id of ids) {
      const key = await this.storage.get(`ui:${id}`);
      const record = key ? await this.storage.get(key) : null;
      if (!record || record.address !== who.address || record.state !== "ready") {
        return { error: `attachment ${id} is not an uploaded attachment of ${who.address}. Upload it first, and note that a ticket expires after 15 minutes` };
      }
      uploads.push({ key, ...record });
    }
    const bytes = uploads.reduce((n, u) => n + u.size, 0);
    const target = internal ? this.email.addresses[base] : null;
    const cap = !internal ? SEND.outsideBytes
      : (target.agentsFrom.includes(who.address) ? attachmentLimit(target, who.address) : 10) * MIB;
    if (bytes > cap) {
      return { error: `the attachments total ${bytes} bytes and ${to.address} takes at most ${cap} from you in one email. Nothing was sent; send fewer or smaller files` };
    }
    if (!internal && !this.sendMail) {
      return { error: "sending outside this mailbox is not set up. Your owner deploys the mailbox with email sending enabled" };
    }

    await this.storage.put(`ms:${who.address}`, [...mine, now]);
    await this.storage.put("ms", [...all, now]);
    if (requestKey) await this.storage.put(requestKey, { print, at: now });
    for (const upload of uploads) {
      const { key, ...record } = upload;
      await this.storage.put(key, { ...record, state: "sending" });
    }
    const undo = async (text) => {
      const without = (list) => {
        const kept = day(list);
        const at = kept.indexOf(now);
        if (at >= 0) kept.splice(at, 1);
        return kept;
      };
      await this.storage.put(`ms:${who.address}`, without(await this.storage.get(`ms:${who.address}`)));
      await this.storage.put("ms", without(await this.storage.get("ms")));
      if (requestKey) await this.storage.delete(requestKey);
      for (const upload of uploads) {
        const { key, ...record } = upload;
        if (await this.storage.get(key)) await this.storage.put(key, record);
      }
      return { error: text };
    };

    const files = [];
    for (const upload of uploads) {
      const data = await this.readFile(upload.id, upload.pieces, upload.size);
      if (!data) return undo(`attachment ${upload.id} could not be read. Upload it again`);
      files.push({ name: upload.name, type: "application/octet-stream", bytes: data });
    }
    let result;
    if (internal) {
      const got = await this.email.receiveInternal({
        from: who.address, to: to.address, subject, text: args.body, attachments: files, by: caller.by,
      });
      if (got.reject || got.retry) {
        return undo(`${base} did not take the email: ${got.reject || `it cannot take more right now (${got.retry})`}. Nothing was sent`);
      }
      result = { sent: true, to: to.address, delivered_to: "an agent of this mailbox", attachments_kept: got.attachments, attachments_dropped: got.dropped };
    } else {
      try {
        await this.sendMail({ from: who.address, to: to.address, subject, text: args.body, attachments: files });
      } catch (e) {
        return undo(`the email was not sent: ${cleanLine(String(e && (e.code || e.message)), 200) || "the sending service refused it"}`);
      }
      result = { sent: true, to: to.address, delivered_to: "the sending service; delivery to the recipient is not confirmed", attachments_kept: files.length, attachments_dropped: 0 };
    }
    for (const upload of uploads) await this.discard(upload.key);
    await this.email.tally(who.address, "sent");
    if (requestKey) {
      await this.storage.put(requestKey, { print, at: now, result });
      await this.storage.put(`x:${await this.box.nextSeq()}`, { key: requestKey, at: now });
    }
    return { value: result };
  }

  async purge() {
    const now = this.box.seconds();
    const old = await this.storage.list({ prefix: "uq:", limit: MAIL.purgeStep });
    let removed = 0;
    for (const [index, entry] of old) {
      if (!(entry.at < now - SEND.ticketSeconds)) break;
      const record = await this.storage.get(entry.key);
      if (record && (record.state === "busy" || record.state === "sending") && entry.at > now - SEND.busySeconds) continue;
      if (record) await this.discard(entry.key);
      else await this.storage.delete(index);
      removed++;
    }
    return removed === MAIL.purgeStep;
  }
}
