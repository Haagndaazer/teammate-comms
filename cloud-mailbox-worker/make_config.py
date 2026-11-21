"""Create and maintain the secrets for a teammate-comms cloud mailbox.

    python make_config.py --out <folder> --machine desktop --account rakazo --bot Chief --bot Scout
    python make_config.py --out <folder> --add-machine laptop
    python make_config.py --out <folder> --add-bot Scribe --account rakazo
    python make_config.py --out <folder> --add-account grok --bot CommsTest
    python make_config.py --out <folder> --rebuild

An account is one place your cloud agents live that shares a single connection to the
mailbox: a space, a workspace or a whole login, whatever the product shares connectors
across. Every agent in it uses the account's one token and says its own name on each call.
Agent names are unique across the whole mailbox.

The folder then holds:
    config.json               readable source of truth; paste each agent's doorbell here
    wrangler-secrets.json     what to upload: npx wrangler secret bulk <folder>/wrangler-secrets.json
    machine-<name>.key        one per machine, named in the terminal command that links it
                              (python -m teammate_comms.peers link_mailbox <name> <url> <key file>)
    account-<name>.token.txt  one per account: the bearer token for its mailbox connection

A machine name is a lowercase label of your choice; a cloud agent sees your agents as
name@<that label>. To replace a key use --new-key NAME (a machine or an account), then
upload again and relink the machine or update the connection's token. Keep the folder out of any project or synced folder. Standard library only.
"""
import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
BOT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
MACHINE_KEY_RE = re.compile(r"tcm_[A-Za-z0-9_-]{32,124}")
ACCOUNT_KEY_RE = re.compile(r"tcb_[A-Za-z0-9_-]{32,124}")
MAX_UPLOAD_BYTES = 4500
DEFAULT_ACCOUNT = "main"


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def private_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def in_repository(folder):
    return any((parent / ".git").exists() for parent in (folder, *folder.parents))


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower())[:63].strip("-") or "bot"


def load(path):
    """The stored configuration, with the older one-key-per-bot layout converted."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        config = {"machines": dict(raw["machines"]), "accounts": {}}
        for label, account in dict(raw.get("accounts") or {}).items():
            config["accounts"][label] = {"key": account["key"], "bots": dict(account["bots"])}
            if "can_set_status" in account:
                config["accounts"][label]["can_set_status"] = account["can_set_status"]
        for name, bot in dict(raw.get("bots") or {}).items():
            label = slug(name)
            if label in config["accounts"]:
                fail(f"bot {name!r} in the old layout clashes with account {label!r}")
            config["accounts"][label] = {"key": bot["key"], "bots": {name: {
                "doorbell_url": bot.get("doorbell_url") or "",
                "doorbell_key": bot.get("doorbell_key") or ""}}}
        return config, [slug(n) for n in dict(raw.get("bots") or {})]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        fail(f"{path} is missing or not readable; create the mailbox first with --machine and --bot")


def check_bot(name, bot):
    if not BOT_RE.fullmatch(name) or name.lower() in ("comms-relay", "comms-status"):
        fail(f"bot name {name!r} is not usable")
    if not isinstance(bot, dict):
        fail(f"bot {name!r} must be an object")
    url, key = bot.get("doorbell_url") or "", bot.get("doorbell_key") or ""
    if not isinstance(url, str) or not isinstance(key, str):
        fail(f"bot {name!r}: doorbell_url and doorbell_key must be text")
    url, key = url.strip(), key.strip()
    if bool(url) != bool(key):
        fail(f"bot {name!r}: doorbell_url and doorbell_key must both be set or both be empty")
    if url:
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            parts, host = None, None
        if not host or parts.scheme != "https":
            fail(f"bot {name!r}: doorbell_url must be a full https:// address")


def check(config):
    keys = set()

    def take(key, pattern, label):
        if not isinstance(key, str) or not pattern.fullmatch(key):
            fail(f"{label}: the key in config.json is not one this tool made. Keys must not "
                 f"be edited by hand; remove the entry and add it again to get a new key.")
        if key in keys:
            fail(f"{label}: its key is also used by another entry")
        keys.add(key)

    if not isinstance(config.get("machines"), dict) or not isinstance(config.get("accounts"), dict):
        fail("config.json must have a 'machines' object and an 'accounts' object")
    for name, key in config["machines"].items():
        if not LABEL_RE.fullmatch(name):
            fail(f"machine name {name!r} must be lowercase letters, digits and hyphens")
        take(key, MACHINE_KEY_RE, f"machine {name!r}")
    seen = {}
    for label, account in config["accounts"].items():
        if not LABEL_RE.fullmatch(label):
            fail(f"account name {label!r} must be lowercase letters, digits and hyphens")
        if not isinstance(account, dict) or not isinstance(account.get("bots"), dict):
            fail(f"account {label!r} must have a key and a 'bots' object")
        take(account.get("key"), ACCOUNT_KEY_RE, f"account {label!r}")
        if not account["bots"]:
            fail(f"account {label!r} has no bots")
        if not isinstance(account.get("can_set_status", False), bool):
            fail(f"account {label!r}: can_set_status must be true or false")
        for name, bot in account["bots"].items():
            check_bot(name, bot)
            if name.lower() in seen:
                fail(f"bot name {name!r} is used twice (also in account {seen[name.lower()]!r}); "
                     f"names must be unique across the mailbox, ignoring capitals")
            seen[name.lower()] = label
    if not config["machines"] or not config["accounts"]:
        fail("at least one machine and one bot are needed")


def upload_form(config):
    accounts = {}
    for label, account in config["accounts"].items():
        bots = {}
        for name, bot in account["bots"].items():
            entry = {}
            if (bot.get("doorbell_url") or "").strip():
                entry = {"doorbell_url": bot["doorbell_url"].strip(),
                         "doorbell_key": bot["doorbell_key"].strip()}
            bots[name] = entry
        accounts[label] = {"key": account["key"], "bots": bots}
        if account.get("can_set_status") is True:
            accounts[label]["can_set_status"] = True
    return {"machines": config["machines"], "accounts": accounts}


def write_all(out, config, converted=()):
    upload = json.dumps(upload_form(config))
    if len(upload.encode("utf-8")) > MAX_UPLOAD_BYTES:
        fail(f"the configuration is {len(upload.encode('utf-8'))} bytes, more than a Worker "
             f"secret can hold ({MAX_UPLOAD_BYTES}). Use a second mailbox for more bots.")
    out.mkdir(parents=True, exist_ok=True)
    private_write(out / "config.json", json.dumps(config, indent=2) + "\n")
    private_write(out / "wrangler-secrets.json", json.dumps({"MAILBOX_CONFIG": upload}) + "\n")
    for name, key in config["machines"].items():
        private_write(out / f"machine-{name}.key", key + "\n")
    for label, account in config["accounts"].items():
        private_write(out / f"account-{label}.token.txt", account["key"] + "\n")
    for label in converted:
        for name in config["accounts"][label]["bots"]:
            (out / f"bot-{name}.secret.txt").unlink(missing_ok=True)
    wanted = {f"machine-{n}.key" for n in config["machines"]}
    wanted |= {f"account-{n}.token.txt" for n in config["accounts"]}
    for stale in [*out.glob("machine-*.key"), *out.glob("account-*.token.txt")]:
        if stale.name not in wanted:
            stale.unlink()


def new_bot():
    return {"doorbell_url": "", "doorbell_key": ""}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Create the secrets for a teammate-comms cloud mailbox.",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="Folder for the secret files, outside any project.")
    parser.add_argument("--machine", action="append", default=[],
                        help="First run: a lowercase label for one of your machines.")
    parser.add_argument("--account", default=None,
                        help="The account the --bot or --add-bot names belong to "
                             f"(lowercase label; default '{DEFAULT_ACCOUNT}' on the first run).")
    parser.add_argument("--bot", action="append", default=[],
                        help="A cloud agent's name, for the first run or with --add-account.")
    parser.add_argument("--add-machine", action="append", default=[],
                        help="Add a machine to an existing mailbox (lowercase label).")
    parser.add_argument("--add-account", default=None,
                        help="Add an account with its own token; name its agents with --bot.")
    parser.add_argument("--add-bot", action="append", default=[],
                        help="Add a cloud agent to an existing account; needs --account.")
    parser.add_argument("--new-key", action="append", default=[], metavar="NAME",
                        help="Give a machine or an account a new key, keeping everything "
                             "else about it. The old key stops working once uploaded.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Rewrite the upload and key files from an edited config.json.")
    parser.add_argument("--allow-repository", action="store_true",
                        help="Write even though the folder is inside a git repository.")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    if in_repository(out) and not args.allow_repository:
        fail(f"{out} is inside a git repository. Choose a folder outside any project so the "
             f"keys cannot be committed.")
    path = out / "config.json"
    converted = []
    updating = (args.rebuild or args.add_machine or args.add_bot or args.add_account
                or args.new_key)
    if args.account and not (args.add_bot or (not updating and args.bot)):
        fail("--account goes with --add-bot, or with --bot on the first run")
    if updating:
        if args.machine:
            fail("use --add-machine to extend an existing mailbox")
        config, converted = load(path)
        if args.add_account:
            if args.add_account in config["accounts"]:
                fail(f"account {args.add_account!r} is already in the mailbox")
            if not args.bot:
                fail("--add-account needs at least one --bot")
            config["accounts"][args.add_account] = {
                "key": "tcb_" + secrets.token_urlsafe(32), "bots": {n: new_bot() for n in args.bot}}
        elif args.bot:
            fail("--bot is for the first run or with --add-account; use --add-bot NAME --account LABEL")
        if args.add_bot:
            label = args.account or (next(iter(config["accounts"])) if len(config["accounts"]) == 1 else None)
            if label not in config["accounts"]:
                fail("say which account the new bot joins with --account; accounts: "
                     + ", ".join(config["accounts"]))
            for name in args.add_bot:
                if name in config["accounts"][label]["bots"]:
                    fail(f"bot {name!r} is already in account {label!r}")
                config["accounts"][label]["bots"][name] = new_bot()
        for name in args.new_key:
            if name in config["machines"]:
                config["machines"][name] = "tcm_" + secrets.token_urlsafe(32)
            elif name in config["accounts"]:
                config["accounts"][name]["key"] = "tcb_" + secrets.token_urlsafe(32)
            else:
                fail(f"{name!r} is not a machine or an account in this mailbox")
            print(f"  new key for {name}: relink that machine, or paste the new token into "
                  f"that account's connection, after uploading")
    else:
        if path.exists():
            fail(f"{path} already exists. Use --add-machine, --add-account, --add-bot or "
                 f"--rebuild; making new keys would cut off everything already linked.")
        if not args.machine or not args.bot:
            fail("give at least one --machine and one --bot")
        config = {"machines": {}, "accounts": {args.account or DEFAULT_ACCOUNT: {
            "key": "tcb_" + secrets.token_urlsafe(32), "bots": {n: new_bot() for n in args.bot}}}}
    for name in args.machine + args.add_machine:
        if name in config["machines"]:
            fail(f"machine {name!r} is already in the mailbox")
        config["machines"][name] = "tcm_" + secrets.token_urlsafe(32)
    check(config)
    write_all(out, config, converted)
    print(f"Wrote the mailbox secrets to {out}")
    print(f"  machines: {', '.join(config['machines'])}")
    for label, account in config["accounts"].items():
        allowed = " (may set your availability status)" if account.get("can_set_status") is True else ""
        print(f"  account {label}: {', '.join(account['bots'])}{allowed}")
    missing = [n for a in config["accounts"].values() for n, b in a["bots"].items()
               if not (b.get("doorbell_url") or "").strip()]
    if missing:
        print(f"  no doorbell yet for: {', '.join(missing)} (paste it into config.json, then --rebuild)")
    if converted:
        print("  converted from the one-key-per-bot layout; keys are unchanged")
    for name in args.bot + args.add_bot:
        print(f'  to bring {name} on, paste to it: "Use the teammate-comms server to check for '
              f'mail with your name set to {name}, and do what the message asks."')
    print(f"Upload with: npx wrangler secret bulk \"{out / 'wrangler-secrets.json'}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
