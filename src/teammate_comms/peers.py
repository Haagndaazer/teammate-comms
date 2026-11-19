"""Terminal command that approves, removes or lists peer machines for cross-machine comms,
turns remote control of this machine on or off, and links or unlinks a cloud mailbox.

``python -m teammate_comms.peers add <machine>``;
``python -m teammate_comms.peers link_mailbox <name> <url> <key_file>``. Agents have the same
actions through the ``teammate_peers`` tool, except linking a mailbox, which only this
command does.
"""
import argparse
import sys

from . import mailbox, relay
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
                                 relay.REFUSE_ACTION, mailbox.LINK_ACTION,
                                 mailbox.UNLINK_ACTION))
    parser.add_argument("machine", nargs="?",
                        help="Tailscale machine name (for add/remove), or the short name of "
                             "a cloud mailbox (for link_mailbox/unlink_mailbox).")
    parser.add_argument("url", nargs="?", help="link_mailbox: the mailbox's https address.")
    parser.add_argument("key_file", nargs="?",
                        help="link_mailbox: file holding this machine's mailbox key.")
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
        elif args.action == mailbox.LINK_ACTION:
            print(relay.link_mailbox(root, args.machine, args.url, args.key_file, net))
        elif args.action == mailbox.UNLINK_ACTION:
            print(relay.unlink_mailbox(root, args.machine))
        else:
            print(relay.peers_report(root, net))
    except CommsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
