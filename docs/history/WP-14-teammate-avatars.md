# WP-14 — Teammate profile images (avatars)

**Manager:** Silvie · **Implementer:** Svetlana (proposed) · **Status:** Peer-reviewed + revised (v2), pending Colton's go
**Branch:** `feat/wp14-teammate-avatars` (off `main`, OR off WP-13 — see §10 sequencing)

---

## 1. Goal

Let a teammate **optionally** attach a small profile image. The image:
- renders in the **dashboard** roster next to the agent, and
- is **discoverable by a Claude Code statusline** as a tiny terminal-rendered thumbnail
  (ANSI color half-blocks, with a monochrome ASCII fallback).

Ingestion is forgiving: any image the agent points at is **forced down to 256×256**, preserving
aspect ratio, **padded with black** if non-square. Default-off; absence changes nothing.

---

## 2. The load-bearing constraint (read first)

**The MCP server is deliberately zero-dependency, pure stdlib** (`pyproject.toml`: `dependencies = []`
— "keeps the MCP spawn instant and avoids a dependency tree blocking the stdio handshake"). This
constraint is non-negotiable and shapes the whole design:

- **No image library on the hot path.** Module-level imports stay stdlib-only. The MCP spawn,
  `teammate_register`, `teammate_list`, `teammate_inbox`, and the dashboard's read/serve paths must
  never import an image library.
- **Image *ingestion* (decode arbitrary format → resize → pad → encode) realistically needs Pillow.**
  Resolution: Pillow is an **optional extra**, imported **lazily inside the set-avatar handler only**.
  Setting an avatar is a rare, heavy, one-time operation; serving and reading are not.
- **Everything downstream of ingest is dependency-free** because ingest **pre-renders and caches**
  every artifact a consumer needs (the PNG, the ANSI strip, the ASCII strip). The dashboard and the
  statusline only ever read already-rendered bytes — no Pillow at read time.

This is the core architectural decision: **separate the heavy ingest path from the light serve path,
and cache everything at ingest.**

---

## 3. Locked decisions (do NOT re-litigate — see §9 known-intentional)

1. **Image bytes are NOT stored in the agent JSON.** A base64 blob inline would bloat every
   `read_agent_record` / `teammate_list` / `/api/conversations` read. Bytes live in a **separate
   team-scoped `avatars/` directory**; the agent record holds only tiny metadata (a content hash +
   timestamp) used for cache-busting and presence.
2. **Pillow is an optional dependency**, lazy-imported only in the set-avatar handler. Missing Pillow
   → a clear `CommsError` ("install `teammate-comms[images]`"), never an import error on spawn.
3. **Pre-render + cache at ingest.** Ingest writes three sidecars per agent: canonical PNG, ANSI
   color strip, ASCII mono strip. Consumers read the sidecar; zero per-render cost, no dep at read.
4. **Input is a filesystem path** (the agent points at an image file on disk). Optional `image_base64`
   accepted as a fallback, but a path avoids burning context tokens on a base64 blob in the tool call.
5. **Default-off, fully optional.** No avatar = current behavior exactly (dashboard shows the status
   dot as today).

---

## 4. Data model & storage

New team-scoped directory, mirroring `get_agents_dir` / `get_groups_dir` (`comms.py` ~182-200):

```
<root>/TeammateComms/[<team>/]avatars/
    <name>.png     # canonical 256×256 RGB, black-padded (served to the dashboard)
    <name>.ansi    # pre-rendered truecolor half-block strip (statusline / in-band profile view)
    <name>.txt     # pre-rendered monochrome ASCII strip (no-color fallback)
```

`get_avatars_dir(root, team)` — new helper, exact mirror of `get_agents_dir`.

Agent record (`agents/<name>.json`) gains ONE small key (written via existing `write_agent_record`
field-merge; **NOT** added to `PROFILE_FIELDS`, which is for capped single-line strings):

```python
record["avatar"] = {"hash": "<sha256-12>", "updated_at": "<iso>"}   # absent = no avatar
```

The hash is the dashboard's cache-buster (ETag + `?v=` query param) and the presence flag.

---

## 5. Ingestion pipeline (the only Pillow-touching code)

New module `avatars.py` (keeps Pillow import isolated from `comms.py`/`tools.py` top level). One entry
point, e.g. `ingest_avatar(root, team, name, *, path=None, image_base64=None)`:

1. **Lazy import**: `try: from PIL import Image ... except ImportError: raise CommsError(<install msg>)`.
2. **Bound the source before decode.** Reject sources over a **50 MB** byte cap *before* handing bytes
   to PIL. Set `Image.MAX_IMAGE_PIXELS` (note: it raises at pixel-*decode* — `.load()`/`.convert()` —
   not at `Image.open()`, so the byte cap is the first line of defense). A malformed deflate/zlib
   CPU-bomb that stays under the pixel cap is an **accepted v1 risk** (ingest is a rare, self-initiated,
   loopback-only op by a trusted local agent — not an attack surface); note it in DESIGN.md rather than
   adding a decode timeout.
3. Convert to RGB. **Scale longest side to 256 preserving aspect; paste centered onto a 256×256 black
   canvas** (the "pad with black if non-square" rule). Re-encode PNG → `<name>.png`.
4. **Render the ANSI strip** from the 256×256 (downsample to the strip's cell grid; emit `▀` U+2580
   with fg = top pixel, bg = bottom pixel per the half-block convention) → `<name>.ansi`. **Quantize to
   the xterm 256-color palette, not 24-bit truecolor** — Claude Code's statusLine documents only basic
   ANSI; 256-color is near-universally supported where truecolor is not guaranteed (§8).
5. **Render the ASCII strip** (luminance ramp, mono) → `<name>.txt`.
6. Compute hash, then `write_agent_record(root, team, name, avatar={...})`. **If it returns `False`
   (lock contested) raise `CommsError`** — never leave sidecars on disk with no `avatar` key in the
   record (a silent broken state: files present, dashboard never requests them, no error surfaced).
7. **`clear=True` order: update the record FIRST** (drop the `avatar` key), *then* delete the three
   sidecars. Record-first means at worst a brief orphaned-file window; file-first would make the
   dashboard request a PNG the record still advertises and get a 404.

**Sidecar writes are binary-atomic.** `write_json_atomic` (comms.py:282) is text-mode/UTF-8 — unusable
for PNG bytes. Add a `write_bytes_atomic(path, data)` helper (binary temp + `os.replace`, mirroring the
json one) and use it for all three sidecars.

**Lazy-import discipline — two levels, the zero-dep invariant depends on BOTH:**
- In `avatars.py`, PIL is imported *only* inside `ingest_avatar` (step 1); the module top stays
  stdlib-only.
- In `tools.py`, import `avatars` **lazily inside `_handle_set_avatar`** (mirror the existing
  `from . import dashboard` lazy import at tools.py ~1403). Do **NOT** add `avatars` to the module-level
  import block — that is the trap that would silently load PIL on spawn if `avatars.py` ever grew a
  top-level non-stdlib import.

Strip dimensions: small, e.g. target ~8 cells wide. Exact cell grid (and how many rows are viable in a
statusline) is pinned by §8 below.

---

## 6. Tools (1 new + 1 enrichment; `tools.py` `TOOL_DEFINITIONS` + `_HANDLERS`)

### 6.1 `teammate_set_avatar` (NEW)
- Args: `path` (image file) **or** `image_base64`; `clear` (bool) to remove.
- Calls `avatars.ingest_avatar(...)`. Returns a confirmation + a text preview of the rendered ASCII
  strip so the agent can see what it produced.
- Description names the convention: set your **own** avatar; one-time, requires the `[images]` extra.
- Kept **separate from `teammate_update`** deliberately: different input shape (binary/path), heavy
  processing, optional dependency. Folding it into `teammate_update` would drag the optional-dep
  surface onto the common profile-edit tool.

### 6.2 `teammate_profile` (ENRICH)
- When the target has an avatar, append the pre-rendered **ASCII strip (`.txt`) only** — never the
  `.ansi` sidecar. Raw ANSI escapes (`\033[38;5;…m▀`) feed straight into the reader's model context as
  unprintable noise/token-waste; the ASCII strip is human/LLM-readable. The `.ansi` sidecar is
  exclusively for the terminal statusline (§8). Cheap — just reads the cached `.txt` file.

No fetch-bytes tool for agents — the dashboard serves the image; agents get the text strip.

---

## 7. Dashboard (`dashboard.py` + `static/index.html`)

### 7.1 New image route — **token in query string, NOT under `/api/`**
`<img>` tags cannot send the `X-Dashboard-Token` header, so the avatar endpoint mirrors how `/`
validates the token from the query string:

- `GET /avatar?name=<name>&token=<token>&v=<hash>` → reads `avatars/<name>.png`, serves
  `Content-Type: image/png`, **`Content-Length: len(bytes)`** (HTTP/1.1 keep-alive hangs the socket on
  the *next* request without it — every existing `_json`/`_html` helper sets it), `ETag: <hash>`,
  `Cache-Control`. 404 when absent.
- **Path-traversal defense is explicit, not implied:** call `validate_agent_name(name)` (comms.py:62 —
  the existing `^[a-zA-Z0-9][a-zA-Z0-9._-]*$` + `".." in name` guard) **before constructing any path**;
  reject **422** on failure. No existing dashboard route validates a query-param name, so an implementer
  who reads "exact stem match" loosely would `join` raw input — state the guard explicitly.
- Add to `do_GET` route table (`dashboard.py` ~158-172) alongside `/` and `/api/*`.

### 7.2 `_api_conversations` (~210-226)
Include `avatar` (the hash, or null) in each roster row so the client knows whether to request an image
and can cache-bust on `?v=<hash>`.

### 7.3 Frontend `navItem` (`index.html` ~296-311) + CSP
- When a roster row has an avatar hash, render a small `<img class="avatar"
  src="/avatar?name=…&token=…&v=…">` in place of / beside the status dot; fall back to the existing
  dot when absent. Online/offline still conveyed (e.g. ring/opacity on the img).
- **Relax CSP** (`index.html` ~10-11): `img-src 'none'` → `img-src 'self'`. (Not `data:` — we serve
  same-origin via the endpoint, which keeps bytes out of the conversations JSON.)

---

## 8. Statusline integration — feasibility CONFIRMED

Confirmed against the Claude Code statusLine docs (claude-code-guide check):
- **Multi-line output is supported** — each `echo`/`print` line renders as its own row, no documented
  row limit. A multi-row half-block avatar is viable (not just a single color bar).
- **ANSI color: basic ANSI is documented; 24-bit truecolor is NOT.** → render the strip in **xterm
  256-color** (near-universal) as the default; truecolor is an optional opt-in, never the default.
- **Unicode block glyphs render as-is** (`▀` etc.).
- **stdin carries session context** — `agent` and `workspace.project_dir` resolve "self"; **`COLUMNS`
  env** gives terminal width; updates debounce ~300ms (cheap file-read fits easily).

Design:
- **CLI surface (dependency-free):** new `avatar` subcommand dispatched at the **top of
  `server.main()`** on `sys.argv[1] == "avatar"`, *before* the MCP loop starts. The sole entry point is
  `teammate-comms = teammate_comms.server:main` (pyproject.toml:15) with **no subcommand router today**,
  so this branch must be added. It reads the cached `.ansi`/`.txt` sidecar and prints to stdout — **no
  Pillow, no network** — fast enough for a 300ms-debounced statusline. Honors `COLUMNS`/`--cols` to
  bound width (truncate, don't wrap). `--format ascii` is the no-color fallback.
- **Name resolution — explicit beats magic.** Primary form is **`--name <RegisteredName>`**: the human
  knows their agent's registered name and puts it in their statusLine config. `--self` is a best-effort
  convenience that reads the statusLine stdin JSON `agent` field and matches it against the registry;
  **there is no guaranteed bijection between the CC session `agent` and the `teammate_register` name**,
  so if `--self` doesn't resolve to a registered avatar it **prints nothing and exits 0** (statusline
  degrades to blank, never errors). `workspace.project_dir` is deliberately NOT used to disambiguate —
  multiple agents share one project dir. Document `--name` as the reliable path.
- **Docs:** ship an example `settings.json` `statusLine` snippet using `--name`, plus the `--self`
  variant noted as best-effort.

---

## 9. Known-intentional — do NOT "fix"

- **Zero runtime deps on the MCP hot path** (§2) — Pillow stays an optional, lazily-imported extra.
- **Image bytes never in the agent JSON** (§3.1) — bytes live in `avatars/`, only a hash in the record.
- **`avatar` is not a `PROFILE_FIELDS` entry** — it's structured metadata, not a capped string; do not
  route it through `validate_profile_field`.
- **The status dot stays** as the no-avatar default; avatars are additive.
- **Avatar endpoint takes token via query** (not header) on purpose — `<img>` can't set headers.
- **`file_lock_optional` for heartbeats stays** — avatar record writes use the existing
  `write_agent_record` merge-write; no new locking scheme.

---

## 10. Sequencing vs WP-13 — REQUIRES A DECISION (Q1)

WP-13 (project profiles, also drafted, version-bumps to **0.9.0**) and WP-14 touch the **same files**
heavily: `comms.py`, `tools.py` (TOOL_DEFINITIONS/_HANDLERS), `dashboard.py` (`_api_conversations` +
routes), `index.html` (renderNav/navItem), the three version files, `CHANGELOG.md`, `SKILL.md`,
`DESIGN.md`, `README.md`. Same implementer (Svetlana). **Parallel branches off `main` would conflict
on nearly every file.** Options:
- **(a, recommended) Land WP-13 first, branch WP-14 off post-WP-13 `main`.** WP-13 is further along.
  WP-14 then bumps **0.10.0** (or 0.9.0 if bundled into the same release).
- **(b) WP-14 first**, pause WP-13.
- **(c) Bundle both** into one 0.9.0 release on a shared integration branch.

**Default carried by this plan: option (a)** — land WP-13 first, branch WP-14 off post-WP-13 `main`,
version **0.10.0**. This is ultimately Colton's priority call (the human gate), but the plan carries (a)
as the recommended default so nothing is left genuinely TBD.

---

## 11. Docs / version (same PR)
- Bump version to **0.10.0** in `src/teammate_comms/__init__.py:3`, `.claude-plugin/plugin.json:3`,
  `pyproject.toml:3` (per §10 default option (a); becomes 0.9.0 only if Colton bundles with WP-13).
- Add the `[project.optional-dependencies] images = ["Pillow>=10"]` extra in `pyproject.toml`.
- `CHANGELOG.md` — new section (Added: profile avatars, `teammate_set_avatar`, dashboard image route,
  statusline CLI).
- `SKILL.md` — document `teammate_set_avatar` + the statusline setup (one-line schema descriptions per
  the leanness rule; verbose guidance in SKILL.md).
- `DESIGN.md` + `README.md` — avatar section, tool count update.

---

## 12. Acceptance criteria (pre-committed; the gate checks these)
1. `teammate_set_avatar(path=<square png>)` → `avatars/<name>.png` is exactly 256×256; record gains
   `avatar.hash`; `teammate_set_avatar(clear=True)` removes all three sidecars + the key.
2. **Non-square padding:** a wide (e.g. 512×128) and a tall (128×512) source both yield a 256×256 PNG
   with the image centered and **black** bars — assert the padded regions are black and aspect is
   preserved (not stretched).
3. **Zero-dep hot path:** importing/spawning the MCP server with Pillow **absent** still works; only
   `teammate_set_avatar` raises a clear install-guidance `CommsError`. (Prove by simulating ImportError.)
4. Dashboard `GET /avatar?name=…&token=…` returns the PNG with correct `Content-Type`/`ETag`; bad/no
   token → 401/403; unknown name → 404; a traversal attempt (`name=../…`) is rejected, not served.
5. `_api_conversations` carries the avatar hash; `navItem` renders the `<img>` when present and the dot
   when absent; CSP allows the same-origin image and nothing more.
6. Statusline CLI prints the cached strip for `--self` with **no Pillow import** and no network.
7. **Tautology guard:** each behavioral test fails against the reverted code (assert the specific
   reason, not just "it threw").
8. Full existing suite stays green. **Fix + proof land in the same commit.**

---

## 13. Out of scope (v1)
- Animated images / non-PNG output formats.
- Avatar history/versioning (latest wins).
- Cropping/positioning UI (centered-pad only).
- Serving avatars to anything but the loopback dashboard + local CLI.
- Identity verification of who an avatar "really" is (same advisory model as the rest of comms).
