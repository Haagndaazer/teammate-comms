"""Create and maintain the secrets for a teammate-comms cloud mailbox.

    python make_config.py --out <folder> --machine desktop --bot CommsTest
    python make_config.py --out <folder> --add-machine laptop
    python make_config.py --out <folder> --rebuild

The folder then holds:
    config.json            readable source of truth; paste each bot's doorbell here
    wrangler-secrets.json  what to upload: npx wrangler secret bulk <folder>/wrangler-secrets.json
    machine-<name>.key     one per machine, given to teammate_peers(action="link_mailbox")
    bot-<name>.secret.txt  one per cloud agent, pasted into its masked secret

Keep the folder out of any project or synced folder. Standard library only.
"""
import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

MACHINE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
BOT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def private_write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")
    if os.name != "nt":
        os.chmod(path, 0o600)


def in_repository(folder):
    return any((parent / ".git").exists() for parent in (folder, *folder.parents))


def check_names(config):
    for name in config["machines"]:
        if not MACHINE_RE.match(name):
            fail(f"machine name {name!r} must be lowercase letters, digits and hyphens")
    lowered = set()
    for name, bot in config["bots"].items():
        if not BOT_RE.match(name) or name.lower() == "comms-relay":
            fail(f"bot name {name!r} is not usable")
        if name.lower() in lowered:
            fail(f"two bot names differ only by case: {name!r}")
        lowered.add(name.lower())
        url, key = bot.get("doorbell_url") or "", bot.get("doorbell_key") or ""
        if bool(url) != bool(key):
            fail(f"bot {name!r}: doorbell_url and doorbell_key must both be set or both be empty")
        if url and not url.startswith("https://"):
            fail(f"bot {name!r}: doorbell_url must start with https://")
    if not config["machines"] or not config["bots"]:
        fail("at least one machine and one bot are needed")


def upload_form(config):
    bots = {}
    for name, bot in config["bots"].items():
        entry = {"key": bot["key"]}
        if bot.get("doorbell_url"):
            entry["doorbell_url"] = bot["doorbell_url"].strip()
            entry["doorbell_key"] = bot["doorbell_key"].strip()
        bots[name] = entry
    return {"machines": config["machines"], "bots": bots}


def write_all(out, config):
    check_names(config)
    private_write(out / "config.json", json.dumps(config, indent=2) + "\n")
    private_write(out / "wrangler-secrets.json",
                  json.dumps({"MAILBOX_CONFIG": json.dumps(upload_form(config))}) + "\n")
    for name, key in config["machines"].items():
        private_write(out / f"machine-{name}.key", key + "\n")
    for name, bot in config["bots"].items():
        private_write(out / f"bot-{name}.secret.txt", f"Bearer {bot['key']}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create the secrets for a teammate-comms cloud mailbox.")
    parser.add_argument("--out", required=True, help="Folder for the secret files, outside any project.")
    parser.add_argument("--machine", action="append", default=[], help="Name for one of your machines.")
    parser.add_argument("--bot", action="append", default=[], help="Name of a cloud agent.")
    parser.add_argument("--add-machine", action="append", default=[])
    parser.add_argument("--add-bot", action="append", default=[])
    parser.add_argument("--rebuild", action="store_true",
                        help="Rewrite wrangler-secrets.json and key files from an edited config.json.")
    parser.add_argument("--allow-repository", action="store_true",
                        help="Write even though the folder is inside a git repository.")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    if in_repository(out) and not args.allow_repository:
        fail(f"{out} is inside a git repository. Choose a folder outside any project so the "
             f"keys cannot be committed.")
    path = out / "config.json"
    updating = args.rebuild or args.add_machine or args.add_bot
    if updating:
        if args.machine or args.bot:
            fail("use --add-machine/--add-bot to extend an existing mailbox")
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            config["machines"], config["bots"] = dict(config["machines"]), dict(config["bots"])
        except (OSError, ValueError, KeyError, TypeError):
            fail(f"{path} is missing or not readable; create the mailbox first with --machine and --bot")
    else:
        if path.exists():
            fail(f"{path} already exists. Use --add-machine, --add-bot or --rebuild; making new "
                 f"keys would cut off everything already linked.")
        if not args.machine or not args.bot:
            fail("give at least one --machine and one --bot")
        config = {"machines": {}, "bots": {}}
    for name in args.machine + args.add_machine:
        if name in config["machines"]:
            fail(f"machine {name!r} is already in the mailbox")
        config["machines"][name] = secrets.token_urlsafe(32)
    for name in args.bot + args.add_bot:
        if name in config["bots"]:
            fail(f"bot {name!r} is already in the mailbox")
        config["bots"][name] = {"key": secrets.token_urlsafe(32), "doorbell_url": "", "doorbell_key": ""}
    out.mkdir(parents=True, exist_ok=True)
    write_all(out, config)
    print(f"Wrote the mailbox secrets to {out}")
    print(f"  machines: {', '.join(config['machines'])}")
    print(f"  bots:     {', '.join(config['bots'])}")
    missing = [n for n, b in config["bots"].items() if not b.get("doorbell_url")]
    if missing:
        print(f"  no doorbell yet for: {', '.join(missing)} (paste it into config.json, then --rebuild)")
    print(f"Upload with: npx wrangler secret bulk \"{out / 'wrangler-secrets.json'}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
