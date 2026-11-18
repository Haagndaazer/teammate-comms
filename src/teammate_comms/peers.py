"""Terminal command that approves, removes or lists peer machines for cross-machine comms,
and turns remote control of this machine on or off.

``python -m teammate_comms.peers add <machine>``. The same actions are available to agents
through the ``teammate_peers`` tool.
"""
import argparse
import sys

from . import relay
from .comms import CommsError, resolve_comms_root


def main(argv=None, net=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="python -m teammate_comms.peers",
        description="Approve, remove or list the machines this one exchanges messages with, "
                    "and choose whether they may start and shut down agent sessions here.")
    parser.add_argument("action", nargs="?", default="list",
                        choices=("list", "add", "remove", relay.ACCEPT_ACTION,
                                 relay.REFUSE_ACTION))
    parser.add_argument("machine", nargs="?", help="Tailscale machine name (for add/remove).")
    parser.add_argument("--comms-dir", dest="comms_dir", default=None)
    args = parser.parse_args(argv)
    root, _source = resolve_comms_root(args.comms_dir)
    try:
        if args.action == "add":
            print(relay.approve_peer(root, args.machine, net))
        elif args.action == "remove":
            print(relay.remove_peer(root, args.machine))
        elif args.action in (relay.ACCEPT_ACTION, relay.REFUSE_ACTION):
            print(relay.set_remote_control(root, args.action == relay.ACCEPT_ACTION))
        else:
            print(relay.peers_report(root, net))
    except CommsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
