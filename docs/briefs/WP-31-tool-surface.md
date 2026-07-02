# WP-31 — Tool-surface polish; shared message renderer; group open-read disclosure

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: N7, N9 (low-medium), N10 (medium), N6 (medium) —
> `docs/260701-fable-audit-consumer.md`. Cognition tasks: `e8fa4a45a4fa`, `21477b1b6530`,
> `0461f5dfab9d`.

## Findings being fixed

- **N7:** `@mention` syntax exists only in SKILL.md — invisible at the schema level where a
  zero-context agent actually reads.
- **N9:** inconsistent required-param errors ("'message' is required…" vs "Invalid agent name
  None…"); 64KB message cap undisclosed; `project` field description says "at registration"
  even inside teammate_update's schema; group prose lists 7 of 10 actions.
- **N10:** inbox and group history render near-identical blocks with different field
  order/tags — no shared renderer; ad-hoc grammars per tool.
- **N6:** group descriptions imply Slack-style privacy; reads are open to any registered
  teammate and nothing says so.

## Direction

1. **N7:** `teammate_send`'s `message` description gains: "@name in a group post mentions a
   member (they get a 🔔 flag in their wake and inbox)." Same one-liner in `teammate_group`'s
   description. Keep it to one sentence each (the WP-11b leanness decision stands — this is
   invocable behavior, not guidance prose).
2. **N9:**
   - `validate_agent_name` / `validate_group_name`: when the value is None/empty, the error
     reads "'<param>' is required (a teammate name)" — add an optional `param=` argument
     (default keeps current wording for non-None bad values). Callers that validate a
     specific arg pass its name (`to`, `agent`, `group`, `members[i]`…). Grep every call
     site; only the None/missing branch changes wording.
   - `message` schema description discloses the cap: "Max 64 KB."
   - `_PROFILE_DESCRIPTIONS["project"]`: reword context-neutral ("your working project —
     auto-filled from the project directory when you register; set to override").
   - `teammate_group` description names all 10 actions.
3. **N10 shared renderer:** extract `_format_message_block(msg, *, agent=None,
   reactions=None)` producing the `--- id | from [tags] ---` header + reply-line + body +
   reactions line, used by BOTH inbox and group history. Field order/tags unify on the inbox's
   current form (it's the richer one: group/post-type/urgent/mention flags). CONSTRAINT: the
   exact substrings existing tests assert must keep working — notably "1 unread message(s)"
   (harness line ~2105), the "--- id: " block prefix, "[URGENT]", "🔔(@you)", "reactions: ".
   Run the full harness early and often here. teammate_whoami stays JSON (deliberate).
4. **N6:** `teammate_group` description gains: "Groups are open: any registered teammate can
   join, post, and read history — do not post secrets expecting privacy." (Documenting is the
   fix; gating reads on membership would contradict the recorded open-membership design.)

## Acceptance criteria

- AC-1: schema-level greps — "@" mention hint present in send+group descriptions; "64 KB" in
  message description; all 10 actions named; no "at registration" wording in the update
  schema's project description.
- AC-2: `teammate_send` with a missing `to` and `teammate_group` with a missing `group` both
  produce "'<param>' is required" errors (behavior test); a BAD non-empty name keeps the
  current "Invalid agent name …" wording.
- AC-3: inbox and group history render byte-identical block shapes for the same message
  (hermetic: same record through both paths, compare the block lines ignoring the
  header/footer that legitimately differ).
- AC-4: existing harness output assertions untouched and green (the renderer must be a
  refactor, not a reformat).
- AC-5: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- WP-11b schema-leanness (verbose guidance lives in SKILL.md) — one-liners only.
- Open membership + open read (document, never gate).
- The ack "all" schema explanation is the current gold standard — don't touch it.
- whoami's JSON output stays JSON.
