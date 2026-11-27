import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { MAIL } from "../src/emailbox.js";
import { SEND } from "../src/emailsend.js";
import { baseConfig, world } from "./fake.mjs";

const ADA = "ada-desk@agents.example.com";
const BOT = "grok@agents.example.com";
const QUIET = "quiet@agents.example.com";
const config = (over = {}) => baseConfig({
  email: {
    addresses: {
      [ADA]: { owner: "Ada@desk", agents_from: [BOT], attachment_mb: { [BOT]: 20 }, send_to: [BOT], ...over.ada },
      [BOT]: { owner: "Grok", send_to: [ADA, "boss@example.com"], send_per_day: 3, ...over.bot },
      [QUIET]: { owner: "Quiet" },
    },
  },
});
const grok = { kind: "bot", bot: "Grok", by: "Grok (grok)" };
const quiet = { kind: "bot", bot: "Quiet", by: "Quiet (quiet)" };
const ada = { kind: "machine", machine: "desk", agent: "Ada", by: "Ada@desk" };
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const data = (size, fill = 5) => new Uint8Array(size).fill(fill);

async function upload(w, caller, from, bytes, name = "export.zip", declared = {}) {
  const got = await w.box.send.attach(caller, { from, name, size: bytes.byteLength, sha256: sha(bytes), ...declared });
  if (got.error) return got;
  const open = await w.box.send.uploadOpen(got.value.ticket);
  let pieces = 0;
  for (let at = 0; at < bytes.byteLength; at += MAIL.pieceBytes) {
    await w.box.send.uploadPiece(open, pieces++, bytes.slice(at, at + MAIL.pieceBytes));
  }
  const closed = await w.box.send.uploadClose(open, bytes.byteLength, pieces);
  return { ...closed, ticket: got.value.ticket, id: got.value.attachment };
}
const leftovers = (w) => [...w.storage.map.keys()].filter((k) => /^(up|ui|uq|ma):/.test(k));

test("a cloud agent sends files to a local agent that lists it: delivered inside the mailbox, files fetched", async () => {
  const w = world(config());
  const big = data(MAIL.pieceBytes * 13 + 77);
  const a = await upload(w, grok, BOT, big, "frames-000.zip");
  const b = await upload(w, grok, BOT, data(960, 9), "projects-000.zip");
  assert.equal(a.value.size, big.byteLength);
  const sent = await w.box.send.send(grok, { from: BOT, to: "Ada-Desk+exports@agents.example.com", subject: "Failed export", body: "see files", attachments: [a.id, b.id] });
  assert.deepEqual(sent.value, { sent: true, to: "ada-desk+exports@agents.example.com", delivered_to: "an agent of this mailbox", attachments_kept: 2, attachments_dropped: 0 });
  const e = (await w.sync("desk")).emails[0];
  assert.deepEqual([e.from, e.to, e.listed, e.internal, e.sent_by, e.fetching, e.attachments, e.subject, e.body],
    [BOT, "ada-desk+exports@agents.example.com", true, true, "Grok (grok)", true, 2, "Failed export", "see files"]);
  assert.deepEqual(e.files.map((f) => [f.name, f.size, f.sha256]), [["frames-000.zip", big.byteLength, sha(big)], ["projects-000.zip", 960, sha(data(960, 9))]]);
  const first = await w.box.email.piece("desk", e.id, 0, 13);
  assert.equal(first.byteLength, 77);
  assert.equal(leftovers(w).filter((k) => !k.startsWith(`ma:${e.id}:`)).length, 0, "the uploads are gone once sent");
  assert.equal(await w.storage.get(`c:upn:${BOT}`), 0);
  assert.equal(await w.storage.get(`c:upb:${BOT}`), 0);
});

test("an agent address the recipient does not list is flagged and its files are held", async () => {
  const w = world(config({ ada: { agents_from: [] , attachment_mb: {} } }));
  const a = await upload(w, grok, BOT, data(100));
  await w.box.send.send(grok, { from: BOT, to: ADA, subject: "s", body: "b", attachments: [a.id] });
  const e = (await w.sync("desk")).emails[0];
  assert.deepEqual([e.listed, e.internal, e.fetching, e.attachments], [false, true, false, 1]);
});

test("a cloud agent recipient gets the text, marked as from another agent, without files", async () => {
  const w = world(config());
  const sent = await w.box.send.send(ada, { from: ADA, to: BOT, subject: "Hello", body: "hi there" });
  assert.equal(sent.value.sent, true);
  const got = await w.tool("Grok", "mail_check");
  const lines = got.messages[0].message.split("\n");
  assert.equal(lines[1], `From: ${ADA} (NOT on your sender list) | Subject: Hello`);
  assert.match(lines[2], /another agent of your owner's mailbox/);
  assert.equal(lines.at(-1), "> hi there");
});

test("who may send: only the owner, only from an address with send_to, only to a listed address", async () => {
  const w = world(config());
  const send = (caller, args) => w.box.send.send(caller, { subject: "s", body: "b", ...args });
  assert.match((await send(quiet, { from: BOT, to: ADA })).error, /one of your own addresses: quiet@/);
  assert.match((await send({ kind: "machine", machine: "lap", agent: "Ada", by: "x" }, { from: ADA, to: BOT })).error, /your own addresses; you have none/);
  assert.match((await send({ kind: "machine", machine: "desk", agent: "Mallory", by: "x" }, { from: ADA, to: BOT })).error, /your own addresses/);
  assert.match((await send(quiet, { from: QUIET, to: ADA })).error, /may not send/);
  assert.match((await send(grok, { from: BOT, to: "stranger@elsewhere.example" })).error, /may only write to/);
  assert.match((await send(grok, { from: BOT, to: QUIET })).error, /may only write to/);
  assert.match((await send(grok, { from: BOT, to: "not an address" })).error, /one full email address/);
  assert.match((await send(grok, { from: BOT, to: ADA, subject: "  " })).error, /subject/);
  assert.match((await send(grok, { from: BOT, to: ADA, body: "" })).error, /body/);
  assert.match((await send(grok, { from: BOT, to: ADA, body: "x".repeat(SEND.bodyChars + 1) })).error, /body/);
  assert.match((await send(grok, { from: BOT, to: ADA, attachments: ["nope"] })).error, /attachment ids/);
  assert.match((await send(grok, { from: BOT, to: ADA, attachments: ["a".repeat(32)] })).error, /not an uploaded attachment/);
  assert.match((await send(grok, { from: BOT, to: ADA, request_id: "bad id" })).error, /request_id/);
  assert.equal((await w.sync("desk")).emails.length, 0, "nothing was delivered by any refused send");
});

test("one agent cannot use another address's upload", async () => {
  const w = world(config());
  const a = await upload(w, ada, ADA, data(50));
  assert.match((await w.box.send.send(grok, { from: BOT, to: ADA, subject: "s", body: "b", attachments: [a.id] })).error, /not an uploaded attachment of grok@/);
});

test("tickets: one use, bound to size and hash, expire, and release what they reserved", async () => {
  const w = world(config());
  const bytes = data(1000);
  const good = await upload(w, grok, BOT, bytes);
  assert.equal(await w.box.send.uploadOpen(good.ticket), null, "a used ticket cannot be used again");
  assert.equal(await w.box.send.uploadOpen("z".repeat(64)), null);
  assert.equal(await w.box.send.uploadOpen("a".repeat(64)), null);
  assert.equal(await w.box.send.uploadOpen(undefined), null);
  assert.equal([...w.storage.map.keys()].some((k) => k.includes(good.ticket)), false, "the ticket itself is never stored");

  const short = await w.box.send.attach(grok, { from: BOT, name: "a", size: 1000, sha256: sha(bytes) });
  const open = await w.box.send.uploadOpen(short.value.ticket);
  await w.box.send.uploadPiece(open, 0, bytes.slice(0, 900));
  assert.match((await w.box.send.uploadClose(open, 900, 1)).error, /did not match/);
  const wrong = await upload(w, grok, BOT, data(1000, 6), "a", { sha256: sha(bytes) });
  assert.match(wrong.error, /did not match/);
  assert.equal(leftovers(w).filter((k) => !k.includes(good.id) && !/^(up|uq):/.test(k)).length, 0, "a refused upload leaves nothing");

  const idle = await w.box.send.attach(grok, { from: BOT, name: "a", size: 500, sha256: sha(data(500)) });
  assert.equal(await w.storage.get(`c:upb:${BOT}`), 1500);
  w.t += (SEND.ticketSeconds + 1) * 1000;
  assert.equal(await w.box.send.uploadOpen(idle.value.ticket), null, "an expired ticket is refused");
  await w.box.send.purge();
  assert.equal(await w.storage.get(`c:upn:${BOT}`), 0);
  assert.equal(await w.storage.get(`c:upb:${BOT}`), 0);
  assert.equal(leftovers(w).length, 0);
  assert.match((await w.box.send.send(grok, { from: BOT, to: ADA, subject: "s", body: "b", attachments: [good.id] })).error, /not an uploaded attachment/);
});

test("tickets: bad requests and the limits on waiting uploads", async () => {
  const w = world(config());
  const ok = { from: BOT, name: "a.zip", size: 10, sha256: sha(data(10)) };
  const attach = (over) => w.box.send.attach(grok, { ...ok, ...over });
  assert.match((await attach({ from: ADA })).error, /your own addresses/);
  assert.match((await attach({ name: " \n " })).error, /name/);
  assert.match((await attach({ size: 0 })).error, /size/);
  assert.match((await attach({ size: SEND.fileBytes + 1 })).error, /size/);
  assert.match((await attach({ size: "10" })).error, /size/);
  assert.match((await attach({ sha256: "abc" })).error, /sha256/);
  assert.match((await w.box.send.attach(quiet, { ...ok, from: QUIET })).error, /may not send/);
  for (let i = 0; i < SEND.tickets; i++) assert.ok((await attach({})).value.ticket);
  assert.match((await attach({})).error, /too many attachments/);
  const w2 = world(config());
  const big = { ...ok, size: SEND.fileBytes };
  assert.ok((await w2.box.send.attach(grok, big)).value);
  assert.ok((await w2.box.send.attach(grok, big)).value);
  assert.match((await w2.box.send.attach(grok, big)).error, /too many attachments/);
});

test("limits: a daily count per address, and a repeated request id does not send twice", async () => {
  const w = world(config());
  const send = (n, extra = {}) => w.box.send.send(grok, { from: BOT, to: ADA, subject: `s${n}`, body: "b", ...extra });
  const first = await send(1, { request_id: "run-1" });
  const again = await send(1, { request_id: "run-1" });
  assert.equal(again.value.already_sent, true);
  assert.equal((await w.sync("desk")).emails.length, 1);
  assert.equal(first.value.sent, true);
  await send(2);
  await send(3);
  assert.match((await send(4)).error, /daily sending limit/);
  w.t += 86401 * 1000;
  assert.equal((await send(5)).value.sent, true);
  assert.equal((await w.sync("desk")).email_counts[ADA].delivered, 4);
});

test("size: the default is 10 MB, a listed agent can be given more, and a refused email is not sent", async () => {
  const w = world(config({ ada: { agents_from: [BOT], attachment_mb: {} } }));
  const eleven = data(11 * 1024 * 1024);
  const a = await upload(w, grok, BOT, eleven);
  const sent = await w.box.send.send(grok, { from: BOT, to: ADA, subject: "s", body: "b", attachments: [a.id] });
  assert.match(sent.error, /takes at most 10485760 from you/);
  assert.ok(await w.storage.get(`ui:${a.id}`), "a refused email keeps its upload");
  assert.equal((await w.sync("desk")).emails.length, 0);
  assert.equal(((await w.storage.get(`ms:${BOT}`)) || []).length, 0);
  const w2 = world(config());
  const b = await upload(w2, grok, BOT, eleven);
  const ok = await w2.box.send.send(grok, { from: BOT, to: ADA, subject: "s", body: "b", attachments: [b.id] });
  assert.deepEqual([ok.value.attachments_kept, ok.value.attachments_dropped], [1, 0]);
  await w2.storage.put(`c:mw:${ADA}`, MAIL.waiting);
  const c = await upload(w2, grok, BOT, data(10));
  const full = await w2.box.send.send(grok, { from: BOT, to: ADA, subject: "s", body: "b", attachments: [c.id] });
  assert.match(full.error, /cannot take more right now \(full\)/);
  assert.ok(await w2.storage.get(`ui:${c.id}`), "the upload is kept so the send can be tried again");
});

test("outside recipients go through the sending service, only when it is set up", async () => {
  const w = world(config());
  const args = { from: BOT, to: "boss@example.com", subject: "Report", body: "text" };
  assert.match((await w.box.send.send(grok, args)).error, /not set up/);
  const out = [];
  w.box.send.sendMail = async (mail) => out.push(mail);
  const a = await upload(w, grok, BOT, data(2000), "r.pdf");
  const sent = await w.box.send.send(grok, { ...args, attachments: [a.id] });
  assert.match(sent.value.delivered_to, /not confirmed/);
  assert.deepEqual([out[0].from, out[0].to, out[0].subject, out[0].text, out[0].attachments[0].name, out[0].attachments[0].bytes.byteLength],
    [BOT, "boss@example.com", "Report", "text", "r.pdf", 2000]);
  assert.equal(leftovers(w).length, 0);
  w.box.send.sendMail = async () => { throw Object.assign(new Error("x"), { code: "E_DAILY_LIMIT_EXCEEDED" }); };
  assert.match((await w.box.send.send(grok, args)).error, /not sent: E_DAILY_LIMIT_EXCEEDED/);
  const big = await upload(w, grok, BOT, data(SEND.outsideBytes + 1));
  assert.match((await w.box.send.send(grok, { ...args, attachments: [big.id] })).error, /takes at most 15728640/);
  assert.match((await w.box.send.send(grok, { ...args, to: "boss+tag@example.com" })).error, /may only write to/, "no plus tag on an outside address");
});

test("cloud tools: offered only to an account that owns an address; the upload address hides nothing else", async () => {
  const w = world(config());
  const list = async (account) => (await w.box.rpc(account, { jsonrpc: "2.0", id: 1, method: "tools/list" })).result.tools.map((t) => t.name);
  assert.ok((await list("grok")).includes("email_send"));
  const none = world();
  assert.equal((await none.box.rpc("grok", { jsonrpc: "2.0", id: 1, method: "tools/list" })).result.tools.some((t) => t.name.startsWith("email_")), false);
  const call = async (name, args) => {
    const reply = await w.box.rpc("grok", { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: { me: "Grok", ...args } } }, "https://box.example");
    return reply.result.isError ? { error: reply.result.content[0].text } : JSON.parse(reply.result.content[0].text);
  };
  assert.deepEqual((await call("email_addresses", {})).addresses, [{ address: BOT, can_send_to: [ADA, "boss@example.com"], sends_per_day: 3 }]);
  const got = await call("email_attach", { from: BOT, name: "a.zip", size: 10, sha256: sha(data(10)) });
  assert.match(got.upload_address, /^https:\/\/box\.example\/up\/[0-9a-f]{64}$/);
  assert.equal("ticket" in got, false);
  assert.equal((await call("email_send", { from: BOT, to: ADA, subject: "s", body: "b" })).sent, true);
});

test("a machine's relay sends for a local agent, and only for that machine's agents", async () => {
  const w = world(config());
  const ask = (machine, body) => w.box.machineEmail(machine, { v: 1, ...body });
  const ok = await ask("desk", { agent: "Ada", action: "send", args: { from: ADA, to: BOT, subject: "s", body: "b" } });
  assert.equal(ok.body.ok.sent, true);
  assert.match((await ask("lap", { agent: "Ada", action: "send", args: { from: ADA, to: BOT, subject: "s", body: "b" } })).body.refused, /your own addresses/);
  assert.ok((await ask("desk", { agent: "Ada", action: "attach", args: { from: ADA, name: "a", size: 5, sha256: sha(data(5)) } })).body.ok.ticket);
  assert.equal((await ask("desk", { agent: "Ada", action: "nope" })).status, 400);
  assert.equal((await ask("desk", { agent: "a b", action: "send" })).status, 400);
  assert.equal((await w.box.machineEmail("desk", { v: 9 })).status, 400);
});

test("review: simultaneous sends cannot pass the daily limit, reuse an upload, or send one request twice", async () => {
  const w = world(config({ bot: { send_per_day: 2 } }));
  let calls = 0;
  w.box.send.sendMail = async () => { calls++; await new Promise((r) => setTimeout(r, 15)); };
  const a = await upload(w, grok, BOT, data(100));
  const args = { from: BOT, to: "boss@example.com", subject: "s", body: "b" };
  const all = await Promise.all([1, 2, 3, 4, 5].map((n) => w.box.send.send(grok, { ...args, subject: `s${n}`, attachments: n === 1 ? [a.id] : [] })));
  assert.equal(all.filter((r) => r.value).length, 2);
  assert.equal(calls, 2);
  assert.equal((await w.storage.get(`ms:${BOT}`)).length, 2);
  const w2 = world(config());
  w2.box.send.sendMail = async () => { calls++; await new Promise((r) => setTimeout(r, 15)); };
  calls = 0;
  const b = await upload(w2, grok, BOT, data(100));
  const both = await Promise.all([1, 2].map(() => w2.box.send.send(grok, { ...args, attachments: [b.id] })));
  assert.equal(both.filter((r) => r.value).length, 1, "one upload goes out once");
  const same = await Promise.all([1, 2].map(() => w2.box.send.send(grok, { ...args, request_id: "r1" })));
  assert.deepEqual(same.map((r) => Boolean(r.value?.already_sent)), [false, true]);
  assert.equal(calls, 2);
});

test("review: a request id belongs to one email, and a failed send releases what it reserved", async () => {
  const w = world(config());
  const out = [];
  w.box.send.sendMail = async (mail) => out.push(mail);
  const args = { from: BOT, to: "boss@example.com", subject: "first", body: "b", request_id: "r1" };
  assert.equal((await w.box.send.send(grok, args)).value.sent, true);
  assert.match((await w.box.send.send(grok, { ...args, to: ADA })).error, /different email/);
  assert.match((await w.box.send.send(grok, { ...args, subject: "second" })).error, /different email/);
  assert.equal(out.length, 1);
  w.box.send.sendMail = async () => { throw new Error("down"); };
  const a = await upload(w, grok, BOT, data(100));
  const failed = await w.box.send.send(grok, { ...args, request_id: "r2", attachments: [a.id] });
  assert.match(failed.error, /not sent/);
  assert.equal((await w.storage.get(`ms:${BOT}`)).length, 1, "the daily count is given back");
  w.box.send.sendMail = async (mail) => out.push(mail);
  const retry = await w.box.send.send(grok, { ...args, request_id: "r2", attachments: [a.id] });
  assert.equal(retry.value.sent, true, "the same request and upload can be tried again");
  assert.equal(out.length, 2);
});

test("review: a ticket removed while its upload is still running leaves nothing behind", async () => {
  const w = world(config());
  const bytes = data(MAIL.pieceBytes * 3);
  const got = await w.box.send.attach(grok, { from: BOT, name: "a", size: bytes.byteLength, sha256: sha(bytes) });
  const open = await w.box.send.uploadOpen(got.value.ticket);
  await w.box.send.uploadPiece(open, 0, bytes.slice(0, MAIL.pieceBytes));
  w.t += (SEND.ticketSeconds + 60) * 1000;
  await w.box.send.purge();
  assert.ok(await w.storage.get(open.key), "a running upload is not purged while it is young");
  w.t += SEND.busySeconds * 1000;
  await w.box.send.purge();
  assert.equal(await w.storage.get(open.key), undefined);
  await w.box.send.uploadPiece(open, 1, bytes.slice(MAIL.pieceBytes, MAIL.pieceBytes * 2));
  await w.box.send.uploadPiece(open, 2, bytes.slice(MAIL.pieceBytes * 2));
  assert.match((await w.box.send.uploadClose(open, bytes.byteLength, 3)).error, /expired/);
  assert.equal(leftovers(w).length, 0);
  assert.equal(await w.storage.get(`c:upn:${BOT}`), 0);
});
