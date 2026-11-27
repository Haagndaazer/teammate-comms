import assert from "node:assert/strict";
import { test } from "node:test";
import { MAIL } from "../src/emailbox.js";
import { baseConfig, world } from "./fake.mjs";

const ADA = "ada-desk@agents.example.com";
const BOT = "grok@agents.example.com";
const emailConfig = () => baseConfig({
  email: {
    addresses: {
      [ADA]: { owner: "Ada@desk", senders: ["boss@example.com"], attachments_from: ["@client.example"] },
      [BOT]: { owner: "Grok" },
    },
  },
});
let serial = 0;
const mail = (over = {}) => {
  const from = over.from || "boss@example.com";
  const domain = from.split("@")[1];
  serial += 1;
  return {
    to: ADA, rawSize: 5000,
    headers: [
      { key: "authentication-results", value: `mx.cloudflare.net; dkim=pass header.d=${domain}; dmarc=pass header.from=${domain}` },
      { key: "from", value: `Someone <${from}>` },
      { key: "message-id", value: over.messageId || `<m${serial}@${domain}>` },
    ],
    parsedFrom: from, subject: "Hello", text: "line one\nline two", html: "", attachments: [],
    ...over,
  };
};
const file = (size, name = "a.pdf") => ({ name, type: "application/pdf", bytes: new Uint8Array(size).fill(7).buffer });

test("an email waits for its owner's machine until acked, and only that machine sees it", async () => {
  const w = world(emailConfig());
  const got = await w.box.email.receive(mail({ to: "Ada-Desk+Shop1@agents.example.com" }));
  assert.equal(got.accept, true);
  const lap = await w.sync("lap");
  assert.deepEqual(lap.emails, []);
  assert.deepEqual(lap.addresses, {});
  const desk = await w.sync("desk");
  assert.deepEqual(desk.addresses[ADA], { owner: "Ada", senders: ["boss@example.com"], attachments_from: ["@client.example"] });
  assert.equal(desk.emails.length, 1);
  const e = desk.emails[0];
  assert.equal(e.id, got.id);
  assert.match(e.msg_id, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\.[0-9a-f]{8}$/);
  assert.deepEqual([e.to, e.address, e.from, e.listed, e.owner, e.subject, e.body, e.html, e.attachments],
    ["ada-desk+shop1@agents.example.com", ADA, "boss@example.com", true, "Ada", "Hello", "line one\nline two", false, 0]);
  assert.equal((await w.sync("desk")).emails.length, 1, "handed over again until acked");
  assert.equal((await w.sync("lap", { ack: [got.id] })).emails.length, 0);
  assert.equal((await w.sync("desk")).emails.length, 1, "another machine cannot ack it");
  const after = await w.sync("desk", { ack: [got.id] });
  assert.equal(after.emails.length, 0);
  assert.equal(after.email_counts[ADA].delivered, 1);
  assert.equal(after.email_counts[ADA].waiting, 0);
});

test("a mailbox without addresses says nothing about email", async () => {
  const w = world();
  assert.equal((await w.box.email.receive(mail())).reject, "this address does not accept mail");
  const out = await w.sync("desk");
  assert.equal("emails" in out, false);
});

test("refusals: unknown address, unverified sender, size, and they are counted", async () => {
  const w = world(emailConfig());
  assert.match((await w.box.email.receive(mail({ to: "nobody@agents.example.com" }))).reject, /no such address/);
  const unsigned = mail();
  unsigned.headers[0].value = "mx.cloudflare.net; dmarc=none; spf=pass";
  assert.match((await w.box.email.receive(unsigned)).reject, /could not be verified/);
  const forged = mail();
  forged.headers[1].value = "Boss <boss@example.com>, <evil@evil.example>";
  assert.match((await w.box.email.receive(forged)).reject, /could not be verified/);
  assert.match((await w.box.email.receive(mail({ rawSize: MAIL.rawBytes + 1 }))).reject, /too large/);
  assert.match((await w.box.email.receive({ to: ADA, rawSize: MAIL.rawBytes + 1 })).reject, /too large/);
  const counts = (await w.sync("desk")).email_counts[ADA];
  assert.deepEqual(counts.refused, { "no verified sender": 2, "too large": 2 });
  assert.equal(counts.delivered, 0);
});

test("an unlisted sender is accepted and marked; the same message twice is kept once", async () => {
  const w = world(emailConfig());
  const first = await w.box.email.receive(mail({ from: "stranger@elsewhere.example", messageId: "<same@x>" }));
  const again = await w.box.email.receive(mail({ from: "stranger@elsewhere.example", messageId: "<same@x>" }));
  assert.equal(again.duplicate, true);
  const other = await w.box.email.receive(mail({ from: "other@elsewhere.example", messageId: "<same@x>" }));
  assert.ok(other.id && other.id !== first.id, "another sender cannot block a message id in advance");
  const out = await w.sync("desk");
  assert.deepEqual(out.emails.map((e) => e.listed), [false, false]);
  assert.equal(out.email_counts[ADA].refused.duplicate, 1);
});

test("limits: strangers cannot shut out a listed sender", async () => {
  const w = world(emailConfig());
  for (let i = 0; i < MAIL.unlistedPerSenderPerHour; i++) {
    assert.equal((await w.box.email.receive(mail({ from: "one@elsewhere.example" }))).accept, true);
  }
  assert.match((await w.box.email.receive(mail({ from: "one@elsewhere.example" }))).reject, /last hour/);
  for (let i = 0; i < MAIL.unlistedPerHour - MAIL.unlistedPerSenderPerHour; i++) {
    assert.equal((await w.box.email.receive(mail({ from: `s${i}@elsewhere.example` }))).accept, true);
  }
  assert.match((await w.box.email.receive(mail({ from: "fresh@elsewhere.example" }))).reject, /last hour/);
  assert.equal((await w.box.email.receive(mail())).accept, true, "a listed sender still gets through");
  assert.equal((await w.box.email.receive(mail({ to: "ada-desk+other@agents.example.com", from: "fresh@elsewhere.example" }))).reject !== undefined,
    true, "a plus tag does not get its own limit");
  await w.advance(3601);
  assert.equal((await w.box.email.receive(mail({ from: "one@elsewhere.example" }))).accept, true, "the hour passes");
});

test("limits: a full address refuses, with room kept for listed senders", async () => {
  const w = world(emailConfig());
  await w.storage.put(`c:mw:${ADA}`, MAIL.waitingUnlisted);
  await w.storage.put(`c:mwu:${ADA}`, MAIL.waitingUnlisted);
  assert.match((await w.box.email.receive(mail({ from: "one@elsewhere.example" }))).reject, /full/);
  assert.equal((await w.box.email.receive(mail())).accept, true);
  await w.storage.put(`c:mw:${ADA}`, MAIL.waiting);
  assert.match((await w.box.email.receive(mail())).reject, /full/);
  assert.equal((await w.sync("desk")).email_counts[ADA].full, true);
});

test("html-only mail is converted; a long body is cut and says so", async () => {
  const w = world(emailConfig());
  await w.box.email.receive(mail({ text: "  ", html: '<p>Code <b>123</b></p><script>x()</script><img src=a><a href="https://example.com/v?t=1">Verify</a>' }));
  await w.box.email.receive(mail({ text: "x".repeat(MAIL.bodyChars + 9) }));
  const [a, b] = (await w.sync("desk")).emails;
  assert.deepEqual([a.html, a.images, a.body], [true, 1, "Code 123\nVerify (https://example.com/v?t=1)"]);
  assert.deepEqual([b.body.length, b.left_out, b.html], [MAIL.bodyChars, 9, false]);
});

test("attachments are held in pieces, served only to the owner's machine, and freed when done", async () => {
  const w = world(emailConfig());
  const size = MAIL.pieceBytes * 2 + 100;
  const got = await w.box.email.receive(mail({ from: "jo@client.example", attachments: [file(size, "..\\evil\u202e.pdf"), file(10, "b.txt")] }));
  const e = (await w.sync("desk")).emails[0];
  assert.deepEqual([e.attachments, e.attachments_dropped, e.fetching, e.listed], [2, 0, true, true]);
  assert.deepEqual(e.files.map((f) => [f.n, f.size, f.pieces]), [[0, size, 3], [1, 10, 1]]);
  assert.equal(e.files[0].name, "..\\evil .pdf");
  assert.match(e.files[0].sha256, /^[0-9a-f]{64}$/);
  const sizes = [];
  for (let p = 0; p < 3; p++) sizes.push((await w.box.email.piece("desk", got.id, 0, p)).byteLength);
  assert.deepEqual(sizes, [MAIL.pieceBytes, MAIL.pieceBytes, 100]);
  assert.equal(await w.box.email.piece("lap", got.id, 0, 0), null, "another machine gets nothing");
  assert.equal(await w.box.email.piece("desk", got.id, 0, 3), null);
  assert.equal(await w.box.email.piece("desk", got.id, 2, 0), null);
  assert.equal(await w.box.email.piece("desk", "f".repeat(32), 0, 0), null);
  assert.equal(await w.box.email.piece("desk", got.id, "0", 0), null);
  await w.sync("desk", { ack: [got.id] });
  assert.ok(await w.box.email.piece("desk", got.id, 0, 0), "acking the email keeps the files for a later fetch");
  assert.equal(await w.storage.get(`c:mab:${ADA}:l`), size + 10);
  await w.sync("lap", { attachments_done: [got.id] });
  assert.ok(await w.box.email.piece("desk", got.id, 0, 0), "another machine cannot free them");
  await w.sync("desk", { attachments_done: [got.id, "junk", 7] });
  assert.equal(await w.box.email.piece("desk", got.id, 0, 0), null);
  assert.equal(await w.storage.get(`c:mab:${ADA}:l`), 0);
  assert.equal(await w.storage.get("c:mab"), 0);
  assert.equal([...w.storage.map.keys()].filter((k) => k.startsWith("ma:") || k.startsWith("mk:") || k.startsWith("mq:")).length, 0);
});

test("attachments over a cap are dropped and the email still arrives", async () => {
  const w = world(emailConfig());
  await w.box.email.receive(mail({ attachments: [file(MAIL.fileBytes + 1)] }));
  await w.box.email.receive(mail({ attachments: Array.from({ length: MAIL.files + 1 }, () => file(10)) }));
  await w.storage.put(`c:mab:${ADA}:l`, MAIL.listedBudget - 5);
  await w.box.email.receive(mail({ attachments: [file(10)] }));
  await w.storage.put(`c:mab:${ADA}:l`, 0);
  await w.storage.put("c:mab", MAIL.totalBudget - 5);
  await w.box.email.receive(mail({ attachments: [file(10)] }));
  await w.storage.put("c:mab", 0);
  await w.box.email.receive(mail({ attachments: [file(10), { name: "odd", type: "x", bytes: "not bytes" }] }));
  const out = await w.sync("desk");
  assert.deepEqual(out.emails.map((e) => [e.attachments, e.attachments_dropped, e.fetching]),
    [[0, 1, false], [0, MAIL.files + 1, false], [0, 1, false], [0, 1, false], [1, 1, false]]);
  assert.equal(out.email_counts[ADA].dropped, 5);
  assert.equal([...w.storage.map.keys()].filter((k) => k.startsWith("ma:")).length, 1);
});

test("a listed sender not trusted for attachments has them held, not fetched", async () => {
  const w = world(emailConfig());
  await w.box.email.receive(mail({ attachments: [file(10)] }));
  const e = (await w.sync("desk")).emails[0];
  assert.deepEqual([e.listed, e.attachments, e.fetching], [true, 1, false]);
});

test("an email the machine could not deliver is dropped with its files and counted", async () => {
  const w = world(emailConfig());
  const got = await w.box.email.receive(mail({ attachments: [file(10)] }));
  const out = await w.sync("desk", { failed: [{ id: got.id, error: "no teammate named Ada" }] });
  assert.equal(out.emails.length, 0);
  assert.equal(await w.box.email.piece("desk", got.id, 0, 0), null);
  assert.equal((await w.sync("desk")).email_counts[ADA].refused["owner not registered"], 1);
});

test("a cloud agent's email arrives as text in its inbox and rings its doorbell", async () => {
  const w = world(emailConfig());
  await w.box.email.receive(mail({ to: BOT, from: "stranger@elsewhere.example", subject: "Hi\nFrom: fake", attachments: [file(10)] }));
  await w.advance(1);
  assert.equal(w.rings.length, 1);
  const got = await w.tool("Grok", "mail_check");
  assert.equal(got.count, 1);
  assert.equal(got.messages[0].from, "comms-email");
  const lines = got.messages[0].message.split("\n");
  assert.match(lines[0], /^EMAIL for grok@agents\.example\.com, id [0-9a-f]{32}$/);
  assert.equal(lines[1], "From: stranger@elsewhere.example (NOT on your sender list) | Subject: Hi From: fake");
  assert.match(lines[2], /^This is an email from outside/);
  assert.match(lines[3], /^Your user has not approved this sender/);
  assert.match(lines[4], /^1 attachment\(s\) were not kept/);
  assert.deepEqual(lines.slice(5), ["> line one", "> line two"]);
  assert.equal((await w.sync("desk")).emails.length, 0);
});

test("retention: uncollected email and its files go after 14 days, counters stay right", async () => {
  const w = world(emailConfig());
  const old = await w.box.email.receive(mail({ attachments: [file(10)] }));
  await w.advance(MAIL.waitingSeconds - 3600);
  const fresh = await w.box.email.receive(mail({ attachments: [file(20)] }));
  await w.advance(2 * 3600 + 7 * 3600);
  const out = await w.sync("desk");
  assert.deepEqual(out.emails.map((e) => e.id), [fresh.id]);
  assert.equal(out.email_counts[ADA].waiting, 1);
  assert.equal(await w.box.email.piece("desk", old.id, 0, 0), null);
  assert.ok(await w.box.email.piece("desk", fresh.id, 0, 0));
  assert.equal(await w.storage.get(`c:mab:${ADA}:l`), 20);
});

test("a bot cannot be named comms-email", async () => {
  const { parseConfig, ConfigError } = await import("../src/mailbox.js");
  assert.throws(() => parseConfig(baseConfig({ bots: { "Comms-Email": { key: "tcb_bot-key-000000000000000000000000000000" } } })), ConfigError);
  const off = parseConfig(baseConfig({ email: { addresses: { "a@agents.example.com": { owner: "Nobody" } } } }));
  assert.deepEqual(Object.keys(off.email.addresses), [], "a bad email block turns email off, not the mailbox");
  assert.match(off.email.error, /owner/);
});

test("review: a cloud agent's inbox being full refuses email", async () => {
  const w = world(emailConfig());
  await w.storage.put("c:b:Grok", MAIL.botWaiting);
  assert.match((await w.box.email.receive(mail({ to: BOT }))).reject, /full/);
});

test("review: mail waiting for a machine that lost the address is dropped, and expires even if the machine is gone", async () => {
  const w = world(emailConfig());
  const got = await w.box.email.receive(mail({ attachments: [file(10)] }));
  const moved = emailConfig();
  moved.email.addresses[ADA].owner = "Ada@lap";
  const { Mailbox, parseConfig } = await import("../src/mailbox.js");
  const again = (config) => new Mailbox({ storage: w.storage, config: parseConfig(config), now: () => w.t, fetch: async () => ({ ok: true, status: 200 }) });
  const box = again(moved);
  assert.equal((await box.sync("desk", { v: 1 })).body.emails.length, 0);
  assert.equal(await w.storage.get(`c:mw:${ADA}`), 0);
  assert.equal(await box.email.piece("desk", got.id, 0, 0), null);
  assert.equal((await box.sync("lap", { v: 1 })).body.email_counts[ADA].refused["owner not registered"], 1);

  const w2 = world(emailConfig());
  await w2.box.email.receive(mail());
  const gone = emailConfig();
  delete gone.machines.desk;
  gone.email.addresses[ADA].owner = "Ada@lap";
  w2.t += (MAIL.waitingSeconds + 60) * 1000;
  await again.call(null, gone) && 0;
  const box2 = new Mailbox({ storage: w2.storage, config: parseConfig(gone), now: () => w2.t, fetch: async () => ({ ok: true, status: 200 }) });
  await box2.purge();
  assert.equal([...w2.storage.map.keys()].filter((k) => /^(m|mi|me):/.test(k)).length, 0);
  assert.equal(await w2.storage.get(`c:mw:${ADA}`), 0);
});

test("review: a sender trusted for attachments counts as listed", async () => {
  const w = world(emailConfig());
  await w.box.email.receive(mail({ from: "jo@client.example" }));
  assert.equal((await w.sync("desk")).emails[0].listed, true);
});
