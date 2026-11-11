# Cross-machine comms — implementation plan (first version)

Status: draft for peer review, 2026-11-10. Branch `wp/cross-machine-relay`.

## 1. Goal and rulings

Agents on separate PCs message each other. Rulings already made by Colton:

- Tailscale is the required network and the only authentication layer. No shared secret.
- The product never dictates Tailscale settings. It detects problems and warns loudly.
- Remote agents are addressed `name@machine`.
- Group chats are in scope, and as many features as feasible work across machines.
- Out of scope: spawning a teammate on another machine, removing a teammate another
  machine owns, avatars, compaction requests (the last two are being retired).

## 2. Architecture

Each machine keeps its own local comms root. Nothing about local delivery, locking, wake or
liveness changes. A standalone **relay** process per machine
(`python -m teammate_comms.relay`) moves **events** between machines.

```
desktop                                   laptop
MCP server (tools)                        MCP server (tools)
  └─ writes event file ─► outbox/laptop/     inbox files ◄─ relay applies event
relay ── HTTP over Tailscale ───────────► relay
      ◄──────────────────────────────────
```

- **Outbound:** a tool call that targets a remote name writes one JSON event file into
  `<root>/TeammateComms/relay/outbox/<machine>/`. The relay delivers files in name order.
- **Inbound:** the relay applies each event by calling the same local cores the tools use
  (`send_dm`-equivalent record delivery, group fan-out, `react`, `delete_message`). The
  recipient's existing watcher wakes it. `channel.py` is untouched.
- **No forwarding.** An event received from the network is never re-sent. The origin
  machine sends to every machine that needs it, so machines sharing a group must be
  directly peered.
- **Team namespaces:** not supported in this version. A remote operation from a
  team-namespaced identity raises a clear error.

### 2.1 Names

Disk is **local-relative**: a bare name is an agent on this machine, `name@machine` is
remote. The wire is **sender-relative**: exactly what the sender has on disk. The receiving
relay localizes every name using the machine it *verified* the connection came from (`M`):

| On the wire | Stored on the receiver |
|---|---|
| `bob` | `bob@M` |
| `alice@<receiver>` | `alice` |
| `carol@other` | `carol@other` |

Trust rule: fields that say *who did it* (`from`, reactor, deleter, leaver, group deleter)
must be bare on the wire, so they always localize to `…@M`. A peer can only ever speak for
names at its own machine.

`machine` is the first DNS label of the Tailscale device name, lowercase,
`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`. Agent names keep `AGENT_NAME_PATTERN`.

New helpers in `comms.py`: `split_address`, `is_remote`, `validate_address`
(local or remote form), `validate_machine_name`.

Every place that derives a file name from a member or participant name must skip remote
names. Known sites: `send_group` fan-out, `delete_message` (`tombstone_in_inbox` for members,
`to`, author), group delete (`remove_group_messages_from_inbox`), `group_read_positions`,
group `create`/`add` member validation and the unknown-member note.

`_MENTION_RE` becomes `(?<![A-Za-z0-9._-])@(name(?:@machine)?)` so `@bob@laptop` is one
mention and the `@laptop` inside `bob@laptop` or an email address is not a mention start.

### 2.2 Tailscale

`relay.py` holds a `TailscaleNet` class that shells out to `tailscale status --json`
(binary found on PATH, then the default Windows and macOS install paths, overridable with
`TEAMMATE_TAILSCALE_BIN`), cached for 15 s:

- `self_info()` → machine name, IPv4 address, backend state.
- `peer_info(machine)` → address, `Online`, `Expired`.
- `identify(ip)` → machine name for a source address, or None.

The relay binds its HTTP listener **only** to its own Tailscale IPv4 address. If Tailscale is
not running it does not listen at all; it stays alive, reports the reason in its status file
and retries. There is no plain-LAN fallback.

The network object is injected through the Python constructor only (`Relay(root, net=...)`),
never through an environment variable or CLI flag, so there is no runtime switch that
bypasses authentication. Tests pass a fake.

Verified on this machine: `tailscale status --json` has `BackendState`, `Self.DNSName`,
`Self.HostName`, `Self.Online`, `Self.TailscaleIPs`, `Peer`. Tailscale is installed here but
logged out (`BackendState: NoState`), so the per-peer fields `Online`, `Expired`,
`KeyExpiry`, `TailscaleIPs` are from memory of the `ipnstate.PeerStatus` struct (`Expired`
and `KeyExpiry` are omitted when unset) and are **not yet confirmed against a live tailnet**.

### 2.3 Approved peers

`<root>/TeammateComms/relay/peers.json`:

```json
{"peers": {"laptop": {}, "studio": {"port": 47821}}}
```

Shown by a new tool `teammate_peers` (`action`: `list` | `remove` | `restart`). `list` shows
each peer's state plus Tailscale devices that could be added. Approval is mutual: each machine
lists the other.

**Revised 2026-11-10 (owner ruling):** only the user may approve a peer. The `add` action was
removed from the tool; the user runs `python -m teammate_comms.peers add <machine>`, which
validates the name, warns if no such Tailscale device is visible, and starts the relay.
`teammate_peers` prints that command in full.

### 2.4 Transport

One endpoint, `POST /v1/exchange`, default port 47821 (`TEAMMATE_RELAY_PORT`).

Request `{v, from, ack: [ids], events: [...], roster: {...}?}` → response
`{applied: [ids], failed: [{id, error}], events: [...]}`.

- The server rejects with 403 unless `identify(source_ip)` is an approved peer and equals
  the claimed `from`.
- **Both directions on one connection.** The response carries the responder's queued events
  for the requester; the requester acknowledges them on its next request. If only one machine
  accepts inbound connections (Windows Firewall blocks the other), both directions still
  flow. Each relay polls each peer every 2 s, 1 s while it has queued events.
- **At-least-once + dedupe.** An event file is deleted only after `applied`/`ack`. The
  receiver keeps a persisted ring of the last 5000 event ids per peer and drops repeats. All
  applies in one relay run under one lock.
- **Poison events.** An event that raises on apply is reported in `failed` and dropped by
  the sender (logged), so one bad event cannot block the queue.
- Limits: 8 MB body, 200 events per request, 5000 queued events per peer (a send beyond
  that raises), message ids must match `^[0-9A-Za-z:.\-]{1,64}$`, bodies pass
  `_clean_message`, every name passes validation.

### 2.5 Events

| Kind | Sent when | Applied as |
|---|---|---|
| `dm` | `send_dm` to `name@machine` | deliver the record (original id) to the local inbox, tee transcript |
| `group_post` | `send_group` when the group has remote members; one event per machine | create the group if missing (from `members`/`creator` in the event), append to the group transcript, fan out to local members |
| `group_members` op `add` | group create / add / join / auto-join on post | create if missing, union the names in |
| `group_members` op `remove` | leave | remove the names (bare-on-wire only) |
| `group_delete` | group delete | delete locally if the localized actor passes the existing creator/orphan rule |
| `reaction` | `react` on a message whose audience includes remote machines | `react` locally as `reactor@M`; author wake works unchanged |
| `deletion` | `delete_message` likewise | `delete_message` locally as `caller@M`, never as operator, so only the author's own messages tombstone |
| `roster` | every 10 s inside the exchange (not queued) | write `relay/roster/<machine>.json` with a local `received_at` |

A message's *audience* is the set of machines in its `from`/`to` (DM) or its group's
members, minus this machine.

Membership ops are add/remove sets, not whole-list overwrites, so concurrent changes on two
machines commute. Remote liveness is computed by the owning machine and shipped as a
boolean; the receiver only judges the age of the snapshot with its own clock, so clock skew
and time zones do not affect liveness.

The cores gain a keyword `_relay=True`; the relay's apply path passes `False` so an applied
event is never re-queued. `send_dm` and `send_group` are split so a pre-built record with an
existing id can be delivered.

### 2.6 Loud failure

The relay keeps `relay/status.json` (`pid`, `version`, `updated_at`, `self`, `tailscale`,
and per peer: `state`, `reason`, `since`, `last_contact`, `queued`).

Peer states: `ok`, `tailscale_down`, `unknown_device`, `key_expired`, `offline`,
`relay_down` (machine online, port not answering — the message names Windows Firewall and
the exact allow rule), `not_approved` (the other side returned 403).

Surfaces:

1. **Send result.** `teammate_send` to a remote name states the peer state and, when not
   `ok`, a `WARNING` with the reason, how long, and how many messages are waiting. It also
   warns if the relay process is not running.
2. **`teammate_list`.** A "Remote teammates" section per approved peer with the same state
   line, then that machine's agents as `name@machine` with live/offline.
3. **A wake to the sender.** If an event is still undelivered after 60 s, the relay DMs each
   affected local sender once per outage from the reserved name `comms-relay`, and once more
   when the peer returns. `comms-relay` is reserved at registration like `compact-broker`.
4. **`teammate_peers list`** and the relay log (`relay/relay.log`).

### 2.7 Lifecycle

`ensure_relay(root)` starts a detached relay when peers are configured and none is alive
(pid in `status.json` checked with `_pid_alive`; the listening port is the second guard).
Called on register, on any remote send, and when the user approves a peer. The relay exits when
the peers list is empty or a stop file names its pid. If the running relay's version differs
from the caller's, `ensure_relay` requests a stop and starts the new one (ledger 19).

## 3. Not in this version

- Read receipts for remote group members.
- Project profile sync.
- Dashboard roster/presence for remote teammates (messages still appear, and the human can
  already send to `name@machine` through the existing cores).
- Group history backfill for a machine that joins a group late.
- Team namespaces.
- Changing message ids or timestamps. Ids stay local-time based; cross-machine *ordering*
  by id needs matching time zones and synced clocks. Delivery and wake do not depend on it.

## 4. Files

- `src/teammate_comms/relay.py` — new: Tailscale probe, peers/status/outbox helpers, HTTP
  server, exchange loop, event apply, notices, `main()`.
- `src/teammate_comms/comms.py` — address helpers, relay dirs, `RELAY_SENDER`.
- `src/teammate_comms/tools.py` — remote branches in the cores, mention regex,
  `teammate_peers`, remote section in `teammate_list`, `teammate_profile name@machine`.
- `src/teammate_comms/server.py` — reserve `comms-relay`, call `ensure_relay` on register.
- `tests/test_relay.py` — new.
- `README.md`, `skills-src/teammate-comms/SKILL.md` (re-rendered), `DESIGN.md`,
  `CHANGELOG.md`.

## 5. Acceptance criteria

Tests use two relays in one process on loopback with a fake network object and two
temporary roots.

1. A DM to `bob@b` lands in bob's unread file on B with `from: alice@a` and the original id.
2. A reply `alice@a` from B lands on A.
3. A send to an unapproved machine raises and queues nothing.
4. A request from an unapproved source, or with a `from` that does not match the source,
   gets 403 and applies nothing.
5. An event whose actor field is qualified on the wire is rejected.
6. Group: create on A with `bob@b`; the group exists on B with localized members; a post on
   A reaches bob; a post on B reaches alice; ids match on both sides.
7. Leave on B removes bob on A. Group delete by the creator removes it on both; by a
   non-creator it is refused remotely.
8. `@bob@b` in a group post sets `mentions` that localize to `bob` on B; `x@b` without a
   leading `@` is not a mention.
9. A reaction on B to alice's message appears in A's reactions with `target_from: alice`.
10. Deleting alice's own message on A tombstones it on B; a deletion event for a message
    authored elsewhere is refused on the receiver.
11. With B stopped, events stay queued on A; after B starts they deliver once, in order.
    A duplicate delivery applies once.
12. With only one side able to connect, both directions deliver.
13. After 60 s undelivered (time injected), the sender gets exactly one `comms-relay` notice,
    and one recovery notice.
14. No file whose name contains `@` exists under `inboxes/` after the suite.
15. Existing suites still pass.

## 6. Risks to review

- Windows Firewall blocking inbound on both machines (then nothing flows; the state is
  `relay_down` with the fix named).
- A detached relay being killed with the Claude Code session's job object.
- Peer status fields unverified against a live tailnet.
- Any missed site that turns a `name@machine` into a file name.
- An agent can call `teammate_peers add`, which opens comms to another device on the
  tailnet (including a device shared in from another user). *(Closed by the 2026-11-10 ruling
  above, as far as a tool can: agents with shell access could still run the user command.)*

## 7. Plan review outcome (2026-11-10)

A sonnet reviewer checked this plan against the code before the build. Changes it forced:

- **Group access is per machine.** A peer may post to or add members to an existing local
  group only if its machine already has a member in it, and may create a group only if the
  group has a member on the receiving machine. (As drafted, a peer that guessed a local
  group's name could join it.)
- **Message ids are checked.** An incoming id must look like a locally minted id, and an id
  already owned by a different author is rejected. The same check makes re-applying a
  message a no-op. (As drafted, a peer could reuse a victim's id and then delete "its own"
  message.) The loose id pattern in 2.4 is replaced by this.
- **Retry versus drop.** Only invalid or unauthorized events are dropped and reported.
  Lock timeouts and I/O errors stay queued and are retried in order.
- **Exchanges are bounded in bytes** (4 MB of events per direction), not only in count.
- **Tailscale identity is stricter.** Devices shared in from another tailnet are never
  identified, and two devices with the same first DNS label are both ignored.
- **A relay singleton lock** replaces the port as the guard against two relays per root.
- **Mentions:** trailing punctuation is stripped and the machine part is case-insensitive.
- **Removing a teammate** now also sends a membership removal to the other machines.
- **A rejected event notifies its sender** through `comms-relay`.

Tailscale fields were confirmed against the `ipnstate` source by the reviewer: `Online`,
`TailscaleIPs`, `HostName`, `DNSName` are always present; `Expired`, `KeyExpiry` and
`ShareeNode` are omitted when unset; `Online` means "connected to the control plane", so it
is used only to word a failure, never as proof the relay is up. They are still unconfirmed
against a live tailnet on this machine, where Tailscale is installed but logged out.

Accepted as documented limits rather than fixed: cross-machine reactions and deletes need
the transcript enabled; operator deletes of others' messages stay local; a peer machine can
create groups and fill disk within the per-request limits; `teammate_peers add` is callable
by an agent *(no longer true: removed by the 2026-11-10 ruling in 2.3)*.
