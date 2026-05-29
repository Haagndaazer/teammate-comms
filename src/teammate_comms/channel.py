"""Channel wake mechanics: heartbeat + inbox watcher + idle push.

The single watcher thread starts with the server but stays dormant until two
gates open: ``notifications/initialized`` (the client is ready) AND registration
(``teammate_register`` has set this instance's identity). Once both are open it
polls the agent's own ``_unread.json`` and emits a ``notifications/claude/channel``
event whenever the unread count rises above a baseline seeded at registration —
so a peer's ``teammate_send`` writing this inbox *is* the nudge.

Reliability contract: the inbox JSON is the source of truth. The baseline reset
on ack is a coarse heuristic, not a correctness guarantee — a missed nudge is
recovered on the next inbox read. Dropped pushes (session closed) lose nothing.
"""

import os
import socket
import time

from .comms import now_timestamp, read_agent_record, read_json_readonly, write_agent_record

HEARTBEAT_SECONDS = 5
POLL_SECONDS = 0.5


def emit_channel_event(send_message, agent, count, personality=None):
    """Push one ``notifications/claude/channel`` event for ``count`` unread.

    If ``personality`` is set, it leads the content so a woken idle instance is
    reminded who it is before it acts.
    """
    intro = f"You are {agent}: {personality.rstrip('. ')}. " if personality else ""
    content = (
        f"{intro}You have {count} new teammate message(s). Use your teammate-comms "
        f"tools to read them: call `teammate_inbox` to view, then `teammate_ack` "
        f"(id \"all\") once handled. Reply with `teammate_send`. (Group threads: the "
        f"full history is in `teammate_group` action=history.) You are a full "
        f"instance — this channel wakes you; no polling loop needed."
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
    baseline = None
    last_agent = None

    while not stop_evt.is_set():
        if not (initialized_evt.is_set() and registered_evt.is_set()):
            stop_evt.wait(POLL_SECONDS)
            continue

        agent, team, root, unread_file = identity.snapshot()
        if agent is None or root is None:
            stop_evt.wait(POLL_SECONDS)
            continue

        # Identity (re)set: reset the baseline so it re-seeds for this inbox.
        if agent != last_agent:
            baseline = None
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
            count = len(messages)
            if baseline is None:
                # Seed at first read after registration: messages already present
                # are drained by the agent's startup teammate_inbox — the channel
                # must not also nudge for them.
                baseline = count
            elif count > baseline:
                # Read the record only when actually nudging (rare) to fetch the
                # personality reminder — avoids a per-poll read.
                record = read_agent_record(root, team, agent) or {}
                emit_channel_event(send_message, agent, count, record.get("personality"))
                baseline = count
            elif count < baseline:
                baseline = count

        stop_evt.wait(POLL_SECONDS)
