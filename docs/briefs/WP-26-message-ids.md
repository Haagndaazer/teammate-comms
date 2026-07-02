# WP-26 — Message-id uniqueness across writers; cross-host id docs; react 3-tier resolution

> Owner: Svetlana. Gate: Silvie. Branch: `fix/fable-audit-260701`.
> Audit findings: B3 (medium, Stage-2), C4 (medium-high, Stage-1), M4/N8 (medium).
> Cognition tasks: `399bc7d3ce75`, `e3288a850c64`, `5d7b245c71f6`.

## Findings being fixed

- **B3:** ids are bare microsecond timestamps; two writers stamping the same microsecond
  produce two records with ONE id — the dashboard's global per-id dedup silently never
  renders one of them, and every id-keyed consumer (ack, react, delete, cursors) is ambiguous.
- **C4:** naive-local-time ids are ordered-by-clock across hosts in the documented cross-host
  shared-root mode — modest skew silently skips messages past advanced cursors. The skew
  itself is unfixable client-side; the honest move is a per-writer disambiguator + documented
  limits.
- **M4/N8:** `react()` resolves ids transcript-only, so with TEAMMATE_TRANSCRIPT=0 or a
  rotated-away id the author is never woken — while `delete` on the same id works via its
  3-tier fallback. Worse (N8): reacting to a NONEXISTENT id returns a success string.

## Direction

1. **B3 — `new_message_id()` in comms.py:** returns
   `now_timestamp() + "." + <disambiguator>` where the disambiguator is compact and
   per-writer-unique, e.g. `f"{os.getpid():x}"` plus a module-level monotonic counter
   (`itertools.count()`) rendered base-36 or hex — pid alone is not enough (same process,
   same microsecond is exactly the collision the audit's `_window` docstring dismisses;
   pid+counter kills both same-process and cross-process duplicates; hostname is NOT
   included — too long, and pid+timestamp collision across hosts within one microsecond is
   acceptable residual, say so in the docstring). USE it at: DM send, group send, reaction
   append, deletion append (the two appenders stamp under their locks today — keep
   stamp-under-lock placement, just call new_message_id() instead of now_timestamp()).
   Heartbeats/registration timestamps stay bare `now_timestamp()` (they're parsed with
   strptime — verified the ONLY strptime consumer is `is_channel_alive` on lastHeartbeat).
   Lexical ordering is preserved: equal timestamp-prefix → suffixed id sorts after bare, and
   all comparisons in the codebase are `>=`/`max` on strings. Frontend `fmtWhen` regex and
   read-receipt `pos >= rec.id` tolerate the suffix (verified in plan review).
2. **C4 — document, don't fix skew:** one paragraph in DESIGN §storage (land it in THIS WP,
   not WP-32 — docs ride the behavior): ids order by each writer's local clock; cross-host
   ordering is only as good as clock sync; the disambiguator guarantees UNIQUENESS, not
   global order; NTP-synced hosts are the supported envelope.
3. **M4/N8 — react resolves like delete:** `react()` replaces `_scan_transcript_for_id` with
   `resolve_message()` (the 3-tier: transcript tail→full, group files, inboxes). On a clean
   miss (None): raise CommsError "No message with id X found" — matching ack/delete's error
   idiom. `target_from` comes from the resolved record's `from`. Reacting to a TOMBSTONED
   message must still work and still wake the author (id is kept by tombstones —
   known-intentional S4).

## Acceptance criteria

- AC-1: 1000 ids minted in a tight loop are all unique and lexically non-decreasing;
  a DM record + its transcript tee + a reaction targeting it all carry/reference consistent
  ids end-to-end through inbox → react → group history rendering.
- AC-2: two send_dm calls monkeypatched to the SAME now_timestamp() value produce distinct
  ids (the B3 scenario — tautology: identical on current main).
- AC-3 (M4): with TEAMMATE_TRANSCRIPT=0, reacting to a group post resolves target_from via
  the group-file fallback and the reaction records it (wake path testable via the record's
  target_from field). Tautology: current main records no target_from there.
- AC-4 (N8): reacting to "totally-bogus-id" raises; error names the id. Existing harness
  react flows (ids 47+) stay green.
- AC-5: WP-7 P3/P4 cursor tests + `_window` collision-group tests stay green untouched (the
  suffix only makes ids MORE unique — strictly fewer boundary collisions).
- AC-6: full harness green (three suites) on Windows.

## Known-intentional — do NOT "fix"

- Reacting to a deleted message wakes its author (S4) — preserve.
- `_window`'s same-id boundary-swallow logic stays (still correct, now nearly-unreachable).
- Do NOT migrate/rewrite existing stored ids — old bare ids and new suffixed ids coexist
  (lexical compare handles the mix; note it in the new_message_id docstring).
- ack's id equality match: an OLD bare id remains ack-able (equality on the stored string —
  unaffected).
