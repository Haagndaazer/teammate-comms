"""The user's availability: one status per comms root, shared by every agent on it.

The effective status is computed from the stored state and the clock, never flipped by a
timer. Copies on other machines and on a cloud mailbox converge by ``(seq, rev)``.
"""

import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from .comms import (
    COMMS_SUBDIR,
    CommsError,
    ensure_inbox,
    file_lock,
    file_lock_optional,
    get_inboxes_dir,
    message_ts,
    new_message_id,
    read_json_readonly,
    read_json_safe,
    write_agent_record,
    write_json_atomic,
)

STATUS_SENDER = "comms-status"
FILE_NAME = "availability.json"
SEEN_FIELD = "availability_seen"
AVAILABLE, AWAY, HEADS_UP = "available", "away", "heads_up"
STATUSES = (AVAILABLE, AWAY)
DEFAULT_BEHAVIOUR = "work autonomously"
BEHAVIOUR_CHARS = 500
BY_CHARS = 160
DEFAULT_HEADS_UP_SECONDS = 600
MAX_HEADS_UP_MINUTES = 120
MAX_LEAVE_SECONDS = 7 * 86400
MAX_SEQ = 10 ** 12
MAX_INCOMING_SEQ = 10 ** 9
_REV_RE = re.compile(r"[A-Za-z0-9]{1,32}")
MAX_EPOCH = 4102444800
NOTICE_LOCK_SECONDS = 1


def state_file(root):
    return Path(root) / COMMS_SUBDIR / FILE_NAME


def default_state():
    return {"v": 1, "seq": 0, "rev": "", "status": AVAILABLE, "status_epoch": None,
            "updated_epoch": None, "updated_by": "", "leave_at_epoch": None,
            "heads_up_seconds": DEFAULT_HEADS_UP_SECONDS,
            "away_behaviour": DEFAULT_BEHAVIOUR, "behaviour_seq": 0, "behaviour_rev": "",
            "behaviour_by": ""}


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or not low <= value <= high:
        return None
    return value


def _line(value, cap):
    if not isinstance(value, str):
        return ""
    text = "".join(ch if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp") else " "
                   for ch in value)
    return " ".join(text.replace("«", '"').replace("»", '"').split())[:cap]


def _rev(value):
    return value if isinstance(value, str) and _REV_RE.fullmatch(value) else ""


def clean(raw):
    """A stored or received state made safe, or None if it is not a state."""
    if not isinstance(raw, dict) or raw.get("status") not in STATUSES:
        return None
    seq = _number(raw.get("seq"), 0, MAX_SEQ)
    bseq = _number(raw.get("behaviour_seq"), 0, MAX_SEQ)
    if seq is None or bseq is None or int(seq) != seq or int(bseq) != bseq:
        return None
    heads = _number(raw.get("heads_up_seconds"), 0, MAX_HEADS_UP_MINUTES * 60)
    out = default_state()
    out.update({
        "seq": int(seq), "rev": _rev(raw.get("rev")), "status": raw["status"],
        "status_epoch": _number(raw.get("status_epoch"), 1, MAX_EPOCH),
        "updated_epoch": _number(raw.get("updated_epoch"), 1, MAX_EPOCH),
        "updated_by": _line(raw.get("updated_by"), BY_CHARS),
        "leave_at_epoch": _number(raw.get("leave_at_epoch"), 1, MAX_EPOCH),
        "heads_up_seconds": int(heads) if heads is not None else DEFAULT_HEADS_UP_SECONDS,
        "away_behaviour": _line(raw.get("away_behaviour"), BEHAVIOUR_CHARS) or DEFAULT_BEHAVIOUR,
        "behaviour_seq": int(bseq), "behaviour_rev": _rev(raw.get("behaviour_rev")),
        "behaviour_by": _line(raw.get("behaviour_by"), BY_CHARS),
    })
    if out["status"] == AWAY:
        out["leave_at_epoch"] = None
    return out


def read(root):
    return clean(read_json_readonly(state_file(root))) or default_state()


def is_away(state, now):
    leave = state["leave_at_epoch"]
    return state["status"] == AWAY or (leave is not None and now >= leave)


def away_since(state, now):
    if state["status"] == AWAY:
        return state["status_epoch"] or state["updated_epoch"]
    return state["leave_at_epoch"] if is_away(state, now) else None


def phase(state, now):
    if is_away(state, now):
        return AWAY
    leave = state["leave_at_epoch"]
    if leave is not None and now >= leave - state["heads_up_seconds"]:
        return HEADS_UP
    return AVAILABLE


def snapshot(state, now):
    return {"rev": state["rev"], "phase": phase(state, now),
            "leave": state["leave_at_epoch"] is not None,
            "behaviour_rev": state["behaviour_rev"]}


def clean_behaviour(value):
    if not isinstance(value, str) or not value.strip():
        raise CommsError("'away_behaviour' must be text, for example 'work autonomously' "
                         "or 'wait'.")
    if len(value.strip()) > BEHAVIOUR_CHARS:
        raise CommsError(f"'away_behaviour' must be at most {BEHAVIOUR_CHARS} characters.")
    text = _line(value, BEHAVIOUR_CHARS)
    if not text:
        raise CommsError("'away_behaviour' has no readable text.")
    return text


def parse_leave(leave_at, in_minutes, now):
    """The leave time as epoch seconds; ``None`` cancels a pending one."""
    if in_minutes is not None:
        if leave_at not in (None, ""):
            raise CommsError("Give 'leave_at' or 'in_minutes', not both.")
        if isinstance(in_minutes, bool) or not isinstance(in_minutes, int):
            raise CommsError("'in_minutes' must be a whole number of minutes.")
        if not 1 <= in_minutes <= MAX_LEAVE_SECONDS // 60:
            raise CommsError("'in_minutes' must be between 1 and 10080 (7 days).")
        return now + in_minutes * 60
    if not isinstance(leave_at, str):
        raise CommsError("'leave_at' must be a time such as 2026-11-21T15:30:00-06:00.")
    if not leave_at.strip():
        return None
    try:
        when = datetime.fromisoformat(leave_at.strip())
    except ValueError:
        raise CommsError(f"'leave_at' {leave_at!r} is not a time. Use ISO 8601 with a UTC "
                         f"offset, for example 2026-11-21T15:30:00-06:00, or use in_minutes.")
    if when.tzinfo is None or when.utcoffset() is None:
        raise CommsError("'leave_at' needs a UTC offset (for example -06:00 or Z) so the time "
                         "is not ambiguous, or use in_minutes.")
    epoch = when.timestamp()
    if epoch <= now:
        raise CommsError("'leave_at' is in the past. To mark the user away now, pass "
                         "status='away'.")
    if epoch > now + MAX_LEAVE_SECONDS:
        raise CommsError("'leave_at' must be within the next 7 days.")
    return epoch


def clean_heads_up(minutes):
    if isinstance(minutes, bool) or not isinstance(minutes, int) \
            or not 0 <= minutes <= MAX_HEADS_UP_MINUTES:
        raise CommsError(f"'heads_up_minutes' must be a whole number from 0 to "
                         f"{MAX_HEADS_UP_MINUTES}.")
    return minutes * 60


def _new_rev():
    return os.urandom(6).hex()


def _write(root, state):
    path = state_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, state)


_UNSET = object()


def change(root, by, status=None, leave_at=_UNSET, heads_up_seconds=None,
           away_behaviour=None, now=None):
    """Apply a change made on this machine and return the new state.

    ``leave_at`` is epoch seconds, or None to cancel a pending time.
    """
    now = time.time() if now is None else now
    by = _line(by, BY_CHARS)
    if status is not None and status not in STATUSES:
        raise CommsError("'status' must be 'available' or 'away'.")
    if status is not None and leave_at is not _UNSET and leave_at is not None:
        raise CommsError("Give 'status' or a leave time, not both.")
    state_file(root).parent.mkdir(parents=True, exist_ok=True)
    with file_lock(state_file(root), timeout=5):
        state = read(root)
        touched = False
        if leave_at is not _UNSET and leave_at is not None:
            if is_away(state, now):
                raise CommsError("The user is already away. Set status='available' first if "
                                 "they are back and will leave again later.")
            touched = state["leave_at_epoch"] != leave_at
            state["leave_at_epoch"] = leave_at
        elif leave_at is None:
            if state["leave_at_epoch"] is None or is_away(state, now):
                raise CommsError("There is no pending leave time to cancel.")
            state["leave_at_epoch"] = None
            touched = True
        if heads_up_seconds is not None and heads_up_seconds != state["heads_up_seconds"]:
            state["heads_up_seconds"] = heads_up_seconds
            touched = True
        away_now = is_away(state, now)
        if status is not None and ((status == AWAY) != away_now
                                   or state["leave_at_epoch"] is not None and not away_now):
            state.update(status=status, status_epoch=now, leave_at_epoch=None)
            touched = True
        if away_behaviour == state["away_behaviour"]:
            away_behaviour = None
        if touched:
            state.update(seq=state["seq"] + 1, rev=_new_rev(), updated_epoch=now, updated_by=by)
        if away_behaviour is not None:
            state.update(away_behaviour=away_behaviour, behaviour_by=by,
                         behaviour_seq=state["behaviour_seq"] + 1, behaviour_rev=_new_rev())
        if touched or away_behaviour is not None:
            _write(root, state)
        return state


_STATUS_KEYS = ("seq", "rev", "status", "status_epoch", "updated_epoch", "updated_by",
                "leave_at_epoch", "heads_up_seconds")
_BEHAVIOUR_KEYS = ("away_behaviour", "behaviour_seq", "behaviour_rev", "behaviour_by")


def newer(incoming, held):
    return (incoming["seq"], incoming["rev"]) > (held["seq"], held["rev"])


def merge(root, incoming, behaviour=True):
    """Fold a copy from another machine or a mailbox into this one. True if it changed."""
    incoming = clean(incoming)
    if incoming is None or max(incoming["seq"], incoming["behaviour_seq"]) > MAX_INCOMING_SEQ:
        return False
    if not state_file(root).parent.is_dir():
        return False
    held = read(root)
    take_status = newer(incoming, held)
    take_behaviour = behaviour and ((incoming["behaviour_seq"], incoming["behaviour_rev"])
                                    > (held["behaviour_seq"], held["behaviour_rev"]))
    if not (take_status or take_behaviour):
        return False
    with file_lock_optional(state_file(root), timeout=2) as locked:
        if not locked:
            return False
        held = read(root)
        changed = False
        if newer(incoming, held):
            held.update({k: incoming[k] for k in _STATUS_KEYS})
            changed = True
        if behaviour and ((incoming["behaviour_seq"], incoming["behaviour_rev"])
                          > (held["behaviour_seq"], held["behaviour_rev"])):
            held.update({k: incoming[k] for k in _BEHAVIOUR_KEYS})
            changed = True
        if changed:
            _write(root, held)
        return changed


def wire(state, machine):
    """The state as sent to another machine or a mailbox, names qualified with ``machine``."""
    out = dict(state)
    for key in ("updated_by", "behaviour_by"):
        if out[key] and "@" not in out[key] and machine:
            out[key] = f"{out[key]}@{machine}"
    return out


def from_wire(raw, machine):
    """A received state with this machine's own agents named plainly again."""
    state = clean(raw)
    if state is None:
        return None
    suffix = f"@{machine}" if machine else None
    for key in ("updated_by", "behaviour_by"):
        if suffix and state[key].endswith(suffix):
            state[key] = state[key][:-len(suffix)]
    return state


def clock_text(epoch, now):
    try:
        when, today = datetime.fromtimestamp(epoch), datetime.fromtimestamp(now)
    except (OverflowError, OSError, ValueError):
        return "an unknown time"
    return when.strftime("%H:%M") if when.date() == today.date() else when.strftime("%Y-%m-%d %H:%M")


def minutes_text(seconds):
    minutes = max(0, int(round(seconds / 60)))
    if minutes < 1:
        return "under a minute"
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"


def _by(name):
    return f" (set by {name})" if name else ""


def behaviour_text(state):
    return (f"While away: «{state['away_behaviour']}»"
            f"{_by(state['behaviour_by'])}. This never widens what you are permitted to do.")


def status_line(state, now):
    """One line saying where things stand, for teammate_register and the tool."""
    if is_away(state, now):
        since = away_since(state, now)
        when = f" since {clock_text(since, now)}" if since else ""
        return (f"Your user is AWAY{when}{_by(state['updated_by'])}: do not ask them "
                f"questions until they are back. {behaviour_text(state)}")
    leave = state["leave_at_epoch"]
    if leave is not None:
        return (f"Your user is available and leaves at {clock_text(leave, now)}, in "
                f"{minutes_text(leave - now)}{_by(state['updated_by'])}. Ask anything that "
                f"needs them before then. {behaviour_text(state)}")
    return "Your user is available."


def notice(state, seen, now):
    """``(text, urgent)`` for what an agent last told ``seen`` should now hear, or None."""
    current = snapshot(state, now)
    seen = seen if isinstance(seen, dict) else {}
    same_status = (seen.get("rev"), seen.get("phase")) == (current["rev"], current["phase"])
    engaged = current["phase"] != AVAILABLE or current["leave"]
    if same_status:
        if seen.get("behaviour_rev") == current["behaviour_rev"] or not engaged:
            return None
        return (f"The away behaviour changed. {behaviour_text(state)}", False)
    leave = state["leave_at_epoch"]
    if current["phase"] == AWAY:
        since = away_since(state, now)
        when = f" since {clock_text(since, now)}" if since else ""
        return (f"Your user is AWAY{when}{_by(state['updated_by'])}. Do not ask them "
                f"questions until they are back. {behaviour_text(state)}", True)
    if current["phase"] == HEADS_UP:
        return (f"Your user leaves at {clock_text(leave, now)}, in "
                f"{minutes_text(leave - now)}{_by(state['updated_by'])}. Ask anything that "
                f"needs them now. {behaviour_text(state)}", True)
    if leave is not None:
        lead = minutes_text(state["heads_up_seconds"])
        return (f"Your user plans to leave at {clock_text(leave, now)}, in "
                f"{minutes_text(leave - now)}{_by(state['updated_by'])}. You will get a "
                f"heads-up {lead} before. {behaviour_text(state)}", False)
    if seen.get("phase") == AWAY:
        return (f"Your user is available again{_by(state['updated_by'])}. If anything is "
                f"waiting on them, a question, a decision or an approval, put it to them "
                f"now in your own session. If you need nothing from them, say nothing.", False)
    if seen.get("leave") or seen.get("phase") == HEADS_UP:
        return (f"The leave time was cancelled{_by(state['updated_by'])}. Your user is "
                f"available.", False)
    return None


def _notice_key(current):
    return f"{current['rev']}:{current['phase']}:{current['behaviour_rev']}"


def deliver(root, team, agent, text, urgent, key):
    """Put one notice in ``agent``'s own unread inbox. False if the inbox was busy."""
    from . import tools

    inboxes = get_inboxes_dir(root, team)
    ensure_inbox(inboxes, agent)
    unread_file, read_file = inboxes / f"{agent}_unread.json", inboxes / f"{agent}_read.json"
    with file_lock_optional(unread_file, timeout=NOTICE_LOCK_SECONDS) as locked:
        if not locked:
            return False
        messages = read_json_safe(unread_file)
        earlier = messages + (read_json_readonly(read_file) or [])[-50:]
        if any(isinstance(m, dict) and m.get("notice") == key for m in earlier):
            return True
        messages.append({
            "id": new_message_id(root), "ts": message_ts(), "from": STATUS_SENDER,
            "priority": "urgent" if urgent else "normal", "notice": key,
            "message": f"{text} Do not reply to this message; teammate_user_status shows "
                       f"the current state."})
        write_json_atomic(unread_file, tools._cap_unread(messages, read_file))
    return True


def check(root, team, agent, record, now=None):
    """Called by a live session's watcher: tell its own agent anything it has not heard."""
    now = time.time() if now is None else now
    state = read(root)
    current = snapshot(state, now)
    seen = (record or {}).get(SEEN_FIELD)
    if seen == current:
        return False
    told = False
    if isinstance(seen, dict):
        due = notice(state, seen, now)
        if due:
            if not deliver(root, team, agent, due[0], due[1], _notice_key(current)):
                return False
            told = True
    write_agent_record(root, team, agent, timeout=1, **{SEEN_FIELD: current})
    return told
