# Cloud mailbox plan (v0.19.0): Grok Bot as a teammate

Status: plan, reviewed before build. Author: Colton Dyck. Date: 2026-11-18.

## 1. Goal

Let a cloud-hosted agent (first target: xAI Grok Bot) exchange direct messages with
teammates on the user's own machines. The cloud agent cannot reach those machines and they
have no public address, so both sides meet at a small hosted **mailbox** that the user
deploys to their own Cloudflare account. Anyone who wants this deploys their own mailbox;
nothing is hosted by the project.

Out of scope for this version: group chats, reactions, deletions, spawn and shutdown for
cloud agents; any cloud product other than Grok Bot being documented (the mailbox itself is
generic MCP and not Grok specific).

## 2. What was proven on a real account (2026-11-18)

- A Grok Bot routine can be started by a webhook (POST, bearer key, JSON body). The call
  returns only a run id. The run starts with no memory of earlier runs.
- The webhook cannot carry content: a top-level field named `context` is removed, and a
  body somewhere between 2 KB and 8 KB is cut short before the run sees it.
- Grok Bot can add a custom MCP server by address, with the key held in a masked
  environment secret. It connected through an `mcp-remote` proxy on its cloud computer.
- A webhook-started run fetched two messages from a test mailbox and replied through it
  with no approval prompt. A 63,883-character message arrived exact.
- There is no official API to read a bot's output, so the bot must push replies out.

## 3. Design

### 3.1 Shape

```
teammate session --> local relay --HTTPS (outbound only)--> mailbox Worker <--MCP-- Grok Bot
                                                                |
                                                                +--webhook "doorbell"--> Grok Bot routine
```

- The relay that already runs on each machine makes **outbound** HTTPS calls to the
  mailbox. Nothing listens on the machine for it.
- The mailbox holds messages in both directions until the other side collects them.
- When the mailbox receives a message for a bot it rings that bot's webhook (the
  doorbell) with a tiny fixed body. The bot's routine then fetches its mail over MCP.

### 3.2 Identity and trust

- Every caller is identified **only by its key**. The mailbox config maps each key to one
  machine name or one bot name. A caller never states its own name.
- Machine keys and bot keys are separate sets. A bot key is refused on the machine
  endpoint and the reverse.
- A cloud mailbox is **less trusted than a peer machine**. It can only carry direct
  messages. On the machine side this is enforced in code, not left to the bot:
  - inbound: only a direct message to an agent that is registered on this machine, at
    normal priority, from a bot name the mailbox reported;
  - outbound: only direct messages;
  - never spawn, shutdown, group, reaction or deletion events in either direction;
  - a mailbox name is never accepted as a peer machine (see 3.5).
- The link is created on the user's request only, the same rule as adding a peer machine.
- Keys are generated on the user's machine and reach the agent session only as a file
  path, never as text in the conversation.

### 3.3 The mailbox Worker (`cloud-mailbox-worker/` in the repo)

Plain JavaScript, no npm dependencies. One Durable Object holds all state through the
key-value storage API, so the logic can be tested under `node --test` with a Map-backed
fake.

Config: one secret `MAILBOX_CONFIG` (JSON):

```json
{"machines": {"desktop": "<key>"},
 "bots": {"CommsTest": {"key": "<key>", "doorbell_url": "https://...", "doorbell_key": "..."}}}
```

Validation at load: names well formed, every key at least 32 characters, all keys
distinct, doorbell address is https. A bad or missing config answers 503 to everything.

Machine endpoint, `POST /pc/sync` with a machine key:

- request: `{v:1, events:[{id,to,from,message,priority,reply_to,msg_id,ts}], ack:[id],
  failed:[{id,error}], roster:[{name,live}]}`
- response: `{v:1, you_are, accepted:[id], rejected:[{id,error}], messages:[{id,from,to,
  message,reply_to,msg_id,ts}], bots:[{name,last_seen,waiting,doorbell,doorbell_error}]}`
- an event id seen before is accepted again without storing twice;
- messages for the machine are returned on every sync until their ids are acked;
- a `failed` entry removes the message and leaves the bot a notice from `comms-relay`.

Bot endpoint, `POST /mcp` with a bot key (JSON-only MCP over POST, as in the test):

- `mail_check(reread?)`: returns waiting messages once, sender shown as `name@machine`;
  `reread: N` re-shows the last N already delivered (kept 7 days, at most 200).
- `mail_send(to, message, reply_to?)`: `to` is `name@machine`, or a bare name when
  exactly one machine's roster has it. Returns the message id.
- `mail_teammates()`: the rosters the machines last uploaded (names and live flags only).

Limits: message at most 65,536 characters; at most 500 waiting messages per recipient; at
most 30 sends per bot per minute; request body caps. Over a limit is a clear error to the
caller, never a silent drop.

Doorbell: rung when mail for a bot is stored, at most once per `DOORBELL_MIN_SECONDS`
(default 60), with a delayed ring by Durable Object alarm if mail arrives inside that
window. If mail is still unfetched it rings again after 5 and 30 minutes, then stops, to
protect the user's allowance. The body is `{"note":"you have mail","waiting":N}`. A failed
ring is recorded and reported to the machines.

Retention: delivered mail 7 days, undelivered mail 14 days, event ids 2 days.

### 3.4 Machine side

New module `mailbox.py` (config file, link and unlink, HTTPS client, conversion between
relay events and mailbox messages). `relay.py` gains a sync step per mailbox in `tick`.

- Config file `relay/mailboxes.json`: `{name: {url, key, you_are}}`. The key is stored
  here in plain text, in the same per-user folder as the messages themselves. It is never
  written to the status file, the log, or any tool output.
- A mailbox is a **virtual machine name**. A bot is addressed `BotName@<mailbox name>`,
  for example `CommsTest@grok`. This reuses the existing outbox files, the seen ring,
  the delivery report, the undelivered notice after a minute, and `teammate_list`.
- `read_peers` keeps meaning real peer machines only. New `read_mailboxes` and
  `read_links` (both together). Each call site is moved to `read_links` deliberately:
  address resolution, queueing, the remote rows in `teammate_list`, starting the relay,
  the relay loop and its status. Anything not moved treats a mailbox as not a peer, so
  the default is refusal.
- Polling: every 10 seconds when idle, at once when something is queued. Failures back off
  up to 30 seconds like a peer.
- The relay runs when a mailbox is linked even with no peer machines and without Tailscale.
- Inbound mail becomes a normal local direct message from `BotName@<mailbox>`. It is
  refused, and the bot told, if the named recipient is not registered here.
- States shown to agents: reachable; not reachable (network); key refused; mailbox error.

Agent surface:

- `teammate_peers(action="link_mailbox", machine, url, key_file)` and
  `action="unlink_mailbox"`. `list` shows mailboxes, their bots, when each bot last
  fetched mail, and any doorbell failure.
- `teammate_send(to="Bot@mailbox")` works; the result says the bot is a cloud agent that
  reads the message when its next run starts.
- Group membership, spawn and shutdown naming a mailbox address are refused with a plain
  reason. A reaction to a bot's message stays local without a warning; deleting a message
  already sent to a mailbox reports that it could not be withdrawn there.
- The inbox shows `(cloud agent)` beside such a sender.
- Terminal equivalents in `python -m teammate_comms.peers`.

### 3.5 Name collisions

A mailbox name must be a valid machine label and must not be this machine, an approved
peer, or a device visible on the tailnet. Approving a peer whose name is a linked mailbox
is refused. The relay's connection check accepts real peers only.

### 3.6 Setup tooling and documentation

- `cloud-mailbox-worker/make_config.py` (stdlib): given machine and bot names, writes a readable
  `config.json`, the `wrangler-secrets.json` to upload, and one key file per machine and
  bot. `--rebuild` regenerates the upload file after the doorbell is pasted in.
- README section "Cloud agents: Grok Bot through a mailbox" with the full walkthrough:
  deploy, link the machine, add the server and secret in Grok Bot, create the routine
  (exact prompt), paste the doorbell, test, revoke, costs and limits.
- Guide section, tool descriptions, SKILL.md, DESIGN.md, CHANGELOG, whats-new.

## 4. Decisions taken without a ruling (user away; lowest-risk option chosen)

1. Cloud agents get direct messages only.
2. Identity comes from the key, with one key per machine and per bot, not one shared key.
3. The Worker rings the doorbell, so webhook keys live in Cloudflare secrets and not on
   the PCs.
4. Each machine links to the mailbox separately; nothing is forwarded between machines.
5. Rosters sent to the cloud contain names and live flags only, no status or project text.
6. Mail from a cloud agent is always normal priority.

## 5. Tests

- `cloud-mailbox-worker/test/*.test.mjs` under `node --test`: auth separation, config
  validation, sync idempotence, ack and redelivery, limits, name resolution, rate limit,
  doorbell debounce and retries with a fake clock and fake fetch, retention, MCP framing.
- `tests/test_mailbox.py`: relay side against an in-process fake mailbox. Link and unlink
  validation, key never exposed, send to bot, rejected event tells the sender, inbound
  delivery and duplicate redelivery, unknown recipient refused, non-message kinds refused
  both ways, control refused, group add refused, network failure and key refused states,
  undelivered notice, mailbox-only relay without Tailscale, name collisions.
- One real run of the Python relay code against the Worker under `wrangler dev`.
- Existing seven suites stay green. Mutation checks on the new guards.
- CI: add the mailbox suite and a Node job for the Worker tests.

## 6. Not verified by this work

The real Grok Bot against the new Worker (needs the user to repoint the bot and paste the
doorbell), allowance cost per run, and whether the bot's proxy survives its computer being
reset.

## 7. Changes after the plan review

The reviewer's findings that changed the design:

- **Linking could have been misused to send a file somewhere.** The key file must now hold
  exactly one key-shaped token, the address must be https to a named host, and the client
  never follows a redirect.
- **Inbound mail bypasses the peer event path entirely.** It has its own delivery function
  with the checks listed in 3.2, and `to` is resolved against this machine's name as the
  mailbox reports it, so it works without Tailscale.
- **Shared helpers take an explicit purpose.** Address resolution accepts a mailbox only for
  send and profile. Group, spawn and shutdown refuse it. The generic relay-out helper never
  queues for a mailbox.
- **Mailbox syncs run on their own thread** so a slow mailbox cannot stall peer machines.
- **A full mailbox defers instead of rejecting**, so the message is kept and retried.
- **Human operators are left out**: not in the roster sent to the mailbox, not reachable
  from a cloud agent.
- **A daily doorbell limit** (100) was added to the once-a-minute limit.
- **Header lookalikes in a cloud message are broken.** (The banner line added here was
  removed in 0.19.4: trust in a cloud agent is the user's choice, stated in their
  standing instructions.)
- **Inbound volume is capped** per mailbox.
- **An empty mail check reports the previous batch**, so a run that died after fetching can
  be recovered by the next one.
- Event ids are remembered 30 days instead of 2.

Left open for the user (section 4 stands otherwise):

- Whether a cloud agent may write to any registered agent session (current behaviour) or
  only to names on a per-link allow list.
- The default daily doorbell limit.

## 8. Changes after the code review

- **Linking moved out of the agent tool.** Section 3.4's `teammate_peers(action=
  "link_mailbox")` no longer exists. The user links in a terminal; the tool prints the
  command. Reason: a prompt-injected local agent could otherwise link or repoint a mailbox.
- Machine keys start `tcm_` and bot keys `tcb_`, so a file holding another secret is never
  accepted as a key file. A linked name cannot be relinked without unlinking.
- Text returned by the mailbox is cleaned and attributed before any agent sees it; answer
  shapes, ids, names and numbers are validated.
- The reply reference is forced onto one line on both sides.
- The Worker's name lookups no longer match names inherited from Object.
- A deferred message no longer blocks mail queued behind it for another agent.
- The inbound cap is per cloud agent, so one cannot starve another.
- The doorbell keeps trying every 6 hours after its retries; a failure notice rings it.
- The key generator validates an edited config before writing anything.

Known and left as is: the mailbox key is stored in plain text in the comms folder; the
Worker has no request rate limit of its own; a mailbox name is not checked against tailnet
devices when Tailscale is not running; one slow mailbox can delay other mailboxes (not peer
machines); a message can be duplicated if the transcript write fails after the inbox write,
as with peer machines today.

## 9. Changes after the final review and surface audit (0.19.1)

- The agent-facing link action described in section 3.4 does not exist; see section 8.
- Inbound cap: a cloud agent over its limit is refused back to the mailbox (which tells it)
  rather than left waiting, because waiting mail blocked the head of the mailbox's queue for
  every other agent. A whole mailbox is also capped, senders are checked against the
  reported agents before anything is tracked, and bot names are length-limited.
- Inbound mail is applied outside the relay lock with a time budget, so a batch cannot stall
  exchanges with peer machines.
- Each cloud agent may hold at most 200 uncollected messages per machine in the Worker.
- Header neutralising now works anywhere in the text and after any kind of line break.
- All mailbox-supplied text is attributed ("the mailbox said: ...").
- Held (deferred) messages are skipped by file name, so any number of them cannot hide
  later mail for another agent.
- A reply marks the message it answers; re-read output shows answered or not.
- Open: `mailbox_full` is a state of the whole mailbox, so one full agent makes sends to
  other agents there show a warning although they are delivered.

## 10. Changes in 0.19.2

Sections 3.2 and 3.3 describe one key per bot. That is superseded: a key belongs to an
account, an account lists its bots, and a bot states its name (`me`) on each call. See
DESIGN.md, "0.19.2". The full-mailbox deferral added in section 7 was removed in favour of
rejection with a reason; limits are 5,000 uncollected per bot and per machine, 2,000 per bot
per machine. Open questions from sections 7 and 9 about the mailbox-wide full state are
closed by that removal.
