// Pure checks and conversions for agent email. No storage, no clock, no network.

export const EMAIL = {
  maxAddresses: 50,
  maxListEntries: 100,
  tagChars: 40,
  subjectChars: 200,
  htmlChars: 200000,
  linkChars: 2000,
  authServer: "mx.cloudflare.net",
};

export class EmailConfigError extends Error {}

const LOCAL_RE = /^[a-z0-9]([a-z0-9.-]{0,38}[a-z0-9])?$/;
const DOMAIN_RE = /^(?=.{4,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;
const TAG_RE = /^[a-z0-9._-]{0,40}$/;
const SENDER_LOCAL_RE = /^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$/;
const AGENT_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/;
const RESERVED_LOCALS = new Set([
  "postmaster", "abuse", "admin", "root", "hostmaster", "webmaster", "noreply", "no-reply",
  "mailer-daemon", "security",
]);
const PUBLIC_MAIL_DOMAINS = new Set([
  "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
  "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "gmx.com",
]);

const ASCII_RE = /^[\x21-\x7e]+$/;
const HIDDEN_RE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\u034f\ufe00-\ufe0f\u{e0100}-\u{e01ef}]/gu;
const FILLER_RE = /[\u115f\u1160\u2800\u3164\uffa0]/g;

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function parseAddress(value) {
  if (typeof value !== "string" || value.length > 254 || !ASCII_RE.test(value)) return null;
  const text = value.toLowerCase();
  const at = text.lastIndexOf("@");
  if (at < 1) return null;
  const local = text.slice(0, at);
  const domain = text.slice(at + 1);
  if (!SENDER_LOCAL_RE.test(local) || !DOMAIN_RE.test(domain)) return null;
  if (local.startsWith(".") || local.endsWith(".") || local.includes("..")) return null;
  return { address: `${local}@${domain}`, local, domain };
}

function parseList(raw, label, agentDomains, warnings) {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) throw new EmailConfigError(`${label} must be a list`);
  if (raw.length > EMAIL.maxListEntries) {
    throw new EmailConfigError(`${label} has more than ${EMAIL.maxListEntries} entries`);
  }
  const out = [];
  for (const item of raw) {
    const text = typeof item === "string" && ASCII_RE.test(item.trim()) ? item.trim().toLowerCase() : "";
    const whole = text.startsWith("@");
    const domain = whole ? text.slice(1) : parseAddress(text)?.domain;
    if (!domain || !DOMAIN_RE.test(domain)) {
      throw new EmailConfigError(`${label}: ${JSON.stringify(item)} is not an address or @domain`);
    }
    if (agentDomains.has(domain)) {
      throw new EmailConfigError(`${label}: ${domain} carries agent addresses and cannot be listed`);
    }
    if (whole && PUBLIC_MAIL_DOMAINS.has(domain)) {
      warnings.push(`${label}: @${domain} lists everyone with an address at a public mail service`);
    }
    if (!out.includes(text)) out.push(text);
  }
  return out;
}

export function parseEmailConfig(raw, machines, bots) {
  const none = { addresses: Object.create(null), warnings: [] };
  if (raw === undefined || raw === null) return none;
  if (!isObject(raw) || (raw.addresses !== undefined && !isObject(raw.addresses))) {
    throw new EmailConfigError("email must be an object with addresses");
  }
  const entries = Object.entries(raw.addresses || {});
  if (entries.length > EMAIL.maxAddresses) {
    throw new EmailConfigError(`email has more than ${EMAIL.maxAddresses} addresses`);
  }
  const agentDomains = new Set();
  const parsed = [];
  for (const [key, entry] of entries) {
    const at = key.lastIndexOf("@");
    const local = key.slice(0, at);
    const domain = key.slice(at + 1);
    if (at < 1 || !LOCAL_RE.test(local) || local.includes("..") || !DOMAIN_RE.test(domain)) {
      throw new EmailConfigError(`email address ${JSON.stringify(key)} is not valid (lower case only)`);
    }
    if (RESERVED_LOCALS.has(local)) throw new EmailConfigError(`email address ${key}: ${local} is reserved`);
    if (!isObject(entry)) throw new EmailConfigError(`email address ${key}: must be an object`);
    agentDomains.add(domain);
    parsed.push([key, entry]);
  }
  const addresses = Object.create(null);
  const warnings = [];
  for (const [key, entry] of parsed) {
    addresses[key] = {
      owner: parseOwner(entry.owner, key, machines, bots),
      senders: parseList(entry.senders, `email address ${key} senders`, agentDomains, warnings),
      attachmentsFrom: parseList(entry.attachments_from, `email address ${key} attachments_from`,
        agentDomains, warnings),
    };
  }
  return { addresses, warnings };
}

function parseOwner(raw, key, machines, bots) {
  if (typeof raw !== "string" || !raw) throw new EmailConfigError(`email address ${key}: owner is required`);
  const at = raw.indexOf("@");
  if (at < 0) {
    if (!Object.hasOwn(bots, raw)) {
      throw new EmailConfigError(`email address ${key}: owner ${raw} is not a cloud agent of this mailbox`);
    }
    return { kind: "bot", bot: raw };
  }
  const agent = raw.slice(0, at);
  const machine = raw.slice(at + 1);
  if (!AGENT_RE.test(agent) || !Object.hasOwn(machines, machine)) {
    throw new EmailConfigError(`email address ${key}: owner ${raw} must be Name@machine for a machine of this mailbox`);
  }
  return { kind: "local", agent, machine };
}

// The declared address a recipient stands for, and its plus tag; null when refused.
export function splitRecipient(to, addresses) {
  if (typeof to !== "string" || to.length > 254 || !ASCII_RE.test(to.trim())) return null;
  const text = to.trim().toLowerCase();
  const at = text.lastIndexOf("@");
  if (at < 1) return null;
  const local = text.slice(0, at);
  const plus = local.indexOf("+");
  const base = plus < 0 ? local : local.slice(0, plus);
  const tag = plus < 0 ? "" : local.slice(plus + 1);
  const address = `${base}@${text.slice(at + 1)}`;
  if (!Object.hasOwn(addresses, address) || !TAG_RE.test(tag)) return null;
  return { address, tag, shown: tag ? `${base}+${tag}@${text.slice(at + 1)}` : address };
}

function withoutCommentsAndQuotes(value) {
  let out = "";
  let depth = 0;
  let quoted = false;
  for (let i = 0; i < value.length; i++) {
    const ch = value[i];
    if (ch === "\\" && (quoted || depth)) {
      i++;
    } else if (quoted) {
      if (ch === '"') quoted = false;
    } else if (ch === '"' && !depth) {
      quoted = true;
    } else if (ch === "(") {
      depth++;
    } else if (ch === ")" && depth) {
      depth--;
      if (!depth) out += " ";
    } else if (!depth) {
      out += ch;
    }
  }
  return quoted || depth ? null : out;
}

function singleMailbox(value) {
  const bare = withoutCommentsAndQuotes(value);
  if (bare === null || /[,;:]/.test(bare)) return null;
  const angle = /^[^<>]*<([^<>]*)>\s*$/.exec(bare);
  if (!angle && /[<>]/.test(bare)) return null;
  return parseAddress((angle ? angle[1] : bare).trim());
}

function dmarcPassed(value, fromDomain) {
  const bare = withoutCommentsAndQuotes(value);
  if (bare === null) return false;
  const parts = bare.split(";").map((part) => part.trim());
  if (!new RegExp(`^${EMAIL.authServer.replace(/\./g, "\\.")}(\\s+\\d+)?$`).test(parts[0])) return false;
  const results = parts.slice(1).filter((part) => /^dmarc\s*=/i.test(part));
  if (results.length > 1) return false;
  if (results.length === 1 && /^dmarc=pass(\s|$)/i.test(results[0])) {
    const named = /\sheader\.from=(\S+)/i.exec(results[0]);
    return !named || named[1].toLowerCase() === fromDomain;
  }
  return parts.slice(1).some((part) => {
    if (!/^dkim=pass(\s|$)/i.test(part)) return false;
    const signer = part.match(/\sheader\.d=(\S+)/gi) || [];
    return signer.length === 1 && signer[0].trim().slice(9).toLowerCase() === fromDomain;
  });
}

// headers: [{key, value}] in the order they appear in the message, keys lower case.
// Returns the verified sender address, or "" when there is none.
export function senderIdentity(headers, parsedFrom) {
  if (!Array.isArray(headers)) return "";
  const from = headers.filter((h) => h && h.key === "from");
  if (from.length !== 1 || typeof from[0].value !== "string") return "";
  const mailbox = singleMailbox(from[0].value);
  if (!mailbox) return "";
  const auth = headers.find((h) => h && h.key === "authentication-results");
  if (!auth || typeof auth.value !== "string" || !dmarcPassed(auth.value, mailbox.domain)) return "";
  if (typeof parsedFrom !== "string" || parsedFrom.toLowerCase() !== mailbox.address) return "";
  return mailbox.address;
}

export function onList(identity, list) {
  const parsed = parseAddress(identity);
  if (!parsed || !Array.isArray(list)) return false;
  return list.includes(parsed.address) || list.includes(`@${parsed.domain}`);
}

export function declaresAutomatic(headers) {
  if (!Array.isArray(headers)) return false;
  return headers.some((h) => h && typeof h.value === "string" && (
    (h.key === "auto-submitted" && h.value.trim().toLowerCase() !== "no") ||
    (h.key === "precedence" && /^(bulk|junk|list)$/i.test(h.value.trim()))));
}

export function cleanLine(value, max) {
  if (typeof value !== "string") return "";
  const text = value.replace(HIDDEN_RE, " ").replace(FILLER_RE, " ").replace(/[\u00ab\u00bb]/g, '"')
    .replace(/\s+/g, " ").trim();
  return Array.from(text).slice(0, max).join("");
}

const NAMED_ENTITIES = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", copy: "(c)", reg: "(R)",
  ndash: "-", mdash: "-", hellip: "...", lsquo: "'", rsquo: "'", ldquo: '"', rdquo: '"',
  bull: "*", middot: "*", trade: "(TM)", euro: "EUR", pound: "GBP",
};

function decodeEntities(text) {
  return text.replace(/&(#x[0-9a-f]{1,6}|#[0-9]{1,7}|[a-z]{2,8});/gi, (whole, body) => {
    if (body[0] !== "#") {
      const named = NAMED_ENTITIES[body.toLowerCase()];
      return named === undefined ? whole : named;
    }
    const code = /^#x/i.test(body) ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
    if (!(code > 0 && code <= 0x10ffff) || (code >= 0xd800 && code <= 0xdfff)) return whole;
    return String.fromCodePoint(code);
  });
}

const SKIPPED_ELEMENTS = new Set(["script", "style", "head", "title", "template", "svg", "noscript"]);
const BREAKING_ELEMENTS = new Set([
  "p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6",
  "section", "article", "header", "footer", "blockquote", "hr", "pre",
]);

function tagEnd(src, from) {
  let quote = "";
  for (let i = from; i < src.length; i++) {
    const ch = src[i];
    if (quote) {
      if (ch === quote) quote = "";
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === ">") {
      return i;
    }
  }
  return -1;
}

// The value of the first attribute called `name` in the text after a tag's name, or null.
function attribute(rest, name) {
  const space = (ch) => ch === " " || ch === "\t" || ch === "\n" || ch === "\r" || ch === "\f" || ch === "/";
  let i = 0;
  while (i < rest.length) {
    while (i < rest.length && space(rest[i])) i++;
    const start = i;
    while (i < rest.length && !space(rest[i]) && rest[i] !== "=") i++;
    const key = rest.slice(start, i).toLowerCase();
    while (i < rest.length && space(rest[i]) && rest[i] !== "/") i++;
    let value = "";
    if (rest[i] === "=") {
      i++;
      while (i < rest.length && space(rest[i]) && rest[i] !== "/") i++;
      const quote = rest[i] === '"' || rest[i] === "'" ? rest[i] : "";
      if (quote) {
        const close = rest.indexOf(quote, i + 1);
        value = rest.slice(i + 1, close < 0 ? rest.length : close);
        i = close < 0 ? rest.length : close + 1;
      } else {
        const from = i;
        while (i < rest.length && !/\s/.test(rest[i])) i++;
        value = rest.slice(from, i);
      }
    } else if (i === start) {
      i++;
    }
    if (key === name) return value;
  }
  return null;
}

// "" for no link, a web address to show, or LINK_HIDDEN for one that cannot be shown.
function linkTarget(rest) {
  const href = attribute(rest, "href");
  if (href === null) return "";
  const raw = decodeEntities(href).replace(HIDDEN_RE, "").trim();
  if (!raw || raw.startsWith("#") || /^mailto:/i.test(raw)) return "";
  let url;
  try {
    url = new URL(raw);
  } catch {
    return LINK_HIDDEN;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return LINK_HIDDEN;
  return url.href.slice(0, EMAIL.linkChars);
}

const LINK_HIDDEN = "link not shown";

export function htmlToText(html) {
  if (typeof html !== "string") return { text: "", images: 0 };
  const src = html.slice(0, EMAIL.htmlChars);
  const out = [];
  let images = 0;
  let target = "";
  let i = 0;
  while (i < src.length) {
    const open = src.indexOf("<", i);
    if (open < 0) {
      out.push(decodeEntities(src.slice(i)));
      break;
    }
    out.push(decodeEntities(src.slice(i, open)));
    if (src.startsWith("<!--", open)) {
      const close = src.indexOf("-->", open + 4);
      if (close < 0) break;
      i = close + 3;
      continue;
    }
    const named = /^<(\/?)([a-zA-Z][a-zA-Z0-9]*)/.exec(src.slice(open, open + 34));
    if (!named) {
      if (src[open + 1] === "!" || src[open + 1] === "?") {
        const close = src.indexOf(">", open);
        if (close < 0) break;
        i = close + 1;
      } else {
        out.push("<");
        i = open + 1;
      }
      continue;
    }
    const end = tagEnd(src, open + named[0].length);
    if (end < 0) break;
    const name = named[2].toLowerCase();
    const closing = named[1] === "/";
    i = end + 1;
    if (!closing && SKIPPED_ELEMENTS.has(name)) {
      const closer = new RegExp(`</${name}`, "gi");
      closer.lastIndex = i;
      const found = closer.exec(src);
      if (!found) break;
      const close = found.index;
      const after = src.indexOf(">", close);
      if (after < 0) break;
      i = after + 1;
    } else if (name === "img" && !closing) {
      images++;
    } else if (name === "a") {
      if (target) out.push(` (${target})`);
      target = closing ? "" : linkTarget(src.slice(open + named[0].length, end));
    } else if (BREAKING_ELEMENTS.has(name)) {
      out.push("\n");
    } else if (name === "td" || name === "th") {
      out.push(" ");
    }
  }
  if (target) out.push(` (${target})`);
  const text = out.join("")
    .replace(/\r\n?/g, "\n")
    .replace(/[\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\u034f\ufe00-\ufe0f\u{e0100}-\u{e01ef}]|[^\P{Cc}\n\t]/gu, "")
    .replace(FILLER_RE, " ")
    .replace(/[^\S\n]+/g, " ")
    .split("\n").map((line) => line.trim()).join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return { text, images };
}
