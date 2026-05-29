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

Reliability contract: the inbox JSON is the source of truth. A dropped push (session
closed) loses nothing — it's recovered on the next inbox read.
"""

import os
import socket
import time

from .comms import now_timestamp, read_agent_record, read_json_readonly, write_agent_record

HEARTBEAT_SECONDS = 5
POLL_SECONDS = 0.5


def emit_channel_event(send_message, agent, count, personality=None, groups=None):
    """Push one ``notifications/claude/channel`` event for ``count`` unread.

    If ``personality`` is set, it leads the content so a woken idle instance is
    reminded who it is before it acts. If ``groups`` (a set of ``#``-prefixed group
    names whose messages triggered this wake) is non-empty, the content names the
    group reply target so the agent replies to the group, not 1:1 to the sender.
    """
    intro = f"You are {agent}: {personality.rstrip('. ')}. " if personality else ""
    if groups:
        # The `group` field already carries the leading '#', so render it verbatim.
        targets = " or ".join(f"to:'{g}'" for g in sorted(groups))
        group_line = (
            f" Some are group messages — reply to the group with `teammate_send` "
            f"{targets} (replying to the sender instead starts a 1:1 and fractures the "
            f"thread)."
        )
    else:
        group_line = ""
    content = (
        f"{intro}You have {count} new teammate message(s). Use your teammate-comms "
        f"tools to read them: call `teammate_inbox` to view, then `teammate_ack` "
        f"(id \"all\") once handled. Reply with `teammate_send`.{group_line} (Group "
        f"threads: the full history is in `teammate_group` action=history.) You are a "
        f"full instance — this channel wakes you; no polling loop needed."
    )
    meta = {"count": str(count), "agent": agent}
    send_message({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content, "meta": meta},
    })


def run_watcher(send_message, identity, initialized_evt, registered_evt, stop_evt):
    """Heartbeat + inbox poll loop. Dormant until initialized AND registered.

    ``identity`` is the server's shared Identity object (thread-safe snapshot).
    Re-seeds the baseline whenever the registered agent changes.
    """
    hostname = socket.gethostname()
    last_hb = 0.0
    known_ids = None  # ids already seeded-at-registration or already nudged; None until seeded
    last_agent = None

    while not stop_evt.is_set():
        if not (initialized_evt.is_set() and registered_evt.is_set()):
            stop_evt.wait(POLL_SECONDS)
            continue

        agent, team, root, unread_file = identity.snapshot()
        if agent is None or root is None:
            stop_evt.wait(POLL_SECONDS)
            continue

        # Identity (re)set: re-seed for the new inbox (Identity.set already cleared
        # its last_seen, so both reset together).
        if agent != last_agent:
            known_ids = None
            last_agent = agent

        now = time.monotonic()
        if now - last_hb >= HEARTBEAT_SECONDS:
            write_agent_record(
                root, team, agent, timeout=2,
                channel=True, pid=os.getpid(), host=hostname,
                lastHeartbeat=now_timestamp(),
            )
            last_hb = now

        messages = read_json_readonly(unread_file)
        if messages is not None:  # None = unreadable mid-write; skip cycle
            unread_ids = {m.get("id") for m in messages if m.get("id") is not None}
            if known_ids is None:
                # Seed at first read after registration: messages already present are
                # drained by the agent's startup teammate_inbox — don't nudge for them.
                known_ids = set(unread_ids)
            else:
                # Nudge only for messages the agent hasn't been shown (not in last_seen)
                # and we haven't already nudged for (not in known_ids). Count reflects
                # all UNSEEN unread, so a read-but-unacked message never pads it.
                last_seen = identity.get_last_seen() or set()
                fresh = unread_ids - known_ids - last_seen
                if fresh:
                    record = read_agent_record(root, team, agent) or {}
                    unseen_ids = unread_ids - last_seen
                    unseen_count = len(unseen_ids)
                    # Name the group reply target for ANY unseen (unread, not-yet-read)
                    # group message — not just the one that triggered this wake — so a
                    # DM-triggered wake still surfaces a pending group thread (mixed-batch
                    # fix). (.get guards id-less / 1:1 records, which have no 'group' key.)
                    group_targets = {m.get("group") for m in messages
                                     if m.get("id") in unseen_ids and m.get("group")}
                    emit_channel_event(send_message, agent, unseen_count,
                                       record.get("personality"), groups=group_targets)
                    known_ids |= unread_ids
                known_ids &= unread_ids  # prune acked/removed ids; keeps the set bounded

        stop_evt.wait(POLL_SECONDS)
