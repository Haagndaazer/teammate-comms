# WP-13 — Project profiles (v0.9.0)

**Manager:** Silvie · **Implementer:** Svetlana · **Status:** drafted, pending Colton's go
**Branch:** `feat/wp13-project-profiles` (off `main`)

---

## 1. Goal

Add a first-class **project profile** layer on top of the existing per-agent `project` field, so an
agent can discover *which projects are being worked on* and *who to ask for help* — without that
information being fragmented across teammate profiles.

Today `project` is a free-text, 100-char per-agent string auto-filled from `$CLAUDE_PROJECT_DIR`
as a `parent/name` label. There is no project entity. This WP adds one: a canonical record keyed by
that same label, holding the descriptive metadata, with the member roster **derived live** (never
stored).

---

## 2. Locked decisions (do NOT re-litigate — see §7 known-intentional)

1. **Membership is DERIVED, never stored.** The roster on a project profile is computed live by
   grouping agent records whose normalized `project` equals the project key, excluding `type=="human"`.
   The profile JSON stores only descriptive metadata.
2. **Project key = the existing project label.** A profile is keyed by the same string agents already
   carry (e.g. `projects/teammate-comms`), normalized. Zero migration; existing agents auto-link.
3. **Creation is explicit** via a `project_register` tool.
4. **No hard edit gate — advisory only.** Any agent *can* create/edit/delete any project profile.
   The tool descriptions instruct agents to only modify the profile for *their own* project directory
   unless the user requests otherwise. (Membership is self-reported anyway; a hard gate would be
   security theatre and is explicitly out of scope.)
5. **Version → 0.9.0** (new feature, minor bump).

---

## 3. Data model

New cap dict in `comms.py`, mirroring `PROFILE_FIELDS` (lines 40-46):

```python
PROJECT_FIELDS = {
    "summary":     80,   # one-liner shown in list_projects — keep terse
    "description": 600,  # short paragraph
    "tech_stack":  400,  # single-line, comma-separated
    "repo_url":    200,  # optional remote URL
    "name":        100,  # human-friendly display name; defaults to the key if omitted
    # status handled separately — enum, not a free-text cap (see below)
}
PROJECT_STATUS = ("active", "paused", "archived")  # default "active"
```

**`path` is NOT in the cap dict — it is uncapped.** It is auto-filled from `$CLAUDE_PROJECT_DIR`
(machine-supplied, trusted) and a length cap would only risk truncating a legitimate deep path.
Still validate it is a string and whitespace-collapse to a single line (so it can't break the list /
dashboard layout), but apply no length limit.

Stored record (`projects/<slug>.json`), keys:
- `key` — the canonical **normalized** project key (source of truth for matching).
- profile fields above + `status`.
- `created_by`, `created_at`, `updated_by`, `updated_at` — provenance/staleness (agent name + ISO timestamp).

**Dropped from the original sketch:** `lead` (agent-name field). Storing a name reintroduces the exact
staleness we eliminated by deriving the roster — names have no rename op and no cleanup on deregister.
If lead-designation is ever needed it's a follow-up, not v1.

---

## 4. Key normalization — the load-bearing correctness fix

`validate_project_key(value)` in `comms.py` (new, modeled on `validate_group_name` line 115). It MUST,
in order:
1. Trim + collapse internal whitespace (as `validate_profile_field` does).
2. **Replace `\` → `/`** so Windows (`projects\teammate-comms`) and Unix (`projects/teammate-comms`)
   auto-fills converge. **This is the bug that would silently split a roster across OSes.**
3. **Lower-case fold** for the stored/compared key, so `Projects/X` and `projects/x` are one project.
4. Reject any character that is filesystem-unsafe or case-ambiguous after folding (no `:`, `*`, `?`,
   `"`, `<`, `>`, `|`, control chars). Collapse repeated `/`.
5. Length-cap at 100 (same as the `project` profile field).

Because the normalized key is injective into the slug, `key → filename` cannot collide. The slug is a
deterministic encoding of the normalized key (e.g. `/` → a safe separator); the canonical key is also
stored inside the JSON and re-checked on read.

**Roster derivation MUST normalize each agent's `project` through the same function before comparing**
— otherwise the cross-OS / case mismatch reappears at read time. This is the single most important
correctness requirement in the WP.

`validate_project_field(name, value)` — mirrors `validate_profile_field`; for `status`, validate
against `PROJECT_STATUS` and default to `active`.

---

## 5. Storage helpers (`comms.py`, mirror the agents/groups patterns)

- `get_projects_dir(root, team)` — **team-scoped**, under `TeammateComms/<team>/projects/` exactly like
  `get_agents_dir`/`get_groups_dir` (lines 182-200). NOT a flat global dir.
- `project_key_to_slug(key)` / read/list/remove helpers.
- `write_project_record(root, team, key, **fields)` — merge-upsert. **Use a BLOCKING lock**
  (the `append_reaction` pattern, `comms.py` ~1019-1045), NOT `file_lock_optional`. Two simultaneous
  first-creates must not clobber each other and silently report success. Raise `CommsError` on lock
  failure so the caller can retry. Omitted field = leave unchanged; explicit `""` = clear.

---

## 6. Tools (4 new → 17 total; append to `TOOL_DEFINITIONS` + `_HANDLERS` in `tools.py`)

Reuse `_profile_schema_properties`-style schema generation from `PROJECT_FIELDS`.

### 6.1 `project_register`
Create or merge a project profile (upsert).
- `key` (optional) — defaults to the caller's normalized `project`. If passed, normalized too.
- All `PROJECT_FIELDS` + `status`. `path` auto-fills from `$CLAUDE_PROJECT_DIR` when omitted and the
  record has none yet.
- Stamps `created_by/at` on create, `updated_by/at` on every write.
- **Description must say:** "Define/update the profile for a project. By convention only register or
  edit the project matching your own working directory, unless the user asks you to document another."

### 6.2 `list_projects`
Global (within team). **Concise by design — exactly three things per project:** display `name`, the
**live teammate roster** (agent names, derived, humans excluded), and the `summary` one-liner. Nothing
else. All other fields (description, tech_stack, path, repo_url, status, provenance) are retrieved via a
follow-up `project_profile` call — same list-then-detail pattern as `teammate_list` → `teammate_profile`.
- After the per-project list, append a small aggregate **discovery note** (not per-project detail):
  project labels that agents currently carry but have **no profile yet** (so undocumented active
  projects are still discoverable), and a **near-miss note** — agents whose `project` matches a
  registered key only after normalization (surfaces misfiled agents instead of silently dropping them).

### 6.3 `project_profile`
Full detail for one project (`key` optional, defaults to caller's). All stored fields + provenance +
the **live-derived roster** (agent name, role, status, liveness), humans excluded.

### 6.4 `project_delete`
Remove a profile by `key`. Advisory convention same as register (description-guided, no hard gate).
Mirror `teammate_delete` / `delete_group` semantics for the file removal.

---

## 7. Known-intentional — do NOT "fix" these

- **Global-by-default comms** and `project` auto-fill from `$CLAUDE_PROJECT_DIR` — unchanged. We layer on
  top; we do not migrate or rename existing agent `project` fields.
- **The free-text `project` field stays.** Project profiles are additive, not a replacement.
- **`file_lock_optional` for heartbeats is intentional** — do not "upgrade" it. The blocking lock is for
  project writes only.
- **`personality` excluded from `teammate_list`** — do not add it anywhere.
- **Membership is advisory / self-reported** — do not add hard enforcement or identity verification.
- **The inbox NEVER-MISS invariant** (`_prev_seen` captured before `set_last_seen`) — do not touch.
- **Records with no `type` key** fall back to `"unknown"` and DO appear in the roster — that's the same
  behavior as `teammate_list` (`tools.py` line 700); only `type=="human"` is excluded.

---

## 8. Dashboard (single source of truth — no parallel grouping)

- `dashboard.py` `_api_conversations` (~line 210): include each registered project's profile metadata
  (`summary`, `status`, display `name`) keyed by normalized project key in the JSON payload. The roster
  rows already carry `project` (lines 222-226); ensure the key is normalized to match.
- `static/index.html` `renderNav` (~lines 262-282): the client **already groups the roster by project**.
  Enrich the *existing* `byProject` subheads with the profile's `summary`/`status` when a profile exists —
  do not introduce a second grouping structure. Group key must use the normalized key so cross-OS/case
  agents land in one group.

---

## 9. Acceptance criteria (pre-committed; the gate checks these, not re-derived correctness)

1. `project_register` then `project_profile` round-trips all fields; `status` rejects values outside
   `PROJECT_STATUS`; over-cap fields are truncated per `PROJECT_FIELDS`.
2. **Cross-OS/case key convergence:** an agent with `project="Projects\\Foo"` and another with
   `project="projects/foo"` both appear in the same project's derived roster, and both
   `teammate_list`-grouping and the dashboard group them together. *(Test under the actual mismatch —
   not a clean single-spelling case.)*
3. **Humans excluded:** a `type=="human"` record carrying a matching `project` never appears in the roster.
4. **Concurrent create:** two near-simultaneous `project_register` calls for two *distinct* keys that
   would slug-collide cannot occur (normalization makes the slug injective); two calls for the *same*
   new key do not lose a field — last-writer merges, neither silently drops. Demonstrate the blocking
   lock by holding it and showing the second waits/raises rather than clobbering.
5. `list_projects` shows exactly name + live teammate roster + summary per project (no other fields),
   and the trailing aggregate surfaces undocumented project labels and flags near-miss agents.
6. `project_delete` removes the file; `list_projects`/`project_profile` reflect the removal.
7. Dashboard renders one group per normalized project with the profile one-liner; no duplicate/parallel
   grouping when a profile exists vs. when only agents carry the key.
8. **Tautology guard:** each behavioral test must fail against the reverted code (assert the specific
   reason, not just "it threw").
9. Full existing test suite stays green. **Fix + proof land in the same commit.**

---

## 10. Docs / version (same PR)

- Bump `0.8.2 → 0.9.0` in: `src/teammate_comms/__init__.py` (line 3, canonical), `.claude-plugin/plugin.json`
  (line 3), `pyproject.toml` (line 3).
- `CHANGELOG.md` — new `## v0.9.0` section (Added: project profiles + 4 tools).
- `skills/teammate-comms/SKILL.md` — document the project_* tools + the "edit only your own project"
  convention (keep schema descriptions one-line per the leanness rule; verbose guidance goes in SKILL.md).
- `DESIGN.md` + `README.md` — project-profiles section + updated tool count (13 → 17).

---

## 11. Out of scope (v1)

- Renaming a project key / migrating agents between keys.
- Any hard membership enforcement or identity verification.
- A `lead`/owner field (dropped — staleness).
- Indexing for `list_projects` (the O(N) agent scan matches existing `teammate_list`; acceptable at
  current scale — note it in DESIGN.md as a known trade-off).
