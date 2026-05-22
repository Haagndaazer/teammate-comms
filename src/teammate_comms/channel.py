"""Channel wake mechanics: heartbeat + inbox watcher + idle push.

Ported from the proven prototype. Once the session is initialized this polls the
agent's own ``_unread.json`` and emits a ``notifications/claude/channel`` event
whenever the unread count rises above a baseline seeded at init — so a peer's
``teammate_send`` writing this inbox *is* the nudge.

Reliability contract: the inbox JSON is the source of truth. The baseline reset
on ack is a coarse heuristic (it reacts to the array length between polls), NOT a
correctness guarantee — a missed nudge is always recovered when the agent next
reads its inbox. Pushes are best-effort; a dropped event (session closed) loses
nothing.
"""

import os

from .comms import now_timestamp, read_json_readonly, write_agent_record

HEARTBEAT_SECONDS = 5
POLL_SECONDS = 0.5


def emit_channel_event(send_message, agent, count):
    """Push one ``notifications/claude/channel`` event for ``count`` unread."""
    content = (
        f"You have {count} new teammate message(s). Use your teammate-comms "
        f"tools to read them: call `teammate_inbox` to view, then `teammate_ack` "
        f"(id \"all\") once handled. Reply with `teammate_send`. You are a full "
        f"instance — this channel wakes you; no polling loop needed."
    )
    meta = {"count": str(count), "agent": agent}
    send_message({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content, "meta": meta},
    })


def run_watcher(send_message, agent, team, unread_file, hostname,
                initialized_evt, stop_evt):
    """Heartbeat + inbox poll loop. Emits events only after ``initialized``.

    Args mirror the prototype: ``send_message`` is the thread-safe stdout writer
    owned by the server; ``initialized_evt``/``stop_evt`` are the server's gate
    and shutdown signals.
    """
    last_hb = 0.0
    baseline = None  # seeded to the unread count when the gate first opens
    while not stop_evt.is_set():
        now = _monotonic()
        if now - last_hb >= HEARTBEAT_SECONDS:
            write_agent_record(
                team, agent, timeout=2,
                channel=True, pid=os.getpid(), host=hostname,
                lastHeartbeat=now_timestamp(),
            )
            last_hb = now

        if initialized_evt.is_set():
            messages = read_json_readonly(unread_file)
            if messages is not None:  # None = unreadable mid-write; skip cycle
                count = len(messages)
                if baseline is None:
                    # Seed at first post-init read: messages already present at
                    # session start are drained by the agent's startup inbox
                    # read — the channel must not also nudge for them.
                    baseline = count
                elif count > baseline:
                    emit_channel_event(send_message, agent, count)
                    baseline = count
                elif count < baseline:
                    baseline = count

        stop_evt.wait(POLL_SECONDS)


def _monotonic():
    import time
    return time.monotonic()
