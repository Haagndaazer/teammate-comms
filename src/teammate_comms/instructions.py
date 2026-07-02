"""Standing server instructions + a SessionStart re-injection entry point.

The MCP server surfaces ``INSTRUCTIONS`` to the agent every session via the ``instructions``
field of the MCP ``initialize`` handshake (server.py). It is undocumented whether those
survive a context compaction, so the plugin ALSO re-injects them after a compact via a
``SessionStart`` hook (matcher ``compact``) that runs this module's ``main()`` and emits the
text as ``additionalContext``.

Kept deliberately STDLIB-ONLY (``json``/``sys``) so ``python -m teammate_comms.instructions``
is fast and safe on Windows stdout (``json.dump`` defaults to ``ensure_ascii=True``, so any
non-ASCII in the text is escaped). Mirrors the approach vibe-cognition uses.
"""

import json
import sys

from . import __version__

# Surfaced to the agent every session as "MCP Server Instructions" (server.py passes this
# to the initialize response) AND re-injected after a compact (see main()).
INSTRUCTIONS = (
    "This is teammate-comms. Call teammate_register(agent=\"<your-name>\") once at "
    "session start to establish your identity and start your channel, then "
    "teammate_inbox to drain any queued messages. Optionally set a profile at "
    "register (role, personality, status, authority; your project is auto-filled). "
    "Comms are global by default (all projects share one space), so you can message "
    "agents in other projects too. A channel event (notifications/claude/channel) "
    "means a teammate messaged you while idle — read with teammate_inbox, then "
    "teammate_ack. Reply with teammate_send. You are a full instance: the channel "
    "wakes you, so no polling loop is needed.\n"
    "\n"
    "Standing rules:\n"
    "- Update your teammate-comms status as you work (teammate_update) so teammates can "
    "see what you're doing, which project you're in, and which areas you own — via "
    "teammate_list / teammate_profile — without interrupting you.\n"
    "- Before starting a task, find out who holds authority over the areas you'll touch "
    "(teammate_list / teammate_profile); if a teammate owns one, coordinate with them via "
    "teammate_send before you modify it — never overlap another agent's authority unannounced."
)

# Header so the re-injected (post-compact) block is self-explaining when it sits next to
# any MCP instructions that may have survived the compaction. H1: stamped with the package
# version — a mid-session plugin update splits provenance (the running server's INSTRUCTIONS
# are spawn-frozen; this hook re-execs from disk), so a stale-vs-current-version mismatch
# between the two is now visible instead of silent.
_REINJECT_HEADER = f"# teammate-comms v{__version__} - Standing Practices (re-injected after compaction)"


def main():
    """Emit the standing instructions as SessionStart ``additionalContext`` JSON.

    Invoked by the ``compact``-matched SessionStart hook. Always emits (the matcher
    already gates this to post-compaction).
    """
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"{_REINJECT_HEADER}\n\n{INSTRUCTIONS}",
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
