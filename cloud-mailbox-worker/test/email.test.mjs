import assert from "node:assert/strict";
import { test } from "node:test";
import {
  EMAIL, EmailConfigError, cleanLine, declaresAutomatic, htmlToText, onList, parseAddress,
  parseEmailConfig, senderIdentity, splitRecipient,
} from "../src/email.js";

const MACHINES = { desk: "k1", lap: "k2" };
const BOTS = { Chief: {}, Sam: {} };
const config = (addresses) => parseEmailConfig({ addresses }, MACHINES, BOTS);
const AUTH = "mx.cloudflare.net; dkim=pass header.d=example.com; dmarc=pass header.from=example.com policy.dmarc=none; spf=pass";
const headers = (from, auth = AUTH, more = []) => [
  ...(auth === null ? [] : [{ key: "authentication-results", value: auth }]),
  ...more,
  ...(Array.isArray(from) ? from : [from]).map((value) => ({ key: "from", value })),
];

test("email config: absent means no addresses", () => {
  assert.deepEqual(Object.keys(parseEmailConfig(undefined, MACHINES, BOTS).addresses), []);
  assert.deepEqual(Object.keys(config({}).addresses), []);
});

test("email config: addresses on different domains, owned by a local agent or a cloud agent", () => {
  const out = config({
    "ada@agents.example.com": {
      owner: "Ada@desk", senders: ["Boss@Example.com", "@client.example", "boss@example.com"],
      attachments_from: ["@client.example"],
    },
    "chief@bots.example.org": { owner: "Chief" },
  });
  assert.deepEqual(out.addresses["ada@agents.example.com"].owner, { kind: "local", agent: "Ada", machine: "desk" });
  assert.deepEqual(out.addresses["ada@agents.example.com"].senders, ["boss@example.com", "@client.example"]);
  assert.deepEqual(out.addresses["ada@agents.example.com"].attachmentsFrom, ["@client.example"]);
  assert.deepEqual(out.addresses["chief@bots.example.org"].owner, { kind: "bot", bot: "Chief" });
  assert.deepEqual(out.addresses["chief@bots.example.org"].senders, []);
  assert.deepEqual(out.warnings, []);
});

test("email config: bad entries are refused", () => {
  const bad = (addresses, why) => assert.throws(() => config(addresses), EmailConfigError, why);
  assert.throws(() => parseEmailConfig([], MACHINES, BOTS), EmailConfigError);
  assert.throws(() => parseEmailConfig({ addresses: [] }, MACHINES, BOTS), EmailConfigError);
  bad({ "Ada@agents.example.com": { owner: "Chief" } }, "upper case");
  bad({ "ada+x@agents.example.com": { owner: "Chief" } }, "plus in a declared address");
  bad({ "ada@localhost": { owner: "Chief" } }, "bare host");
  bad({ "postmaster@agents.example.com": { owner: "Chief" } }, "reserved");
  bad({ "ada@agents.example.com": "Chief" }, "not an object");
  bad({ "ada@agents.example.com": {} }, "no owner");
  bad({ "ada@agents.example.com": { owner: "Nobody" } }, "unknown cloud agent");
  bad({ "ada@agents.example.com": { owner: "Ada@elsewhere" } }, "unknown machine");
  bad({ "ada@agents.example.com": { owner: "constructor" } }, "inherited name");
  bad({ "ada@agents.example.com": { owner: "Chief", senders: "boss@example.com" } }, "list as text");
  bad({ "ada@agents.example.com": { owner: "Chief", senders: ["not an address"] } }, "bad sender");
  bad({ "ada@agents.example.com": { owner: "Chief", senders: ["@agents.example.com"] } }, "own domain");
  bad({
    "ada@agents.example.com": { owner: "Chief", attachments_from: ["x@bots.example.org"] },
    "chief@bots.example.org": { owner: "Chief" },
  }, "another agent domain");
  const many = Object.fromEntries(Array.from({ length: EMAIL.maxAddresses + 1 }, (_, i) =>
    [`a${i}@agents.example.com`, { owner: "Chief" }]));
  bad(many, "too many");
});

test("email config: a public mail domain is allowed with a warning", () => {
  const out = config({ "ada@agents.example.com": { owner: "Chief", senders: ["@gmail.com", "me@gmail.com"] } });
  assert.equal(out.warnings.length, 1);
  assert.match(out.warnings[0], /gmail\.com/);
});

test("recipient: case, plus tags and unknown addresses", () => {
  const { addresses } = config({ "ada@agents.example.com": { owner: "Chief" } });
  assert.deepEqual(splitRecipient("Ada@Agents.Example.com", addresses),
    { address: "ada@agents.example.com", tag: "", shown: "ada@agents.example.com" });
  assert.deepEqual(splitRecipient("ada+Shop1@agents.example.com", addresses),
    { address: "ada@agents.example.com", tag: "shop1", shown: "ada+shop1@agents.example.com" });
  assert.equal(splitRecipient("ada+@agents.example.com", addresses).shown, "ada@agents.example.com");
  assert.equal(splitRecipient("ada+a+b@agents.example.com", addresses), null);
  assert.equal(splitRecipient("ada+a b@agents.example.com", addresses), null);
  assert.equal(splitRecipient(`ada+${"x".repeat(41)}@agents.example.com`, addresses), null);
  assert.equal(splitRecipient("bob@agents.example.com", addresses), null);
  assert.equal(splitRecipient("ada@other.example.com", addresses), null);
  assert.equal(splitRecipient("constructor@agents.example.com", addresses), null);
  assert.equal(splitRecipient("no-at-sign", addresses), null);
  assert.equal(splitRecipient(undefined, addresses), null);
});

test("sender: a verified From is the identity", () => {
  assert.equal(senderIdentity(headers("Boss <Boss@Example.com>"), "boss@example.com"), "boss@example.com");
  assert.equal(senderIdentity(headers("boss@example.com"), "boss@example.com"), "boss@example.com");
  assert.equal(senderIdentity(headers('"Smith, Jo (sales)" <jo@example.com>'), "jo@example.com"), "jo@example.com");
  assert.equal(senderIdentity(headers("=?utf-8?q?J=C3=B3?= <jo@example.com> (work)"), "jo@example.com"), "jo@example.com");
  assert.equal(senderIdentity(headers("jo@example.com", "mx.cloudflare.net 1; dmarc=pass"), "jo@example.com"), "jo@example.com");
});

test("sender: anything short of one verified mailbox gives no identity", () => {
  const none = (hs, parsed, why) => assert.equal(senderIdentity(hs, parsed), "", why);
  none(headers("boss@example.com", null), "boss@example.com", "no results header");
  none(headers("boss@example.com", "mx.cloudflare.net; dmarc=fail; spf=pass"), "boss@example.com", "dmarc fail");
  none(headers("boss@example.com", "mx.cloudflare.net; spf=pass; dkim=pass"), "boss@example.com", "no dmarc result, unnamed signer");
  none(headers("boss@example.com", "mx.cloudflare.net.evil.example; dmarc=pass"), "boss@example.com", "lookalike server");
  none(headers("boss@example.com", "evil.example; x=mx.cloudflare.net; dmarc=pass"), "boss@example.com", "server named later");
  none(headers("boss@example.com", "mx.cloudflare.net; dmarc=pass; dmarc=fail"), "boss@example.com", "two dmarc results");
  none(headers("boss@example.com", "mx.cloudflare.net (dmarc=pass); dmarc=none"), "boss@example.com", "pass only in a comment");
  none(headers("boss@example.com", "mx.cloudflare.net; dmarc=passed"), "boss@example.com", "not the word pass");
  none([{ key: "authentication-results", value: "evil.example; dmarc=pass" },
    { key: "authentication-results", value: AUTH }, { key: "from", value: "boss@example.com" }],
  "boss@example.com", "the topmost header is not Cloudflare's");
  none(headers(["boss@example.com", "evil@evil.example"]), "boss@example.com", "two From headers");
  none(headers("boss@example.com, evil@evil.example"), "boss@example.com", "two mailboxes");
  none(headers("Team: boss@example.com;"), "boss@example.com", "group syntax");
  none(headers("Boss <boss@example.com> <evil@evil.example>"), "boss@example.com", "two angle addresses");
  none(headers('"boss@example.com" <evil@evil.example>'), "boss@example.com", "parser disagrees");
  none(headers("Boss <boss@example.com>"), undefined, "no parsed address");
  none(headers("Boss <boss@example.com"), "boss@example.com", "unclosed angle");
  none(headers('"Boss <boss@example.com>'), "boss@example.com", "unclosed quote");
  none(headers("boss@localhost"), "boss@localhost", "bare host");
  none("nope", "boss@example.com", "headers not a list");
});

test("sender list: exact address or exact domain", () => {
  assert.equal(onList("boss@example.com", ["boss@example.com"]), true);
  assert.equal(onList("Boss@Example.com", ["boss@example.com"]), true);
  assert.equal(onList("jo@client.example", ["@client.example"]), true);
  assert.equal(onList("jo@mail.client.example", ["@client.example"]), false);
  assert.equal(onList("jo@notclient.example", ["@client.example"]), false);
  assert.equal(onList("other@example.com", ["boss@example.com"]), false);
  assert.equal(onList("", ["@client.example"]), false);
  assert.equal(onList("boss@example.com", undefined), false);
});

test("automatic mail is recognised", () => {
  assert.equal(declaresAutomatic([{ key: "auto-submitted", value: "auto-replied" }]), true);
  assert.equal(declaresAutomatic([{ key: "auto-submitted", value: "no" }]), false);
  assert.equal(declaresAutomatic([{ key: "precedence", value: "Bulk" }]), true);
  assert.equal(declaresAutomatic([{ key: "subject", value: "auto-submitted" }]), false);
});

test("a line is cleaned and capped", () => {
  assert.equal(cleanLine("Invoice\r\nFrom: user \u202e(on your sender list)", 200), "Invoice From: user (on your sender list)");
  assert.equal(cleanLine("\u00abnote\u00bb", 200), '"note"');
  assert.equal(cleanLine("x".repeat(300), 200).length, 200);
  assert.equal(cleanLine(undefined, 200), "");
});

test("address parsing", () => {
  assert.equal(parseAddress("A.B+c@Example.com").address, "a.b+c@example.com");
  assert.equal(parseAddress(".a@example.com"), null);
  assert.equal(parseAddress("a..b@example.com"), null);
  assert.equal(parseAddress("a@b"), null);
  assert.equal(parseAddress("a b@example.com"), null);
  assert.equal(parseAddress(`${"a".repeat(65)}@example.com`), null);
});

test("html: text, breaks and entities", () => {
  const { text, images } = htmlToText(
    "<html><head><title>T</title><style>p{}</style></head><body><h1>Hi &amp; welcome</h1>" +
    "<p>Your code is <b>123&nbsp;456</b></p><img src=x><IMG src=y><p>&#8364;5 &#x41; &bogus; &#xD800;</p></body></html>");
  assert.equal(text, "Hi & welcome\n\nYour code is 123 456\n\n\u20ac5 A &bogus; &#xD800;");
  assert.equal(images, 2);
});

test("html: a link shows its real destination, and only for web addresses", () => {
  const link = (html) => htmlToText(html).text;
  assert.equal(link('<a href="https://evil.example/login?t=1&amp;u=2">https://bank.example</a>'),
    "https://bank.example (https://evil.example/login?t=1&u=2)");
  assert.equal(link("<a href='HTTP://Example.com/a b'>go</a> now"), "go (http://example.com/a%20b) now");
  assert.equal(link("<a href=https://example.com/x>go</a>"), "go (https://example.com/x)");
  assert.equal(link('<a href="javascript:alert(1)">click</a>'), "click (link not shown)");
  assert.equal(link('<a href="data:text/html,x">click</a>'), "click (link not shown)");
  assert.equal(link('<a href="http&colon;//evil.example/">click</a>'), "click (link not shown)");
  assert.equal(link('<a href="/relative">rel</a>'), "rel (link not shown)");
  assert.equal(link('<a href="mailto:a@example.com">mail</a>'), "mail");
  assert.equal(link('<a href="#top">up</a> <a name="x">anchor</a> <a href="">empty</a>'), "up anchor empty");
  assert.equal(link('<a title=" href=http://good.example/" href="http://evil.example/">x</a>'),
    "x (http://evil.example/)", "an href inside another attribute's value is not the link");
  assert.equal(link('<a href="http://first.example/" href="http://second.example/">x</a>'),
    "x (http://first.example/)", "the first href wins, as in a browser");
  assert.equal(link('<a/href="http://evil.example/">x</a>'), "x (http://evil.example/)");
  assert.equal(link("<a data-href='http://good.example/' HREF = http://evil.example/ >x</a>"),
    "x (http://evil.example/)");
  assert.equal(link('<a href=http://evil.example/ title="unclosed>x</a>'), "");
  assert.equal(link('<a href="https://a.example/">one<a href="https://b.example/">two'),
    "one (https://a.example/)two (https://b.example/)");
  assert.equal(link(`<a href="https://example.com/${"x".repeat(5000)}">long</a>`).length, "long ()".length + EMAIL.linkChars);
  assert.equal(link('<a title="a > b" href="https://example.com/">t</a>'), "t (https://example.com/)");
});

test("html: scripts, styles and comments are dropped, and an unclosed one drops the rest", () => {
  const t = (html) => htmlToText(html).text;
  assert.equal(t("a<script>var x = '</p>'; evil()</script>b"), "ab");
  assert.equal(t("a<STYLE>x{}</STYLE>b<!-- hidden -->c"), "abc");
  assert.equal(t("a<script>never closed b"), "a");
  assert.equal(t("a<!-- never closed b"), "a");
  assert.equal(t("a<style>never closed"), "a");
  assert.equal(t("a<div title='never closed b"), "a");
  assert.equal(t("a<!DOCTYPE html>b<?xml x?>c"), "abc");
  assert.equal(t("1 < 2 and 3 > 2"), "1 < 2 and 3 > 2");
});

test("html: invisible characters go, input is capped, nothing hangs", () => {
  assert.equal(htmlToText("a\u200bb\u202ec\u0000d").text, "abcd");
  assert.equal(htmlToText("x".repeat(EMAIL.htmlChars + 500)).text.length, EMAIL.htmlChars);
  const started = Date.now();
  htmlToText("<".repeat(EMAIL.htmlChars));
  htmlToText("<a ".repeat(60000));
  htmlToText("&".repeat(EMAIL.htmlChars));
  htmlToText('<a href="'.repeat(20000));
  assert.ok(Date.now() - started < 2000, "pathological input stays fast");
  assert.deepEqual(htmlToText(undefined), { text: "", images: 0 });
});

test("review: letters that change length when lower-cased cannot expose script or style text", () => {
  const dotted = String.fromCodePoint(0x130).repeat(40);
  for (const name of ["script", "style", "SCRIPT", "Title"]) {
    const html = `${dotted}</${name}><${name}>${"x".repeat(40)}>SECRET-TEXT</${name}>after`;
    assert.equal(htmlToText(html).text, `${dotted}after`, name);
  }
});

test("review: only plain ASCII addresses are read", () => {
  const kelvin = String.fromCodePoint(0x212a);
  assert.equal(parseAddress(`a@exampl${kelvin}.com`), null);
  assert.equal(senderIdentity(headers(`a@exampl${kelvin}.com`), `a@exampl${kelvin}.com`), "");
  const { addresses } = config({ "ada@agents.example.com": { owner: "Chief" } });
  assert.equal(splitRecipient(`ada@agents.example.com${kelvin}`, addresses), null);
  assert.throws(() => config({ "ada@agents.example.com": { owner: "Chief", senders: [`@exampl${kelvin}.com`] } }),
    EmailConfigError);
});

test("review: a comment is a space, never glue", () => {
  assert.equal(senderIdentity(headers("a@go(x)od.com"), "a@good.com"), "");
  assert.equal(senderIdentity(headers("boss@example.com", "mx.cloudflare.net; dmarc=p(x)ass"), "boss@example.com"), "");
  assert.equal(senderIdentity(headers("boss@example.com", "mx.cloudflare.net; dmarc = pass"), "boss@example.com"), "");
  assert.equal(senderIdentity(headers("Boss (the real one) <boss@example.com>"), "boss@example.com"), "boss@example.com");
});

test("review: a named From domain in the result must be the From domain", () => {
  const withFrom = (domain) => `mx.cloudflare.net; dmarc=pass header.from=${domain} policy.dmarc=none`;
  assert.equal(senderIdentity(headers("boss@example.com", withFrom("example.com")), "boss@example.com"), "boss@example.com");
  assert.equal(senderIdentity(headers("boss@example.com", withFrom("EXAMPLE.com")), "boss@example.com"), "boss@example.com");
  assert.equal(senderIdentity(headers("boss@example.com", withFrom("other.example")), "boss@example.com"), "");
  assert.equal(senderIdentity(headers("boss@mail.example.com", withFrom("example.com")), "boss@mail.example.com"), "");
});

test("review: names inherited from Object are not addresses", () => {
  assert.throws(() => parseEmailConfig(JSON.parse('{"addresses":{"__proto__":{"owner":"Chief"}}}'), MACHINES, BOTS),
    EmailConfigError);
  const { addresses } = config({ "ada@agents.example.com": { owner: "Chief" } });
  assert.equal(splitRecipient("__proto__@agents.example.com", addresses), null);
  assert.equal(splitRecipient("tostring@agents.example.com", addresses), null);
});

test("review: characters a person cannot see are removed", () => {
  const hidden = [0x034f, 0xfe0f, 0xe0100, 0xe000, 0x0378, 0x200b, 0x202e].map((c) => String.fromCodePoint(c)).join("");
  const filler = [0x3164, 0x115f, 0x2800, 0xffa0].map((c) => String.fromCodePoint(c)).join("");
  assert.equal(cleanLine(`a${hidden}b${filler}c`, 50), "a b c");
  assert.equal(htmlToText(`a${hidden}b`).text, "ab");
  assert.equal(htmlToText(`a${filler}b`).text, "a b");
  assert.equal(htmlToText(`<a href="https://exam${hidden}ple.com/">x</a>`).text, "x (https://example.com/)");
});

test("live format: a signature from the From domain verifies a sender with no DMARC policy", () => {
  const live = (d, from = "sender-domain.example") => "mx.cloudflare.net;\tdkim=pass header.d=" + d + " header.s=google header.b=RbjabfBH;\t" +
    "dmarc=none header.from=" + from + " policy.dmarc=none;\tspf=none (mx.cloudflare.net: no SPF records found for " +
    "postmaster@mail-ua1-x92d.google.com) smtp.helo=mail-ua1-x92d.google.com;\tspf=pass (mx.cloudflare.net: domain of " +
    "me@sender-domain.example designates 2607:f8b0:4864:20::92d as permitted sender) smtp.mailfrom=me@sender-domain.example;\tarc=none";
  const id = (auth, from = "me@sender-domain.example") => senderIdentity([
    { key: "received", value: "x" }, { key: "arc-authentication-results", value: "i=2; mx.cloudflare.net; dmarc=pass" },
    { key: "received-spf", value: "pass" }, { key: "authentication-results", value: auth },
    { key: "authentication-results", value: "mx.cloudflare.net; dmarc=pass" }, { key: "from", value: `Me <${from}>` }], from);
  assert.equal(id(live("sender-domain.example")), "me@sender-domain.example");
  assert.equal(id(live("SENDER-DOMAIN.example")), "me@sender-domain.example");
  assert.equal(id(live("gmail.com")), "", "a signature from another domain");
  assert.equal(id(live("mail.sender-domain.example")), "", "a subdomain's signature");
  assert.equal(id(live("sender-domain.example.evil.example")), "", "a lookalike signer");
  assert.equal(id(live("sender-domain.example"), "me@other.example"), "", "a From on another domain");
  assert.equal(id("mx.cloudflare.net; dkim=fail header.d=sender-domain.example; dmarc=none"), "");
  assert.equal(id("mx.cloudflare.net; dkim=pass header.d=evil.example header.d=sender-domain.example; dmarc=none"), "", "two signer names");
  assert.equal(id("mx.cloudflare.net; dkim=pass (header.d=sender-domain.example) header.d=evil.example; dmarc=none"), "", "a signer only in a comment");
  assert.equal(id("mx.cloudflare.net; dkim=pass header.d=sender-domain.example; dmarc=fail header.from=sender-domain.example"), "me@sender-domain.example",
    "an aligned signature stands even when the policy result is not pass");
  assert.equal(id("mx.cloudflare.net; spf=pass smtp.mailfrom=me@sender-domain.example; dmarc=none"), "", "SPF alone is not enough");
  assert.equal(id("mx.cloudflare.net; dkim=pass header.d=evil.example; dkim=pass header.d=sender-domain.example; dmarc=none"), "me@sender-domain.example");
});
