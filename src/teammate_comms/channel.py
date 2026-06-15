"""Channel wake mechanics: heartbeat + inbox watcher + idle push.

The single watcher thread starts with the server but stays dormant until two
gates open: ``notifications/initialized`` (the client is ready) AND registration
(``teammate_register`` has set this instance's identity). Once both are open it
polls the agent's own ``_unread.json`` and emits a ``notifications/claude/channel``
event for messages the agent **has not yet been shown** — so a peer's
``teammate_send`` writing this inbox *is* the nudge.

Nudge gating (v0.4.2): a message wakes the agent only if its id is neither already
seen (in ``Identity.last_seen`` — the ids returned by the last full
``teammate_inbox`` read) nor already nudged-for (a watcher-local ``known_ids`` set).
At registration ``known_ids`` is seeded to whatever is already in the inbox, so
pre-existing messages don't nudge (the agent drains them with a startup
``teammate_inbox``). The emitted count is the number of *unseen* unread messages, so
a message you've read but not yet acked never pads the count. Reading — not acking —
is what silences a nudge. This is missed-nudge-safe: a genuinely new message has a
fresh id in neither set, so it always nudges.

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
import sys
import time

from .comms import (
    REACTION_EMOJI,
    now_timestamp,
    read_agent_record,
    read_json_readonly,
    read_reactions,
    write_agent_record,
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


def emit_channel_event(send_message, agent, count, groups=None,
                       mentioned=False, senders=None):
    """Push one ``notifications/claude/channel`` event for ``count`` unread.

    Signal-only wake: names WHERE the messages came from (``senders`` for DMs,
    ``groups`` for group posts) plus a 🔔 @mention note and the group reply target
    when applicable. The v0.6 "You are <name>: <personality>" owner-reminder was
    dropped (WP-11a): the persona is durable in the agent's session context and the
    register echo; repeating it every ~10 msgs was redundant token cost.
    """
    mention_note = "🔔 @mention. " if mentioned else ""
    # DM senders first, then #groups (groups already carry the '#').
    sources = sorted(set(senders or [])) + sorted(groups or [])
    from_part = f" from {', '.join(sources)}" if sources else ""
    if groups:
        targets = " or ".join(f"to:'{g}'" for g in sorted(groups))
        group_line = f" Reply to {targets} not the sender."
    else:
        group_line = ""
    content = f"{mention_note}📬 {count} new message(s){from_part}.{group_line}"
    meta = {"count": str(count), "agent": agent}
    send_message({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content, "meta": meta},
    })


def emit_reaction_event(send_message, agent, reactions):
    """Wake the AUTHOR of reacted-to messages. Distinct ``meta.kind="reaction"`` so
    consumers separate it from message wakes (it never participates in the unseen count)."""
    parts, seen = [], set()
    for r in reactions[-6:]:
        who, em = r.get("from"), r.get("emoji")
        if (who, em) in seen:
            continue
        seen.add((who, em))
        parts.append(f"{who} {REACTION_EMOJI.get(em, em)}")
    content = f"💬 {', '.join(parts)} reacted to your message(s)."
    send_message({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content,
                   "meta": {"count": str(len(reactions)), "agent": agent, "kind": "reaction"}},
    })


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

    ``unseen_ids`` MUST be the same ``(unread - muted) - last_seen`` set the fresh path
    uses, so a read-but-unacked or muted message can never re-nudge — that single shared
    computation is what keeps the v0.4.2 no-noise contract provably intact.

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


def _renudge_ids(messages, unseen_ids, agent):
    """Pure: subset of ``unseen_ids`` that warrant a re-nudge — DM / urgent / @mention.

    Ambient group chatter is excluded: the first-nudge already ran; re-nudging for
    low-priority group traffic wastes tokens. ``compute_reemit`` still receives the full
    ``unseen_ids`` for clock management so the disarm-on-empty (all-caught-up) invariant
    holds — this function gates only the EMIT, not the state update.
    """
    return {
        m.get("id") for m in messages
        if m.get("id") in unseen_ids
        and (not m.get("group")                       # DM
             or m.get("priority") == "urgent"         # urgent group post
             or agent in m.get("mentions", []))        # @mention in group post
    }


def run_watcher(send_message, identity, initialized_evt, registered_evt, stop_evt):
    """Heartbeat + inbox poll loop. Dormant until initialized AND registered.

    ``identity`` is the server's shared Identity object (thread-safe snapshot).
    Re-seeds the baseline whenever the registered agent changes.
    """
    hostname = socket.gethostname()
    last_hb = 0.0
    known_ids = None  # ids already seeded-at-registration or already nudged; None until seeded
    last_agent = None
    muted = set()     # cached muted_groups for this agent; refreshed on the heartbeat tick
    known_reaction_ids = None  # previous tick's returned reaction ids; None until first read
    reaction_cursor = None     # high-water mark (max reaction id seen) driving the read window
    last_emit_mono = None      # monotonic time of the last wake emit (fresh or re-nudge); None until first
    reemit_attempts = 0        # re-nudge attempts in the current quiet period (capped)

    while not stop_evt.is_set():
        if not (initialized_evt.is_set() and registered_evt.is_set()):
            stop_evt.wait(POLL_SECONDS)
            continue

        agent, team, root, unread_file = identity.snapshot()
        if agent is None or root is None:
            stop_evt.wait(POLL_SECONDS)
            continue

        # Identity (re)set: re-seed for the new inbox (Identity.set already cleared
        # its last_seen, so both reset together). last_hb=0 forces an immediate
        # heartbeat + muted-cache refresh for the new identity.
        if agent != last_agent:
            known_ids = None
            last_agent = agent
            muted = set()
            last_hb = 0.0
            known_reaction_ids = None
            reaction_cursor = None
            last_emit_mono = None
            reemit_attempts = 0

        now = time.monotonic()
        if now - last_hb >= HEARTBEAT_SECONDS:
            write_agent_record(
                root, team, agent, timeout=2,
                channel=True, pid=os.getpid(), host=hostname,
                lastHeartbeat=now_timestamp(),
            )
            last_hb = now
            # Refresh the muted-groups cache (≤5s staleness for a mute to take effect) so
            # the per-poll wake filter below never adds a disk read.
            hb_rec = read_agent_record(root, team, agent) or {}
            muted = set(hb_rec.get("muted_groups", []))
            # Reaction wakes (low-volume → only on the 5s heartbeat tick, not every poll):
            # wake the AUTHOR of a reacted-to message. A high-water cursor drives the read
            # window forward (since=reaction_cursor, oldest_first once seeded) so a burst
            # larger than the limit pages across ticks instead of scrolling past a fixed
            # tail and silently missing a wake (audit A-2). A giant burst drains at
            # 500/tick (~5s/tick) — bounded delay, never a drop. Decision logic lives in
            # the pure compute_reaction_wakes (hermetically tested). Seed = no startup wake.
            reactions = read_reactions(root, team, since=reaction_cursor, limit=500,
                                       oldest_first=(reaction_cursor is not None))
            fresh_rx, known_reaction_ids, new_rcursor = compute_reaction_wakes(
                reactions, known_reaction_ids, agent)
            if fresh_rx:
                emit_reaction_event(send_message, agent, fresh_rx)
                _log_emit("reaction", len(fresh_rx), 0)
            if new_rcursor is not None:
                reaction_cursor = new_rcursor

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
                # UNSEEN = (unread - muted) - last_seen — the ONE set both the first-nudge
                # and the WP-9 re-nudge gate on. A read-but-unacked message (in last_seen)
                # and any muted-group message are excluded, so neither can ever (re-)nudge:
                # that shared computation is what keeps the v0.4.2 no-noise contract intact.
                # Muted ids are still tracked in known_ids below, so an unmute never
                # retro-nudges.
                last_seen = identity.get_last_seen() or set()
                unseen_ids = (unread_ids - muted_ids) - last_seen
                fresh = unseen_ids - known_ids
                if fresh:
                    # A genuinely-new message → first nudge. group_targets name the reply
                    # target for ANY unseen group message (mixed-batch fix); mentioned/
                    # senders name the 🔔 and the sources. All from the shared payload.
                    unseen_count, group_targets, mentioned, senders = _wake_payload(
                        messages, unseen_ids, agent)
                    emit_channel_event(send_message, agent, unseen_count,
                                       groups=group_targets,
                                       mentioned=mentioned, senders=senders)
                    _log_emit("fresh", unseen_count, 0)
                    known_ids |= unread_ids
                    last_emit_mono = now      # (re)arm the re-nudge backoff for this batch
                    reemit_attempts = 0
                else:
                    # No NEW message, but a dropped channel push may have left the agent
                    # unaware of still-unseen unread (GH #38736/#61797). Re-nudge with capped
                    # backoff. compute_reemit also RESETS the clock+attempts when unseen is
                    # empty (caught up), so an ack/read re-arms the next batch cleanly.
                    # Clock management uses the FULL unseen_ids so the disarm-on-empty
                    # invariant holds even when ambient group chatter is in the unseen set.
                    do_reemit, reemit_attempts, last_emit_mono = compute_reemit(
                        unseen_ids, now, last_emit_mono, reemit_attempts)
                    if do_reemit and _renudge_ids(messages, unseen_ids, agent):
                        # Emit only for DM/urgent/@mention — ambient group chatter exhausts
                        # the re-nudge budget silently (state still advanced above). The
                        # payload uses the full unseen set so the agent sees their total
                        # backlog even when a DM triggered the re-nudge.
                        unseen_count, group_targets, mentioned, senders = _wake_payload(
                            messages, unseen_ids, agent)
                        emit_channel_event(send_message, agent, unseen_count,
                                           None, groups=group_targets,
                                           mentioned=mentioned, senders=senders)
                        _log_emit("renudge", unseen_count, reemit_attempts)
                # Absorb muted ids as "known" every cycle (even with no fresh wake) so a
                # later unmute finds them already-known → no retro-nudge for still-unread
                # muted messages (safe under-nudge direction).
                known_ids |= muted_ids
                known_ids &= unread_ids  # prune acked/removed ids; keeps the set bounded

        stop_evt.wait(POLL_SECONDS)
