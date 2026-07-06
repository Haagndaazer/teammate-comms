"""Broker delivery CLI: ``python -m teammate_comms.deliver`` (WP-38).

The compaction-broker daemon is NOT an MCP client, so its completion/expiry notices
need a way to land as real teammate-comms DMs (as ``compact-broker``) in the requester's
inbox. This is a thin CLI wrapper over ``send_dm`` (tools.py) reused IN-PROCESS — no new
delivery logic, so a fresh process calling it gets every storage guarantee for free (lock,
the 1000-message unread cap, atomic write, transcript tee, and a live recipient's own
channel watcher noticing the change).

Trust boundary: this is a LOCAL CLI, not an MCP tool — anyone who can run it can send as
ANY name (including impersonating another agent). That is the same trust domain as a
broker that can already inject keystrokes into panes; it is NOT an authz surface, and is
deliberately not restricted to the ``compact-broker`` sender (dashboard/human tooling may
reuse it). The MCP-side forgery path — an agent REGISTERING as ``compact-broker`` and
sending look-alike notices in-band — is closed by WP-37's name reservation in
``register_identity``, not here.

Import-clean (no import-time side effects): imported by server.py's plugin-runtime
pointer logic in principle, and must never risk breaking ``python -m teammate_comms.server``.
"""
import argparse
import sys

from .comms import CommsError, resolve_comms_root, validate_agent_name
from .tools import send_dm

DEFAULT_SENDER = "compact-broker"
_POST_TYPES = ("decision", "blocker", "fyi", "chatter")
_PRIORITIES = ("normal", "urgent")


def _read_stdin_message():
    """Read the message body from stdin. Mandatory ``utf-8-sig`` decode (peer-review
    finding): text-mode ``sys.stdin.read()`` decodes with the locale code page on Windows
    and turns a BOM'd UTF-8 body into mojibake — same trap, same fix, as server.py's own
    stdin read (``line = raw.decode("utf-8-sig", ...)``, server.py:584)."""
    return sys.stdin.buffer.read().decode("utf-8-sig")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m teammate_comms.deliver",
        description=(
            "Deliver a teammate-comms DM from a non-MCP process (the compaction-broker "
            "daemon or similar local tooling)."
        ),
    )
    parser.add_argument("--to", required=True, help="Recipient agent name.")
    parser.add_argument(
        "--message", default=None,
        help="Message body. If omitted, read from stdin (utf-8-sig) — the escape hatch "
             "for multi-line bodies PowerShell quoting can't carry safely.",
    )
    parser.add_argument("--sender", default=DEFAULT_SENDER,
                         help=f"Sender name (default {DEFAULT_SENDER!r}).")
    parser.add_argument("--priority", choices=_PRIORITIES, default="normal")
    parser.add_argument("--post-type", dest="post_type", choices=_POST_TYPES, default=None)
    parser.add_argument("--reply-to", dest="reply_to", default=None,
                         help="Id of the message this replies to (a threading hint).")
    parser.add_argument(
        "--comms-dir", dest="comms_dir", default=None,
        help="Comms root. A daemon launched outside the agents' own environment (Task "
             "Scheduler, bare PowerShell) MUST pass this explicitly — otherwise it can "
             "silently resolve a DIFFERENT root than the agents use (TEAMMATE_COMMS_DIR / "
             "CLAUDE_CONFIG_DIR / ~/.claude) and queue a DM into a root nobody reads.",
    )
    parser.add_argument("--team", default=None, help="Optional team namespace.")
    return parser


def main(argv=None):
    # Gate finding, item 5: a CommsError echoing a non-ASCII bad name (e.g. an emoji) would
    # otherwise die with UnicodeEncodeError under a cp1252 Windows console, surfacing as exit 1
    # + a traceback instead of the exit-2 contract the broker relies on (same trap/fix as
    # test_compact.py's own harness — WP-21 gate micro-CR).
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    # argparse itself exits 2 (with its own multi-line usage text) on a parsing error —
    # deliberately NOT overridden (ArgumentParser.error is left alone; do not promise a
    # one-line stderr contract argparse doesn't give us).
    args = parser.parse_args(argv)

    message = args.message
    if message is None:
        message = _read_stdin_message()

    try:
        # Same helper the MCP tool path uses. send_dm validates `to` and the self-send
        # guard (to == sender) internally, but never validates `sender`'s syntax (the tool
        # path's sender is always ctx-resolved and already guaranteed valid) — the CLI's
        # sender is a free-form arg, so it needs its own explicit check.
        validate_agent_name(args.sender, param="sender")
        root, _source = resolve_comms_root(args.comms_dir)
        team = args.team.strip() if isinstance(args.team, str) and args.team.strip() else None
        result = send_dm(root, team, args.sender, args.to, message,
                          priority=args.priority, post_type=args.post_type,
                          reply_to=args.reply_to)
    except CommsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # unexpected — never a silent broker-side hang
        print(f"unexpected error: {e}", file=sys.stderr)
        return 1

    if result["to_type"] is None:
        state = "queued (unregistered recipient)"
    elif result["live"]:
        state = "live"
    else:
        state = "queued"
    print(f"id={result['id']} to={result['to']} state={state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
