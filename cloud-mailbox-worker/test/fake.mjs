import { Mailbox, parseConfig } from "../src/mailbox.js";

export class FakeStorage {
  constructor() {
    this.map = new Map();
  }

  async get(key) {
    const value = this.map.get(key);
    return value === undefined ? undefined : structuredClone(value);
  }

  async put(key, value) {
    this.map.set(key, structuredClone(value));
  }

  async delete(key) {
    return this.map.delete(key);
  }

  async list({ prefix = "", limit = Infinity, reverse = false } = {}) {
    let keys = [...this.map.keys()].filter((k) => k.startsWith(prefix)).sort();
    if (reverse) keys.reverse();
    keys = keys.slice(0, limit);
    return new Map(keys.map((k) => [k, structuredClone(this.map.get(k))]));
  }
}

export const KEYS = {
  desk: "tcm_desk-key-00000000000000000000000000000",
  lap: "tcm_lap-key-000000000000000000000000000000",
  bot: "tcb_bot-key-000000000000000000000000000000",
  bot2: "tcb_bot2-key-00000000000000000000000000000",
};

export function baseConfig(extra = {}) {
  return {
    machines: { desk: KEYS.desk, lap: KEYS.lap },
    bots: {
      Grok: { key: KEYS.bot, doorbell_url: "https://bell.example/hook", doorbell_key: "ring-secret" },
      Quiet: { key: KEYS.bot2 },
    },
    ...extra,
  };
}

export function world(config = baseConfig()) {
  const w = {
    t: 1800000000000,
    storage: new FakeStorage(),
    rings: [],
    alarmAt: null,
    bellStatus: 200,
    bellThrows: false,
  };
  w.box = new Mailbox({
    storage: w.storage,
    config: parseConfig(config),
    now: () => w.t,
    fetch: async (url, init) => {
      w.rings.push({ url, init, at: w.t });
      if (w.bellThrows) throw Object.assign(new Error("down"), { name: "TypeError" });
      return { ok: w.bellStatus >= 200 && w.bellStatus < 300, status: w.bellStatus };
    },
    setAlarm: async (ms) => {
      w.alarmAt = ms;
    },
  });
  w.advance = async (seconds) => {
    const end = w.t + seconds * 1000;
    while (w.alarmAt !== null && w.alarmAt <= end) {
      w.t = Math.max(w.t, w.alarmAt);
      w.alarmAt = null;
      await w.box.alarm();
    }
    w.t = end;
  };
  w.sync = async (machine, body = {}) => (await w.box.sync(machine, { v: 1, ...body })).body;
  w.tool = async (bot, name, args = {}) => {
    const account = w.box.config.bots[bot].account;
    const reply = await w.box.rpc(account, { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: { me: bot, ...args } } });
    const text = reply.result.content[0].text;
    return reply.result.isError ? { error: text } : JSON.parse(text);
  };
  return w;
}

let counter = 0;
export function eid() {
  counter += 1;
  return counter.toString(16).padStart(32, "0");
}

export function event(to, message, extra = {}) {
  return { id: eid(), to, from: "alice", message, priority: "normal", ts: 1800000000, ...extra };
}
