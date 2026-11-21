# User availability status: implementation plan (0.20.0)

Backlog task 95f3b2135776. Peer reviewed before building; sections 7 and 8 record the
user's rulings and what the review changed.

## 1. What the user asked for

1. A system-wide status for the user, **available** or **away**. Agents on comms are alerted
   when it changes.
2. A **leave-at time** an agent can set. Before it, agents get a heads-up to wrap up anything
   that needs the user. At that time the status becomes away by itself.
3. A simple way for an agent to ask whether the user is available for questions.
4. An **away behaviour** the user chooses: "work autonomously" (default), "wait", or any
   freeform instruction. The user changes it by asking an agent.
5. Main use: one agent, for example a cloud agent on Rakazo that watches the user's calendar,
   sets the leave-at time. Local agents then hear both the heads-up and the departure.

## 2. State

One file per comms root, `TeammateComms/availability.json`:

```
{"v": 1, "seq": 7, "updated_epoch": 1795000000.0, "updated_by": "Sylvie@desktop",
 "status": "available" | "away",
 "leave_at_epoch": 1795003600.0 | null,
 "heads_up_seconds": 600,
 "away_behaviour": "work autonomously",
 "behaviour_seq": 3, "behaviour_by": "Sylvie@desktop"}
```

The **effective status is computed, never flipped by a timer**: away if `status == "away"`,
or if `leave_at_epoch` is set and has passed. `away_since` is derived the same way. A missing
or unreadable file means available with the default behaviour.

`seq` is a Lamport counter: a writer sets `seq = max(every seq it has seen) + 1`. Two copies
are ordered by `(seq, updated_by)`, so a machine whose clock runs ahead cannot keep winning.
The behaviour text has its own counter because it merges separately (section 5).

## 3. Notices to local agents

No scheduler: the relay only runs when a machine has links. Each live session's watcher
thread (`channel.run_watcher`), on its 5-second heartbeat tick and only after its inbox
baseline is seeded, compares the state with the snapshot its agent was last told about,
kept as the field `availability_seen` on the agent's own record:

```
{"seq": 7, "phase": "available" | "heads_up" | "away"}
```

`phase` is what the state means right now: `away` if effectively away, `heads_up` if a
leave time is set and now is inside the heads-up window, else `available`. A notice is due
when `(seq, phase)` differs from the snapshot. One notice describes the whole current
situation, so stages that piled up collapse into one and a changed or cancelled leave time
is always stated outright:

- new seq, phase available, leave time set: "your user plans to leave at HH:MM (in N min)"
- new seq, phase available, no leave time, previous snapshot had one or was away:
  "leave time cancelled" / "your user is available again"
- phase heads_up: "your user leaves at HH:MM, in N min. Ask anything that needs them now."
- phase away: "your user is away since HH:MM: no more questions until they are back.
  While away: «text» (set by X)."

Every notice names who set the state and ends "do not reply to this message". A change of
the away behaviour alone is announced only while the user is away or a leave time is set.

Delivery: the watcher writes one record straight into its own agent's unread inbox, from
the reserved system sender `comms-status`, with the deterministic id
`<utc stamp of updated_epoch>.av<seq><phase initial>`, skipping the transcript and the
liveness probe. It uses a short lock timeout; on contention it retries next tick. The
snapshot is written after the record, and a record whose id is already in the unread or
read inbox is not written twice, so a crash between the two steps cannot duplicate or lose
a notice. The normal wake then fires on every harness.

At registration and re-registration the snapshot is set to the current `(seq, phase)` and
`teammate_register` prints the status line, including any pending leave time, so a fresh
or compacted session knows the state without an old notice waking it.

`type == "human"` records and sessions with no watcher get no notices.

## 4. Tool

`teammate_user_status`:

- no arguments: effective status, away since, leave time (local time and minutes left),
  away behaviour, who set each and when. The "easy check".
- `status`: `"away"` or `"available"`. Either clears a pending leave time.
- `leave_at`: ISO 8601 with offset (a time without offset is refused), or `in_minutes`
  (whole number, 1 to 10080). Must be in the future, at most 7 days out. Refused while
  already away. `leave_at: ""` cancels. `heads_up_minutes`: 0 to 120, default 10.
- `away_behaviour`: `"work autonomously"`, `"wait"`, or freeform, one line, 500 chars,
  control characters stripped.

Any registered agent may set it, since the user changes it by asking an agent. The tool's
description says to set it only when the user asked.

## 5. Other machines and the mailbox

The state rides in data both sides already exchange; there is no new event kind.

- **Peers:** `build_roster` includes the state; the receiving relay merges it. Rosters go
  every exchange (about 10 s), an old peer ignores the extra key, and a machine that was off
  catches up at first contact. A machine with no links has nothing to sync.
- **Merge rule:** take the incoming status fields if its `(seq, updated_by)` is larger; take
  the incoming behaviour if its `(behaviour_seq, behaviour_by)` is larger. Writing the file
  is what the local watchers notice.
- **Mailbox:** each `/pc/sync` body carries the state. The Worker keeps the largest by the
  same rule and returns its copy; the relay merges the status fields from it, **never the
  behaviour**. The mailbox is one more replica, so a cloud change needs no request queue,
  ids or expiry.

## 6. Cloud agents

Worker 1.2.0; `MIN_WORKER` stays 1.1.0, an older Worker just lacks the tools.

- An account marked `"can_set_status": true` in the user's mailbox config (default off)
  gets two more tools. The gate is per account because bots on one connection can use each
  other's names.
  - `user_status(me?)`: status, away since, leave time, away behaviour.
  - `user_set_status(me?, status | leave_at | in_minutes | cancel, heads_up_minutes?)`:
    the Worker resolves `in_minutes` on its own clock, bumps `seq`, stamps
    `updated_by = Name@<account label>`; machines pick it up at their next sync (10 s idle).
- Accounts without the flag get `user_status` only.
- A cloud agent can never set the away behaviour, and there is no free-text field it can
  write into a notice.
- Cloud agents are not sent notices; they read the status when they run.

## 7. User rulings (2026-11-20)

1. Cloud agents set status per connection, opt-in; any local agent may set it when asked.
2. Cloud agents need no notices: they are always-on and do not stop when the user is away.
3. Status returns to available only by asking an agent. Notices and the tool show
   "away since" so a stale away is visible.

## 8. Changed after peer review

Lamport ordering instead of clock time; state carried in the roster instead of a new event
kind and feature negotiation; seen state on the agent record instead of a new file; one
situation notice instead of per-stage flags; deterministic notice ids; notices only after
the inbox baseline is seeded; direct inbox write without transcript; reserved sender in the
three places names are reserved; per-account gate; no `note` field; the mailbox as a replica
instead of a change-request queue; register line shows a pending leave time.

## 9. Risks kept

- A false "away" from a steered cloud agent on an allowed connection. Every notice names
  who set it and any agent can set it back.
- The away behaviour is shown to all agents as the user's wish. It is local-only, one line,
  quoted and attributed, and notices say it never widens what an agent may do.
- Clock skew shifts heads-up and departure by the skew.
- A machine with a wrong clock computes the wrong effective status.
- A change made on one copy in the seconds before it hears of a change made elsewhere can
  lose to it: both carry the same or a lower counter. Copies sync about every 10 seconds;
  the agent whose change lost is told by the next notice what the status now is.

## 10. Work packages

1. `availability.py`: state, validation, merge, phase, notice text (pure, tested).
2. Tool, register line, reserved sender.
3. Watcher integration.
4. Relay: roster carry and merge; mailbox sync carry and merge.
5. Worker 1.2.0, `make_config.py` flag.
6. Docs: README, guide, SKILL, DESIGN, CHANGELOG, whats-new.
7. Tests: `tests/test_availability.py`, additions to mailbox and Worker suites, CI.

Not in this release: a dashboard control, an automatic return time, per-project status.
