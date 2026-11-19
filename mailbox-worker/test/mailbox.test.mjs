import assert from "node:assert/strict";
import { test } from "node:test";
import { ConfigError, DOORBELL, LIMITS, RETAIN, identify, parseConfig } from "../src/mailbox.js";
import { KEYS, baseConfig, eid, event, world } from "./fake.mjs";

test("config: weak, shared or malformed entries are refused", () => {
  const bad = (cfg, why) => assert.throws(() => parseConfig(cfg), ConfigError, why);
  bad("{not json", "not json");
  bad([], "array");
  bad({ machines: {}, bots: { B: { key: KEYS.bot } } }, "no machines");
  bad({ machines: { desk: KEYS.desk }, bots: {} }, "no bots");
  bad({ machines: { desk: "short" }, bots: { B: { key: KEYS.bot } } }, "short key");
  bad({ machines: { desk: KEYS.desk }, bots: { B: { key: KEYS.desk } } }, "key shared across kinds");
  bad({ machines: { "Bad_Name": KEYS.desk }, bots: { B: { key: KEYS.bot } } }, "machine name");
  bad({ machines: { desk: KEYS.desk }, bots: { "b@d": { key: KEYS.bot } } }, "bot name");
  bad({ machines: { desk: KEYS.desk }, bots: { "comms-relay": { key: KEYS.bot } } }, "reserved name");
  bad({ machines: { desk: KEYS.desk }, bots: { B: { key: KEYS.bot }, b: { key: KEYS.bot2 } } }, "case clash");
  bad({ machines: { desk: KEYS.desk }, bots: { B: { key: KEYS.bot, doorbell_url: "http://x.example/h", doorbell_key: "k" } } }, "http doorbell");
  bad({ machines: { desk: KEYS.desk }, bots: { B: { key: KEYS.bot, doorbell_url: "https://x.example/h" } } }, "doorbell without key");
  const ok = parseConfig(JSON.stringify(baseConfig()));
  assert.deepEqual(Object.keys(ok.machines), ["desk", "lap"]);
  assert.equal(ok.bots.Quiet.doorbell_url, undefined);
});

test("identity comes from the key and the two kinds never cross", async () => {
  const config = parseConfig(baseConfig());
  assert.equal(await identify(config, "machines", KEYS.desk), "desk");
  assert.equal(await identify(config, "bots", KEYS.bot), "Grok");
  assert.equal(await identify(config, "bots", KEYS.desk), null);
  assert.equal(await identify(config, "machines", KEYS.bot), null);
  assert.equal(await identify(config, "machines", ""), null);
  assert.equal(await identify(config, "machines", undefined), null);
  assert.equal(await identify(config, "bots", KEYS.bot + "x"), null);
});

test("a message from a machine reaches the bot once, with the machine added to the sender", async () => {
  const w = world();
  const ev = event("Grok", "hello bot", { msg_id: "2026-11-18T10:00:00.000000.ab12", reply_to: "prev" });
  const first = await w.sync("desk", { events: [ev] });
  assert.deepEqual(first.accepted, [ev.id]);
  assert.equal(first.you_are, "desk");
  assert.equal(first.bots.find((b) => b.name === "Grok").waiting, 1);
  const again = await w.sync("desk", { events: [ev] });
  assert.deepEqual(again.accepted, [ev.id], "a resend is accepted again");
  assert.equal(again.bots.find((b) => b.name === "Grok").waiting, 1, "but not stored twice");
  const got = await w.tool("Grok", "mail_check");
  assert.equal(got.count, 1);
  assert.deepEqual(got.messages[0], {
    id: "2026-11-18T10:00:00.000000.ab12", from: "alice@desk",
    sent: "2027-01-15T08:00:00.000Z", message: "hello bot", reply_to: "prev",
  });
  assert.equal((await w.tool("Grok", "mail_check")).count, 0, "returned once");
  const reread = await w.tool("Grok", "mail_check", { reread: 5 });
  assert.equal(reread.messages[0].message, "hello bot", "still available to reread");
  assert.equal((await w.tool("Quiet", "mail_check")).count, 0, "another bot sees nothing");
});

test("events that cannot be delivered are rejected with the reason, and stay rejected", async () => {
  const w = world();
  const unknown = event("Nobody", "hi");
  const empty = event("Grok", "   ");
  const long = event("Grok", "x".repeat(LIMITS.messageChars + 1));
  const badFrom = event("Grok", "hi", { from: "al ice" });
  const out = await w.sync("desk", { events: [unknown, empty, long, badFrom, { id: "nope" }, null] });
  assert.deepEqual(out.accepted, []);
  assert.equal(out.rejected.length, 4);
  assert.match(out.rejected[0].error, /no cloud agent named Nobody/);
  assert.match(out.rejected[0].error, /Grok, Quiet/);
  const again = await w.sync("desk", { events: [unknown] });
  assert.match(again.rejected[0].error, /no cloud agent named Nobody/);
  assert.equal((await w.tool("Grok", "mail_check")).count, 0);
  assert.equal((await w.box.sync("desk", { v: 2 })).status, 400);
  assert.equal((await w.box.sync("desk", null)).status, 400);
});

test("a bot's message waits for its machine until acked", async () => {
  const w = world();
  await w.sync("desk", { roster: [{ name: "alice", live: true }, { name: "bob", live: false }] });
  const sent = await w.tool("Grok", "mail_send", { to: "alice@desk", message: "  hi alice  ", reply_to: "m1" });
  assert.equal(sent.to, "alice@desk");
  assert.match(sent.sent, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\.[0-9a-f]{8}$/);
  assert.equal((await w.sync("lap")).messages.length, 0, "the other machine does not see it");
  const first = await w.sync("desk");
  assert.equal(first.messages.length, 1);
  const m = first.messages[0];
  assert.equal(m.from, "Grok");
  assert.equal(m.to, "alice");
  assert.equal(m.message, "hi alice");
  assert.equal(m.reply_to, "m1");
  assert.equal(m.msg_id, sent.sent);
  assert.match(m.id, /^[0-9a-f]{32}$/);
  assert.equal((await w.sync("desk")).messages.length, 1, "redelivered until acked");
  assert.equal((await w.sync("lap", { ack: [m.id] })).messages.length, 0);
  assert.equal((await w.sync("desk")).messages.length, 1, "another machine cannot ack it");
  assert.equal((await w.sync("desk", { ack: [m.id] })).messages.length, 0);
  assert.equal((await w.sync("desk", { ack: [m.id] })).messages.length, 0, "a second ack is harmless");
});

test("a message the machine could not deliver comes back to the bot as a notice", async () => {
  const w = world();
  await w.tool("Grok", "mail_send", { to: "ghost@desk", message: "anyone?" });
  const m = (await w.sync("desk")).messages[0];
  await w.sync("desk", { failed: [{ id: m.id, error: "no teammate named ghost is registered on desk" }] });
  assert.equal((await w.sync("desk")).messages.length, 0);
  const got = await w.tool("Grok", "mail_check");
  assert.equal(got.messages[0].from, "comms-relay");
  assert.match(got.messages[0].message, /ghost@desk was not delivered.*no teammate named ghost/);
});

test("recipient addresses are resolved against the machines and their rosters", async () => {
  const w = world();
  const send = (to) => w.tool("Grok", "mail_send", { to, message: "x" });
  assert.match((await send("alice")).error, /no teammate named alice is known/);
  assert.match((await send("alice@nowhere")).error, /no machine named "nowhere".*desk, lap/);
  assert.match((await send("al ice@desk")).error, /not a valid teammate address/);
  assert.match((await send("a@b@c")).error, /not a valid teammate address/);
  assert.match((await send("")).error, /to is required/);
  assert.equal((await send("alice@DESK")).to, "alice@desk", "no roster yet: accepted, machine lowercased");
  await w.sync("desk", { roster: [{ name: "alice", live: true }, { name: "dana" }, { name: "bad name" }, { name: "alice" }] });
  await w.sync("lap", { roster: [{ name: "dana", live: true }] });
  assert.equal((await send("alice")).to, "alice@desk", "bare name found on exactly one machine");
  assert.match((await send("dana")).error, /more than one machine: dana@desk, dana@lap/);
  assert.match((await send("zed@desk")).error, /no teammate named zed is registered on desk. Registered there: alice, dana/);
  const mates = await w.tool("Grok", "mail_teammates");
  assert.equal(mates.you_are, "Grok");
  assert.deepEqual(mates.machines[0].teammates, [
    { address: "alice@desk", running: true }, { address: "dana@desk", running: false }]);
  const single = world({ machines: { desk: KEYS.desk }, bots: { Grok: { key: KEYS.bot } } });
  assert.equal((await single.tool("Grok", "mail_send", { to: "alice", message: "x" })).to, "alice@desk");
});

test("limits are errors, never silent drops", async () => {
  const w = world();
  const send = (message) => w.tool("Grok", "mail_send", { to: "alice@desk", message });
  assert.match((await send("")).error, /message is required/);
  assert.match((await send("x".repeat(LIMITS.messageChars + 1))).error, /limit is 65536/);
  assert.equal((await send("x".repeat(LIMITS.messageChars))).to, "alice@desk");
  for (let i = 1; i < LIMITS.sendsPerMinute; i++) assert.ok((await send(`m${i}`)).sent);
  assert.match((await send("one too many")).error, /more than 30 messages in a minute/);
  await w.advance(61);
  assert.ok((await send("after the minute")).sent);
  w.storage.map.set("c:p:desk", LIMITS.waitingPerRecipient);
  await w.advance(61);
  assert.match((await send("full")).error, /500 messages it has not collected/);
  w.storage.map.set("c:b:Grok", LIMITS.waitingPerRecipient);
  const waits = event("Grok", "hi");
  const out = await w.sync("desk", { events: [waits] });
  assert.deepEqual(out.rejected, []);
  assert.match(out.deferred[0].error, /has not collected 500 messages/);
  w.storage.map.set("c:b:Grok", 0);
  assert.deepEqual((await w.sync("desk", { events: [waits] })).accepted, [waits.id], "a deferred event is taken later");
});

test("a large mailbox is handed over in batches", async () => {
  const w = world();
  const big = "y".repeat(60000);
  const events = Array.from({ length: 40 }, () => event("Quiet", big));
  assert.equal((await w.sync("desk", { events })).accepted.length, 40);
  const first = await w.tool("Quiet", "mail_check");
  assert.ok(first.count > 0 && first.count < 40, `first batch ${first.count}`);
  assert.equal(first.more_waiting, 40 - first.count);
  let total = first.count;
  for (let i = 0; i < 10 && total < 40; i++) total += (await w.tool("Quiet", "mail_check")).count;
  assert.equal(total, 40);
});

test("doorbell: rings once per window, then a delayed ring for mail that arrived inside it", async () => {
  const w = world();
  await w.sync("desk", { events: [event("Grok", "one")] });
  await w.advance(1);
  assert.equal(w.rings.length, 1);
  assert.equal(w.rings[0].url, "https://bell.example/hook");
  assert.equal(w.rings[0].init.headers.authorization, "Bearer ring-secret");
  assert.deepEqual(JSON.parse(w.rings[0].init.body), { note: "you have mail", waiting: 1 });
  await w.tool("Grok", "mail_check");
  await w.sync("desk", { events: [event("Grok", "two")] });
  await w.advance(DOORBELL.minSeconds - 5);
  assert.equal(w.rings.length, 1, "no second ring inside the window");
  await w.advance(10);
  assert.equal(w.rings.length, 2, "delayed ring once the window has passed");
  await w.sync("desk", { events: [event("Quiet", "no bell configured")] });
  await w.advance(600);
  assert.ok(w.rings.every((r) => r.url === "https://bell.example/hook"));
});

test("doorbell: retries twice for uncollected mail, then stops until the bot collects", async () => {
  const w = world();
  await w.sync("desk", { events: [event("Grok", "one")] });
  await w.advance(1);
  await w.advance(DOORBELL.retrySeconds[0] + 5);
  assert.equal(w.rings.length, 2);
  await w.advance(DOORBELL.retrySeconds[1] + 5);
  assert.equal(w.rings.length, 3);
  await w.sync("desk", { events: [event("Grok", "more mail")] });
  await w.advance(86400);
  assert.equal(w.rings.length, 3, "new mail does not restart ringing");
  const state = (await w.sync("desk")).bots.find((b) => b.name === "Grok");
  assert.match(state.doorbell_error, /rung 3 times without the agent collecting/);
  await w.tool("Grok", "mail_check");
  assert.equal((await w.sync("desk")).bots.find((b) => b.name === "Grok").doorbell_error, "");
  await w.sync("desk", { events: [event("Grok", "after collecting")] });
  await w.advance(1);
  assert.equal(w.rings.length, 4, "collecting re-arms the doorbell");
});

test("doorbell: a failed ring is reported to the machines without the key", async () => {
  const w = world();
  w.bellStatus = 401;
  await w.sync("desk", { events: [event("Grok", "one")] });
  await w.advance(1);
  let state = (await w.sync("desk")).bots.find((b) => b.name === "Grok");
  assert.equal(state.doorbell_error, "the doorbell answered HTTP 401");
  assert.equal(state.doorbell, true);
  w.bellThrows = true;
  await w.advance(DOORBELL.retrySeconds[0] + 5);
  state = (await w.sync("desk")).bots.find((b) => b.name === "Grok");
  assert.match(state.doorbell_error, /could not be reached \(TypeError\)/);
  assert.ok(!JSON.stringify(await w.sync("desk")).includes("ring-secret"));
  assert.equal((await w.sync("desk")).bots.find((b) => b.name === "Quiet").doorbell, false);
});

test("doorbell: a daily limit protects the owner's allowance", async () => {
  const w = world();
  w.box.maxPerDay = 3;
  for (let i = 0; i < 6; i++) {
    await w.sync("desk", { events: [event("Grok", `m${i}`)] });
    await w.advance(DOORBELL.minSeconds + 1);
    await w.tool("Grok", "mail_check");
  }
  assert.equal(w.rings.length, 3);
  await w.sync("desk", { events: [event("Grok", "over the limit")] });
  await w.advance(600);
  assert.equal(w.rings.length, 3);
  assert.match((await w.sync("desk")).bots.find((b) => b.name === "Grok").doorbell_error, /3 times in 24 hours, the daily limit/);
  await w.advance((w.rings[0].at + 86405000 - w.t) / 1000);
  assert.equal(w.rings.length, 4, "rings again once a day has passed since the first");
});

test("a run that finds nothing is told about the batch handed over just before", async () => {
  const w = world();
  await w.sync("desk", { events: [event("Quiet", "a"), event("Quiet", "b")] });
  assert.equal((await w.tool("Quiet", "mail_check")).previous_batch, undefined);
  await w.advance(120);
  const empty = await w.tool("Quiet", "mail_check");
  assert.equal(empty.count, 0);
  assert.equal(empty.previous_batch.count, 2);
  assert.equal(empty.previous_batch.seconds_ago, 120);
  await w.advance(3600);
  assert.equal((await w.tool("Quiet", "mail_check")).previous_batch, undefined);
});

test("retention: old mail and old event ids are removed, counters stay right", async () => {
  const w = world();
  const ev = event("Quiet", "old news");
  await w.sync("desk", { events: [ev, event("Quiet", "also old")] });
  await w.tool("Quiet", "mail_check");
  await w.sync("desk", { events: [event("Quiet", "never collected")] });
  await w.tool("Quiet", "mail_send", { to: "alice@desk", message: "never fetched" });
  await w.advance(RETAIN.deliveredSeconds + 86400);
  assert.equal((await w.tool("Quiet", "mail_check", { reread: 5 })).count, 0, "delivered mail gone after 7 days");
  assert.equal((await w.sync("desk")).bots.find((b) => b.name === "Quiet").waiting, 1);
  await w.advance(RETAIN.waitingSeconds);
  const later = await w.sync("desk");
  assert.equal(later.bots.find((b) => b.name === "Quiet").waiting, 0, "uncollected mail expires");
  assert.equal(later.messages.length, 0);
  assert.notEqual(await w.storage.get(`e:desk:${ev.id}`), undefined, "event ids outlive the mail");
  await w.advance(RETAIN.eventSeconds);
  assert.equal(await w.storage.get(`e:desk:${ev.id}`), undefined, "event ids forgotten");
  assert.equal([...w.storage.map.keys()].filter((k) => /^(pi|x|b|d|p):/.test(k)).length, 0);
});

test("only the most recent delivered messages are kept for rereading", async () => {
  const w = world();
  for (let round = 0; round < 5; round++) {
    const events = Array.from({ length: 50 }, (_, i) => event("Quiet", `m${round}-${i}`));
    await w.sync("desk", { events });
    await w.tool("Quiet", "mail_check");
  }
  assert.equal(await w.storage.get("c:d:Quiet"), LIMITS.deliveredKept);
  const last = await w.tool("Quiet", "mail_check", { reread: 2 });
  assert.deepEqual(last.messages.map((m) => m.message), ["m4-48", "m4-49"]);
  assert.match((await w.tool("Quiet", "mail_check", { reread: 0 })).error, /whole number from 1 to 50/);
});

test("MCP framing", async () => {
  const w = world();
  const call = (body) => w.box.rpc("Grok", body);
  const init = await call({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-11-25" } });
  assert.equal(init.result.protocolVersion, "2025-11-25");
  assert.match(init.result.instructions, /You are Grok/);
  assert.equal((await call({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: 7 } })).result.protocolVersion, "2025-03-26");
  assert.equal(await call({ jsonrpc: "2.0", method: "notifications/initialized" }), null);
  assert.deepEqual((await call({ jsonrpc: "2.0", id: "a", method: "ping" })).result, {});
  const list = await call({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  assert.deepEqual(list.result.tools.map((t) => t.name), ["mail_check", "mail_send", "mail_teammates"]);
  assert.equal((await call({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "rm_rf" } })).error.code, -32602);
  assert.equal((await call({ jsonrpc: "2.0", id: 4, method: "resources/list" })).error.code, -32601);
  assert.equal((await call({ id: 5, method: "ping" })).error.code, -32600);
  assert.equal((await call("nonsense")).error.code, -32600);
  const batch = await call([{ jsonrpc: "2.0", id: 6, method: "ping" }, { jsonrpc: "2.0", method: "notifications/x" }]);
  assert.equal(batch.length, 1);
  const bad = await call({ jsonrpc: "2.0", id: 7, method: "tools/call", params: { name: "mail_send", arguments: "x" } });
  assert.equal(bad.result.isError, true);
});

test("a bot's last contact is reported to the machines", async () => {
  const w = world();
  assert.equal((await w.sync("desk")).bots.find((b) => b.name === "Grok").last_seen, null);
  await w.tool("Grok", "mail_teammates");
  assert.equal((await w.sync("desk")).bots.find((b) => b.name === "Grok").last_seen, w.t / 1000);
  assert.equal(eid().length, 32);
});
