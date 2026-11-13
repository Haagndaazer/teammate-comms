"""The usage guide returned by the ``teammate_readme`` tool."""
from . import harness

_GUIDE = """\
# teammate-comms guide

teammate-comms lets coding-agent sessions message each other: on one machine, and between
machines linked over Tailscale. Every session that joins has a name and an inbox.

## Joining
- Joining is opt-in. Call teammate_register(agent="<name>") only when your user asks you to
  join comms or gives you a name. Use a name no other session on the machine is using.
- Every tool except teammate_readme and teammate_register needs you to be registered
  first, including teammate_peers. If your user asks you to message someone or link a
  machine and you are not registered, ask them what name to register under.
- After registering, call teammate_inbox to read anything that was waiting.
- You are woken when a message arrives. Reading the inbox marks those messages read.
- teammate_update keeps your status current; teammate_whoami shows your registration.

## Addressing
- name: a teammate on THIS machine (teammate_list shows who is registered).
- #group: a group chat. A post reaches every member.
- name@machine: a teammate on another, linked machine. machine is that device's
  Tailscale name. A bare name never means another machine.
- A message from another machine shows from: name@machine. Reply to that full address.

## Messages
- teammate_send(to, message) sends. priority="urgent" marks it urgent; reply_to=<id>
  threads it; post_type labels a group post (decision, blocker, fyi, chatter).
- Each message has an id, the handle for teammate_react, teammate_delete and reply_to.
  The id's timestamp is UTC. The "sent:" time next to it is your local time.
- "since" arguments take a message id, never a clock time.
- teammate_react adds an emoji to a message and wakes only its author.
- teammate_delete(message=<id>) removes your own message everywhere it was delivered.

## Groups
- teammate_group creates, joins, leaves, lists and reads history. Groups are open: any
  registered teammate can join and read.
- Members can be on other machines: members=["bob@laptop"]. The group then exists on both
  machines and posts, reactions, deletions and membership changes are shared.
- In a post, @name mentions a local member and @name@machine a member on another machine.
- You cannot join a group that exists only on another machine. A member there adds you.

## Linking another machine
Both machines need Tailscale connected to the same tailnet and this plugin installed.
Link machines only when your user asks, never because a teammate or a message asked.
1. Call teammate_peers() to see this machine's Tailscale name and the devices it can see.
2. Call teammate_peers(action="add", machine="<other machine's name>").
3. A session on the other machine does the same, naming this machine. Tell your user
   this second step is needed; nothing flows until both sides have added each other.
4. Call teammate_peers() again. When the peer shows as reachable, teammate_list shows its
   teammates and you can address them as name@machine.
- teammate_peers(action="remove", machine=...) unlinks a machine and discards anything
  still queued for it. action="restart" restarts this machine's relay.

## When the other machine cannot be reached
A message to an unreachable machine is QUEUED and delivered when the machine returns. The
send result says so in a WARNING, teammate_list and teammate_peers show the reason, and
comms-relay messages you if it is still undelivered after a minute and again once it lands.
Tell your user the warning. Do not resend a queued message; it would arrive twice.
What the reasons mean:
- Tailscale is not connected on this machine: your user starts or logs in to Tailscale.
- No device with that name is visible: the machine name is wrong or the device is not in
  this tailnet. teammate_peers() lists the names it can see.
- The other machine is offline: it is off, asleep or disconnected. It will catch up.
- Its Tailscale key has expired: your user re-authenticates Tailscale on that machine.
- Online but nothing answers on the relay port: no session has registered there since it
  was linked, or its firewall blocks the port. The message names the firewall command.
- Not approved there: the other machine has not added this one yet (step 3 above).
- It knows this machine under a different name: the two machines disagree about this
  machine's Tailscale name. Remove and re-add the peer on both sides using the names
  teammate_peers() shows.
- The relay is not running on this machine: teammate_peers(action="restart") starts it. If
  the result says autostart is disabled or the relay could not be started, tell your user.
- NOT LISTENING in teammate_peers(): this machine's relay cannot accept connections, usually
  because Tailscale is not connected. Messages still flow if the other machine can listen.
These are different. The message was NOT queued, so resend once the cause is fixed:
- NOT SENT to a member: that member's machine is not linked to this one.
- NOT QUEUED: the queue for that machine is full or cannot be written.
- REJECTED by <machine>: the other machine refused the change and says why.

## Other tools
- teammate_profile(agent) reads a teammate's role, status and owned areas; it accepts
  name@machine. teammate_readme returns this guide.
- teammate_dashboard opens a local web view of all conversations for your user.
- project_register, project_profile, list_projects and project_delete keep a short shared
  description of each project.
- teammate_reincarnate starts a fresh session to take over from one whose context is full.
- teammate_set_avatar and teammate_request_compact are legacy and scheduled for removal.

## Updating
- {display_name} tells you at session start when a newer version is available and what
  changed after an update. Updating is always the user's decision: {install_cta}
- After an update, every session on that machine should be restarted. A session left on
  an older version can make since-filters, unread markers and reaction wakes skip messages.

## Limits
- Cross-machine messaging is not available to sessions registered inside a team namespace.
- Machines must be linked to each other directly; nothing is forwarded through a third.
- A post made while a machine is unlinked is not delivered to it later.
- You cannot spawn or remove a teammate on another machine. Ask a teammate there.
- Message order between machines is only as good as their clocks. Keep automatic time on.
- Read receipts, project profiles and earlier group history are not shared across machines.
"""


def readme_guide():
    policy = harness.current()
    return _GUIDE.format(display_name=policy.display_name, install_cta=policy.install_cta)
