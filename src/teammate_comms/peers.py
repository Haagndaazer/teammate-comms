"""User-run command that approves or removes peer machines for cross-machine comms.

``python -m teammate_comms.peers add <machine>``. Approval is reserved for the user, so it
is not offered as an agent tool.
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
        description="Approve, remove or list the machines this one exchanges messages with.")
    parser.add_argument("action", nargs="?", choices=("list", "add", "remove"), default="list")
    parser.add_argument("machine", nargs="?", help="Tailscale machine name (for add/remove).")
    parser.add_argument("--comms-dir", dest="comms_dir", default=None)
    args = parser.parse_args(argv)
    root, _source = resolve_comms_root(args.comms_dir)
    try:
        if args.action == "add":
            print(relay.approve_peer(root, args.machine, net))
        elif args.action == "remove":
            print(relay.remove_peer(root, args.machine))
        else:
            print(relay.peers_report(root, net))
    except CommsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
