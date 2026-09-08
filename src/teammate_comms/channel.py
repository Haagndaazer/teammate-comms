"""Channel wake mechanics: heartbeat + inbox watcher + idle push.

The single watcher thread starts with the server but stays dormant until two
gates open: ``notifications/initialized`` (the client is ready) AND registration
(``teammate_register`` has set this instance's identity). Once both are open it
polls the agent's own ``_unread.json`` and emits a ``notifications/claude/channel``
event for messages the agent **has not yet been shown** — so a peer's
``teammate_send`` writing this inbox *is* the nudge.

Nudge gating (WP-42: auto-ack on read simplified this — ``_unread.json`` now literally
means "never shown", since ``teammate_inbox`` moves whatever it shows straight into
``_read.json``): a message wakes the agent only if its id is both still in unread and
not already nudged-for (a watcher-local ``known_ids`` set). At registration
``known_ids`` is seeded to whatever is already in the inbox, so pre-existing messages
don't nudge (the agent drains them with a startup ``teammate_inbox``). The emitted
count is the number of unread-and-unmuted messages. Reading a message removes it from
unread entirely, which is what silences a nudge for it. This is missed-nudge-safe: a
genuinely new message has a fresh id outside ``known_ids``, so it always nudges.

Reliability contract: the inbox JSON is the source of truth, but a dropped channel push
is NOT auto-recovered — an idle agent never reads its inbox unprompted, so the old
"recovered on the next inbox read" assumption does not hold for one. Claude Code drops
channel notifications (GH #38736 — mid-turn notifications dropped, not queued, despite the
docs; GH #61797 — sporadic silent drops at idle; both unresolved), so the watcher
RE-NUDGES still-unseen unread with capped exponential backoff (see ``compute_reemit`` +
``run_watcher``) to compensate, while preserving the v0.4.2 no-noise gating above.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

from . import harness as harness_mod
from .comms import (
    HEARTBEAT_STALENESS_SECONDS,
    REACTION_EMOJI,
    file_lock,
    get_inboxes_dir,
    heartbeat_fresh,
    now_timestamp,
    read_agent_record,
    read_json_readonly,
    read_json_safe,
    read_reactions,
    write_agent_record,
    write_json_atomic,
)

HEARTBEAT_SECONDS = 5
POLL_SECONDS = 0.5

# Re-nudge (WP-9): a dropped channel push leaves an idle agent unaware of its unread.
# Re-emit the wake for still-UNSEEN unread after an exponential-backoff quiet period
# (REEMIT_BASE_SECONDS × 2**attempt: 120, 240, 480 s), capped at REEMIT_MAX_ATTEMPTS.
REEMIT_BASE_SECONDS = 120
REEMIT_MAX_ATTEMPTS = 3


def _log_emit(kind, unseen, attempt):
    """One stable, greppable stderr line per ACTUAL wake emit — evidence-grade for an
    upstream Claude Code bug report (lets us tell server-emitted from client-dropped).
    stderr ONLY: stdout is the JSON-RPC stream and the harness asserts its purity."""
    print(f"[teammate-comms] wake-emit kind={kind} unseen={unseen} attempt={attempt}",
          file=sys.stderr, flush=True)


def build_wake_payload(agent, count, groups=None, mentioned=False, senders=None):
    """Pure: the signal-only wake text + meta for ``count`` unread (senders for DMs,
    groups for group posts, a 🔔 note on @mention, the group reply target when any)."""
    mention_note = "🔔 @mention. " if mentioned else ""
    sources = sorted(set(senders or [])) + sorted(groups or [])
    from_part = f" from {', '.join(sources)}" if sources else ""
    if groups:
        targets = " or ".join(f"to:'{g}'" for g in sorted(groups))
        group_line = f" Reply to {targets} not the sender."
    else:
        group_line = ""
    content = f"{mention_note}📬 {count} new message(s){from_part}.{group_line}"
    return content, {"count": str(count), "agent": agent}


def build_reaction_payload(agent, reactions):
    """Pure: wake text + meta for the AUTHOR of reacted-to messages (``meta.kind``
    separates it from message wakes)."""
    parts, seen = [], set()
    for r in reactions[-6:]:
        who, em = r.get("from"), r.get("emoji")
        if (who, em) in seen:
            continue
        seen.add((who, em))
        parts.append(f"{who} {REACTION_EMOJI.get(em, em)}")
    content = f"💬 {', '.join(parts)} reacted to your message(s)."
    return content, {"count": str(len(reactions)), "agent": agent, "kind": "reaction"}


class ChannelWaker:
    """Claude Code wake: a synchronous ``notifications/claude/channel`` push."""

    name = "channel"

    def __init__(self, send_message):
        self._send = send_message

    def wake(self, content, meta):
        self._send({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {"content": content, "meta": meta},
        })
        return True


CODEX_QUEUE_TIMEOUT_SECONDS = 20


class CodexQueueWaker:
    """Codex wake: queue the wake text as a user turn on our own thread via ``codex queue``,
    on a daemon thread so the watcher loop never blocks. At most one in flight; a wake
    requested while busy is reported dropped so the caller retries next tick."""

    name = "codex-queue"

    def __init__(self, session, runner=None, timeout=CODEX_QUEUE_TIMEOUT_SECONDS):
        self._session = session
        self._runner = runner or subprocess.run
        self._timeout = timeout
        self._lock = threading.Lock()
        self._thread = None
        self._busy_logged = False

    def argv(self, content):
        return ["codex", "queue", "--thread", self._session, "--message", content]

    def _run(self, argv):
        outcome = "rc=?"
        try:
            result = self._runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=self._timeout)
            outcome = f"rc={getattr(result, 'returncode', 0)}"
        except subprocess.TimeoutExpired:
            outcome = "timeout"
        except OSError as exc:
            outcome = f"error={exc}"
        print(f"[teammate-comms] wake-emit harness=codex {outcome}", file=sys.stderr, flush=True)

    def wake(self, content, meta):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if not self._busy_logged:
                    self._busy_logged = True
                    print("[teammate-comms] wake-emit harness=codex dropped-busy (retrying next tick)",
                          file=sys.stderr, flush=True)
                return False
            self._busy_logged = False
            self._thread = threading.Thread(target=self._run, args=(self.argv(content),),
                                            daemon=True)
            self._thread.start()
            return True


WAKERS = {ChannelWaker.name: ChannelWaker, CodexQueueWaker.name: CodexQueueWaker}


def make_waker(harness, session, send_message, runner=None):
    """Build the waker for ``harness``; None when it needs a session id and has none."""
    if harness.wake == ChannelWaker.name:
        return ChannelWaker(send_message)
    if harness.needs_session and not session:
        return None
    cls = WAKERS.get(harness.wake)
    if cls is None:
        return None
    return cls(session, runner=runner)


def compute_reaction_wakes(reactions, known_ids, agent):
    """Pure reaction-wake decision for one heartbeat tick (no I/O — hermetically testable).

    ``reactions`` is the batch read this tick (driven by a high-water cursor, so a burst
    larger than the read limit pages forward across ticks instead of scrolling past a
    fixed tail — the missed-wake hole the message-wake path was hardened against, audit
    A-2). ``known_ids`` is the PREVIOUS tick's returned id set, or None on the first
    (seed) read. Returns ``(fresh_rx, new_known_ids, new_cursor)``:

      * seed read (``known_ids is None``) NEVER wakes — it only establishes the baseline,
        so reactions already present at registration don't nudge.
      * otherwise ``fresh_rx`` = adds targeting ``agent`` (by another teammate) whose id
        wasn't in the previous batch. Comparing against the previous batch (not a strict
        ``> cursor``) gives exact boundary dedup: the ``id >= cursor`` read re-includes the
        cursor record every tick, so without this it would re-wake forever; and it can't
        drop a distinct event that happens to share the boundary's microsecond id.
      * ``new_cursor`` = max id seen this tick (None if the batch was empty, so the caller
        keeps its prior cursor — an empty window must not rewind it).
    """
    rids = {r.get("id") for r in reactions if r.get("id")}
    new_cursor = max(rids) if rids else None
    if known_ids is None:
        return [], set(rids), new_cursor
    fresh_rx = [r for r in reactions
                if r.get("id") not in known_ids
                and r.get("op") == "add"
                and r.get("target_from") == agent
                and r.get("from") != agent]
    return fresh_rx, set(rids), new_cursor


def compute_reemit(unseen_ids, now_mono, last_emit_mono, attempts):
    """Pure re-nudge decision (no I/O — hermetically testable). A dropped channel push
    leaves an idle agent unaware of unread messages; re-emit the wake for STILL-UNSEEN
    unread after an exponential-backoff quiet period (``REEMIT_BASE_SECONDS × 2**attempts``
    → 120, 240, 480 s), capped at ``REEMIT_MAX_ATTEMPTS``.

    ``unseen_ids`` MUST be the same ``unread_ids - muted_ids`` set the fresh path uses
    (WP-42: reading a message removes it from unread, so a read message can never re-nudge
    by construction), so a muted message can never re-nudge — that single shared computation
    is what keeps the v0.4.2 no-noise contract provably intact.

    The clock (``last_emit_mono``) is armed ONLY by a real fresh emit (the caller stamps it
    there), never by this function — so a batch that was never first-nudged (a message that
    arrived while its group was muted and is later unmuted, or the seed window) stays
    permanently re-nudge-silent via the first-emit guard until a genuinely new message
    re-arms through the fresh path. That is what makes "muted can never re-nudge" hold even
    across an unmute (the v0.4.2 retro-nudge sin).

    Returns ``(should_reemit, new_attempts, new_last_emit_mono)``:
      * empty unseen → ``(False, 0, None)``: caught up — reset attempts + DISARM the clock
        (do NOT re-stamp it; re-stamping here would arm a never-emitted batch, so an unmute
        that reveals an absorbed message would wrongly re-nudge it).
      * no prior emit (``last_emit_mono is None``) or attempts exhausted → no re-nudge.
      * else fire iff the current quiet period has elapsed, advancing attempt + clock.

    Accepted edge (documented, consistent with fresh-wake semantics): if the clock IS armed
    (a real emit happened) and a muted message is unmuted INTO the still-unseen batch, the
    re-nudge COUNT includes it — same "count reflects all unseen" rule a fresh wake uses.
    """
    if not unseen_ids:
        return (False, 0, None)
    if last_emit_mono is None or attempts >= REEMIT_MAX_ATTEMPTS:
        return (False, attempts, last_emit_mono)
    threshold = REEMIT_BASE_SECONDS * (2 ** attempts)
    if (now_mono - last_emit_mono) >= threshold:
        return (True, attempts + 1, now_mono)
    return (False, attempts, last_emit_mono)


def compute_heartbeat_permit(record, my_instance_id, my_epoch, now):
    """Pure flap-kill decision for one heartbeat tick (no I/O — hermetically testable, I1).

    Ownership model: OWNERSHIP TRANSFERS ONLY AT REGISTER (register_identity writes
    instance_id + epoch=prev+1); a heartbeat never steals a fresh record outright — it only
    decides whether to keep writing in between registrations. ``record`` is the CURRENT
    on-disk agent record, read fresh this same tick; ``my_epoch`` is the epoch THIS instance
    minted at its own last register (stored on Identity, not recomputed here).

    Returns True (permit the write) when:
      * no record, or its instance_id is absent/ours — nothing foreign to detect (also covers
        every legacy pre-WP-19 record, which never had an instance_id to begin with); or
      * the foreign instance_id's heartbeat has gone STALE (not within
        ``HEARTBEAT_STALENESS_SECONDS`` of ``now``) — a legitimate re-claim of a dead process's
        record; never wait forever for a winner that crashed; or
      * TOCTOU tie-break: the record's epoch equals ``my_epoch``. A window exists where MY
        heartbeat reads the record before a COMPETITOR's register lands, and an unguarded
        write would then stamp MY instance_id back OVER their fresh registration (the
        field-merge keeps THEIR epoch, since heartbeats never write epoch — so the record's
        epoch stays theirs even if instance_id gets clobbered). If the on-disk epoch still
        matches what I minted at MY OWN register, no one has re-registered since I did — a
        foreign instance_id here can only be a heartbeat-write STOMP from that race, not a
        legitimate new registration, so I re-claim. The stomper reads foreign+fresh (with a
        DIFFERENT epoch — theirs) next tick and demotes. Converges to most-recent-REGISTER
        within 2 ticks, deterministically.

    False (skip this tick's write) only for a foreign, FRESH, tie-break-losing record — i.e. a
    genuinely different, currently-live claimant registered after we did.
    """
    if not record:
        return True
    foreign_id = record.get("instance_id")
    if not foreign_id or foreign_id == my_instance_id:
        return True
    if not heartbeat_fresh(record.get("lastHeartbeat"), now, HEARTBEAT_STALENESS_SECONDS):
        return True  # foreign but stale — legitimate re-claim of a dead process's record
    if record.get("epoch") == my_epoch:
        return True  # TOCTOU tie-break: this is MY registration, heartbeat-stomped by a race
    return False


def _wake_payload(messages, unseen_ids, agent):
    """Pure: the wake's at-a-glance context for ``unseen_ids`` → ``(count, group_targets,
    mentioned, senders)``. Shared by the fresh-nudge and re-nudge paths so a re-nudge is
    the same wake. Deliberately EXCLUDES the personality / ``msgs_since_reminder``
    bookkeeping and reads no agent record (a re-nudge is not a received message)."""
    group_targets = {m.get("group") for m in messages
                     if m.get("id") in unseen_ids and m.get("group")}
    mentioned = any(agent in m.get("mentions", []) for m in messages
                    if m.get("id") in unseen_ids and m.get("mentions"))
    senders = {m.get("from") for m in messages
               if m.get("id") in unseen_ids and not m.get("group") and m.get("from")}
    return len(unseen_ids), group_targets, mentioned, senders


def merge_pending_into_unread(root, team, agent):
    """Fold ``{agent}_pending.json`` into ``{agent}_unread.json`` (M2 recovery lane).

    A group fan-out that couldn't acquire a member's unread lock appends the record to their
    OWN pending file instead (a separate, rarely-contended lock) so it's never silently lost.
    Each poll tick, the owning watcher merges it back in, deduped by id against current unread
    (idempotent — a crash between the merge and the clear just re-merges harmlessly, since the
    id is already present next time).

    THREE sequential critical sections, lock order FIXED and never nested (both are
    non-reentrant mkdir locks): read pending (unlocked, cheap peek) → lock+merge unread →
    lock+clear pending. The clear step re-reads pending UNDER its lock and removes only the
    ids from the ORIGINAL snapshot — a sender that appended something new in the meantime
    isn't wiped, it survives for the next tick.

    The unlocked peek uses ``read_json_readonly`` (NEVER ``read_json_safe``) — this file is
    MULTI-WRITER (any group sender appends to it under its own lock), so a lockless read that
    catches a sender mid-write is a torn read, not corruption; the destructive
    ``read_json_safe`` would reset it to ``[]`` and the recovery lane would lose the exact
    messages it exists to save (the same anti-pattern C1 fixed on unread.json — here it's
    worse, this file's whole job is never-lose). A torn/missing peek just retries next tick
    (0.5s later) — never resolved by writing anything. Only the CLEAR step (under the pending
    lock, where no writer can be mid-write) uses ``read_json_safe``; that asymmetry is
    intentional, do not "simplify" it away.

    Returns True if anything was merged.
    """
    inboxes_dir = get_inboxes_dir(root, team)
    pending_file = inboxes_dir / f"{agent}_pending.json"
    unread_file = inboxes_dir / f"{agent}_unread.json"

    pending = read_json_readonly(pending_file)
    if not pending:
        return False
    pending_ids = {m.get("id") for m in pending if m.get("id") is not None}

    with file_lock(unread_file):
        unread = read_json_safe(unread_file)
        existing_ids = {m.get("id") for m in unread}
        new_msgs = [m for m in pending if m.get("id") not in existing_ids]
        if new_msgs:
            write_json_atomic(unread_file, unread + new_msgs)

    with file_lock(pending_file):
        current = read_json_safe(pending_file)
        remaining = [m for m in current if m.get("id") not in pending_ids]
        write_json_atomic(pending_file, remaining)
    return True


def run_watcher(send_message, identity, initialized_evt, registered_evt, stop_evt,
                waker_factory=None):
    """Heartbeat + inbox poll loop. Dormant until initialized AND registered.

    ``identity`` is the server's shared Identity object (thread-safe snapshot).
    Re-seeds the baseline whenever the registered agent changes.
    """
    hostname = socket.gethostname()
    last_hb = 0.0
    known_ids = None  # ids already seeded-at-registration or already nudged; None until seeded
    last_agent = None
    last_generation = None  # Identity generation at last reset; same-name re-register bumps this
    muted = set()     # cached muted_groups for this agent; refreshed on the heartbeat tick
    known_reaction_ids = None  # previous tick's returned reaction ids; None until first read
    reaction_cursor = None     # high-water mark (max reaction id seen) driving the read window
    last_emit_mono = None      # monotonic time of the last wake emit (fresh or re-nudge); None until first
    reemit_attempts = 0        # re-nudge attempts in the current quiet period (capped)
    demoted = False    # WP-19 flap-kill: True while a foreign live claimant has superseded us
                       # (logged once per demotion episode, not per tick)
    hb_failed = False  # W7: True while a heartbeat write is lock-contended (logged once per
                       # consecutive-failure episode, not per tick)
    waker = None
    harness = harness_mod.current()
    last_waker_generation = None
    wake_disabled_logged = False
    make = waker_factory or make_waker
    snapshot_all = getattr(identity, "snapshot_all", None)
    if snapshot_all is None:
        snapshot_waker = getattr(identity, "snapshot_waker", None) or (lambda: (None, 0))

        def snapshot_all():
            return identity.snapshot_with_generation() + snapshot_waker()

    def dispatch(kind, content, meta, count, attempt):
        nonlocal wake_disabled_logged
        if waker is None:
            if harness.needs_session and not wake_disabled_logged:
                wake_disabled_logged = True
                print(f"[teammate-comms] wake disabled for agent={last_agent!r}: register with "
                      f"harness_session so teammates can wake you.", file=sys.stderr, flush=True)
            return False
        if waker.wake(content, meta):
            _log_emit(kind, count, attempt)
            return True
        return False

    while not stop_evt.is_set():
        try:
            if not (initialized_evt.is_set() and registered_evt.is_set()):
                stop_evt.wait(POLL_SECONDS)
                continue

            # W4: ONE lock acquisition — snapshot() + get_generation() as two separate calls
            # could have a set() land in between, pairing a STALE root/inbox with a NEW
            # generation for one tick.
            (agent, team, root, unread_file, generation,
             session, waker_generation) = snapshot_all()
            if agent is None or root is None:
                stop_evt.wait(POLL_SECONDS)
                continue

            # Identity (re)set: re-seed for the new inbox. Triggers on agent name change OR on a
            # same-name re-registration (post-compaction), detected via a bumped generation counter.
            # The watcher reset here purges known_ids and clocks so stale in-memory state doesn't
            # suppress wakes after re-register. last_hb=0 forces an immediate heartbeat +
            # muted-cache refresh.
            if agent != last_agent or generation != last_generation:
                known_ids = None
                last_agent = agent
                last_generation = generation
                muted = set()
                last_hb = 0.0
                known_reaction_ids = None
                reaction_cursor = None
                last_emit_mono = None
                reemit_attempts = 0
                demoted = False
                hb_failed = False
                last_waker_generation = None

            if waker_generation != last_waker_generation:
                harness = harness_mod.current()
                waker = make(harness, session, send_message)
                if last_waker_generation is not None:
                    last_hb = 0.0
                last_waker_generation = waker_generation
                wake_disabled_logged = False

            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_SECONDS:
                # ONE read before the write (I1 flap-kill, gate composition note): decide
                # whether a foreign LIVE claimant has superseded us, and refresh the
                # muted-groups cache from the SAME read — folded into one read, not two.
                hb_rec = read_agent_record(root, team, agent) or {}
                muted = set(hb_rec.get("muted_groups", []))
                my_instance_id = identity.get_instance_id()
                my_epoch = identity.get_epoch()
                if compute_heartbeat_permit(hb_rec, my_instance_id, my_epoch, datetime.now()):
                    # I4: stamp type="full" — truthful, since run_watcher only ever runs for a
                    # registered full instance — so a delete-then-heartbeat resurrection carries
                    # a real type instead of a type-less ghost. MANDATORY guard: never stomp an
                    # existing type="human" record (the same read above already has it) — this
                    # composes with the flap-kill's foreign-instance skip above, which already
                    # covers a foreign AGENT; a human record has no instance_id at all, so it
                    # would otherwise sail through compute_heartbeat_permit's "nothing foreign to
                    # detect" branch and get overwritten here.
                    type_field = {} if hb_rec.get("type") == "human" else {"type": "full"}
                    wrote = write_agent_record(
                        root, team, agent, timeout=2,
                        channel=True, pid=os.getpid(), host=hostname,
                        instance_id=my_instance_id,
                        lastHeartbeat=now_timestamp(),
                        **type_field,
                    )
                    demoted = False
                    if wrote:
                        hb_failed = False
                        last_hb = now  # only advance on a SUCCESSFUL write
                    elif not hb_failed:
                        # W7: lock-contended (False return) — do NOT advance last_hb, so the
                        # next 0.5s poll retries instead of waiting a full 5s; a live agent must
                        # not look stale to is_channel_alive (which reincarnate's guard trusts)
                        # just because one heartbeat write raced a lock. Log once per episode.
                        hb_failed = True
                        print(f"[teammate-comms] heartbeat write contended for agent={agent!r} "
                              f"— retrying next poll (no lastHeartbeat advance this tick).",
                              file=sys.stderr, flush=True)
                else:
                    hb_failed = False
                    if not demoted:
                        demoted = True
                        print(f"[teammate-comms] superseded by a newer claimant for agent={agent!r} "
                              f"(instance_id={hb_rec.get('instance_id')!r}, host={hb_rec.get('host')!r}, "
                              f"pid={hb_rec.get('pid')!r}) — heartbeat write skipped until it goes stale.",
                              file=sys.stderr, flush=True)
                    last_hb = now  # DELIBERATE skip (foreign claimant) — we're not retrying this
                # Reaction wakes still run every tick even when the write above was skipped
                # (low-volume → only on the 5s heartbeat tick, not every poll):
                # wake the AUTHOR of a reacted-to message. A high-water cursor drives the read
                # window forward (since=reaction_cursor, oldest_first once seeded) so a burst
                # larger than the limit pages across ticks instead of scrolling past a fixed
                # tail and silently missing a wake (audit A-2). A giant burst drains at
                # 500/tick (~5s/tick) — bounded delay, never a drop. Decision logic lives in
                # the pure compute_reaction_wakes (hermetically tested). Seed = no startup wake.
                reactions = read_reactions(root, team, since=reaction_cursor, limit=500,
                                           oldest_first=(reaction_cursor is not None))
                fresh_rx, next_known_rx, new_rcursor = compute_reaction_wakes(
                    reactions, known_reaction_ids, agent)
                if fresh_rx:
                    content, meta = build_reaction_payload(agent, fresh_rx)
                    if not dispatch("reaction", content, meta, len(fresh_rx), 0):
                        next_known_rx, new_rcursor = known_reaction_ids, None
                known_reaction_ids = next_known_rx
                if new_rcursor is not None:
                    reaction_cursor = new_rcursor

            # M2: merge any records a group fan-out couldn't deliver directly (lock-contended
            # unread) but recovered into our OWN pending file. Cheap Path.exists() gate — one
            # os.stat per poll, not a full reparse — before touching the pending file at all.
            pending_file = unread_file.with_name(f"{agent}_pending.json")
            if pending_file.exists():
                merge_pending_into_unread(root, team, agent)

            messages = read_json_readonly(unread_file)
            if messages is not None:  # None = unreadable mid-write; skip cycle
                unread_ids = {m.get("id") for m in messages if m.get("id") is not None}
                # MUTE: ids of unread messages in a muted group. They stay in the inbox (seen
                # via teammate_inbox) but are excluded from the WAKE (fresh/count/targets). A
                # record without a 'group' key (a 1:1 DM) can never be muted → never-miss-a-DM.
                muted_ids = {m.get("id") for m in messages
                             if m.get("id") is not None and m.get("group") in muted}
                if known_ids is None:
                    # Seed at first read after registration: messages already present are
                    # drained by the agent's startup teammate_inbox — don't nudge for them.
                    known_ids = set(unread_ids)
                else:
                    # UNSEEN = unread - muted — the ONE set both the first-nudge and the WP-9
                    # re-nudge gate on. WP-42 (auto-ack on read) made this valid: unread now
                    # literally means never-shown, since teammate_inbox moves whatever it shows
                    # straight into the read inbox — no separate last_seen/durable_seen tracking
                    # is needed to exclude a read-but-unacked message, because that state no
                    # longer exists. Muted-group messages are excluded so they can never
                    # (re-)nudge; muted ids are still tracked in known_ids below, so an unmute
                    # never retro-nudges.
                    unseen_ids = unread_ids - muted_ids
                    fresh = unseen_ids - known_ids
                    if fresh:
                        # A genuinely-new message → first nudge. group_targets name the reply
                        # target for ANY unseen group message (mixed-batch fix); mentioned/
                        # senders name the 🔔 and the sources. All from the shared payload.
                        unseen_count, group_targets, mentioned, senders = _wake_payload(
                            messages, unseen_ids, agent)
                        content, meta = build_wake_payload(
                            agent, unseen_count, groups=group_targets,
                            mentioned=mentioned, senders=senders)
                        if dispatch("fresh", content, meta, unseen_count, 0):
                            known_ids |= unread_ids
                            last_emit_mono = now      # (re)arm the re-nudge backoff for this batch
                            reemit_attempts = 0
                    elif not harness.durable_wake:
                        # No NEW message, but a dropped channel push may have left the agent
                        # unaware of still-unseen unread (GH #38736/#61797). Re-nudge with capped
                        # backoff for ANY still-unseen message (content-agnostic recovery — a
                        # dropped emit is a dropped emit regardless of message type). compute_reemit
                        # also RESETS the clock+attempts when unseen is empty (caught up), so a
                        # read re-arms the next batch cleanly. No-noise contract holds because
                        # unseen_ids = unread_ids - muted_ids already excludes read (a read message
                        # leaves unread entirely) and muted, and compute_reemit's first-emit guard
                        # is unchanged.
                        prev_attempts, prev_emit_mono = reemit_attempts, last_emit_mono
                        do_reemit, reemit_attempts, last_emit_mono = compute_reemit(
                            unseen_ids, now, last_emit_mono, reemit_attempts)
                        if do_reemit:
                            unseen_count, group_targets, mentioned, senders = _wake_payload(
                                messages, unseen_ids, agent)
                            content, meta = build_wake_payload(
                                agent, unseen_count, groups=group_targets,
                                mentioned=mentioned, senders=senders)
                            if not dispatch("renudge", content, meta, unseen_count,
                                            reemit_attempts):
                                reemit_attempts, last_emit_mono = prev_attempts, prev_emit_mono
                    # Absorb muted ids as "known" every cycle (even with no fresh wake) so a
                    # later unmute finds them already-known → no retro-nudge for still-unread
                    # muted messages (safe under-nudge direction).
                    known_ids |= muted_ids
                    known_ids &= unread_ids  # prune acked/removed ids; keeps the set bounded

            stop_evt.wait(POLL_SECONDS)
        except Exception as exc:
            print(f"[teammate-comms] watcher error (loop continues): {exc}",
                  file=sys.stderr, flush=True)
            stop_evt.wait(POLL_SECONDS)  # back off at poll cadence; returns immediately on stop
