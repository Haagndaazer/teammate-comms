# Sending email from an agent address: plan (draft, not approved)

Status: draft for the user's approval. Nothing is built. Follows `docs/email-inbox-plan.md`
(receiving, shipped in 0.22.0) and reverses its "receive only" ruling by the user's ruling of
2026-11-27.

## 1. What the user asked for

1. An agent can send email, with attachments, from its own comms address.
2. First use: a cloud agent (the Rakazo "canary", which tests sign-in for a product) sends
   the export files of a failed run to a local agent (Violet) to inspect. The canary gets
   its own address. Rakazo's own email tool cannot attach files.
3. Attachments up to 10 MB by default; the user can allow more for an approved sender.
   The measured case: five files, the largest 6.8 MB, about 6.9 MB together.

## 2. The hard part: getting a file into the mailbox

A cloud agent talks to the mailbox through tool calls, and a tool argument is text the
model writes out. A 6.8 MB file is 9 MB of base64: no model can emit that, and Rakazo cuts
long strings. So a file cannot travel inside a tool call.

The file is on the agent's own computer (a cloud agent's sandbox, or a PC). It goes up
with a plain HTTP upload:

1. The agent calls `email_attach(me, from, name, size)`. The mailbox answers with a
   **one-time upload address**: `https://<mailbox>/up/<ticket>`, where the ticket is 32
   random bytes. It is valid for 15 minutes, for one upload, for that sender address, for
   at most the stated size, and it can do nothing else.
2. The agent uploads from its computer:
   `curl --fail --upload-file frames.zip "https://<mailbox>/up/<ticket>"`.
   The mailbox stores the bytes in pieces as they arrive, counts them, computes the
   SHA-256 and answers with the size and hash.
3. The agent calls `email_send(me, from, to, subject, body, attachments: [ticket ids])`.

The account token never leaves the connection settings; the sandbox only ever sees a
ticket. A local agent needs none of this: its tool takes file paths and the relay uploads
with the machine key (section 5).

Unverified, for the spike: that the canary can run `curl` from its computer to a
`workers.dev` address, and how the Worker reads a 10 MB request body (streamed into
storage in 512 KiB pieces, never held whole).

## 3. Two kinds of recipient

**An address of the same mailbox** (the canary to Violet). The mailbox delivers it itself:
no internet, no sending service, nothing to set up at Cloudflare. It enters the same
path as received mail (section 4 of the inbox plan onward) with the sender identity set
to the sending address, which the mailbox knows for certain. Labels, the outside-email
note, held attachments, limits and the download path all apply unchanged. The recipient
address's `senders` and `attachments_from` lists decide the label and whether files
download by themselves, exactly as for outside mail.

**An outside address.** Sent through Cloudflare Email Sending (the `send_email` binding,
beta; the user's Workers Paid plan allows any recipient, 3,000 a month included). The
sending domain must be onboarded for sending once (`wrangler email sending enable`), which
adds DKIM, SPF and DMARC records. Attachments are read back from storage and passed as
bytes; the whole message may not exceed 25 MiB.

The first release can ship internal delivery alone and still serve the canary. Outside
sending is built in the same release only if the user wants it now (ruling 1).

## 4. Who may send, and to whom

Declared in the mailbox config, per address, beside `senders`:

```
"canary@agents.example.com": {
  "owner": "Canary",
  "send_to": ["violet-desktop@agents.example.com"],
  "send_per_day": 20
}
```

- `send_to` is the complete list of allowed recipients: exact addresses, or `@domain` for
  a whole domain. Empty or missing means the address cannot send. There is no "anyone".
- An address sends only when the caller owns it: for a cloud agent, `me` is the owner and
  the account holds that agent; for a local agent, the owner is `Name@machine` on the
  machine whose key made the call.
- Nothing is ever sent by the mailbox on its own: no auto-replies, no bounces written by
  us, and a send to an address that declared itself automatic is not special-cased.
- Limits: `send_per_day` per address (default 20, at most 200), 200 a day for the mailbox,
  at most 5 recipients per email, all of them in `send_to`.
- Subject one line of 200 characters, body plain text up to 48,000 characters.
- Every send is counted per address and shown in the status the user already has.

## 5. Local agents

`teammate_email` gains `send` (`from`, `to`, `subject`, `body`, `files`: paths). The tool
checks ownership, that each path is a regular file under a size cap and not a link, then
leaves a send request for the relay (as `fetch_attachments` does). The relay uploads each
file to `/pc/upload` with the machine key, in pieces, then posts the send. The outcome
arrives as a `comms-email` message: sent, or refused and why. A request is tried for an
hour and then reported as failed.

## 6. Attachment size

Today: a received email may be 5 MiB with 3 MiB of attachments in at most 5 files.

New defaults: 10 MiB of attachments per email in at most 10 files, received or sent. An
outside email carrying that is about 14 MiB raw (base64), so the raw limit becomes
15 MiB; the live test measured 84 ms of CPU for 3 MiB, and the spike measures 14 MiB
before this is settled. Per address the user may raise it for chosen senders:

```
"attachment_mb": {"@client.example": 20, "canary@agents.example.com": 20}
```

- at most 20 MiB (an outside message is capped at 25 MiB by Cloudflare, and base64 adds a
  third; internal delivery has no encoding but keeps the same cap for one rule);
- the entry must also be on `senders` or `attachments_from`, so a stranger never gets it.

Budgets grow with it: 300 MiB per address for listed senders, 1 GiB for the mailbox;
unlisted senders keep 20 MiB per address and the 10 MiB default cannot be raised for them.
On the PC, `FILE_BYTES` follows the same numbers; downloads already stream in pieces.

## 7. Storage and limits in the Worker

- `up:<ticket>` an upload ticket: sender address, owner, declared name and size, expiry,
  state. Deleted when used by a send or after 15 minutes; its pieces go with it.
- Uploaded pieces use the existing `ma:` piece keys, so an internal send hands them to the
  recipient's held-attachment record without copying.
- At most 10 open tickets per address; the upload budget counts against the sender
  address until the send, then against the recipient like any received attachment.
- `ms:<address>` sends in the last day, for the limit and the counts.
- `/up/<ticket>` accepts only `PUT` or `POST`, refuses a body longer than the ticket's
  size while it is still arriving, and answers every bad ticket identically.

## 8. Tool surface

Cloud agents (MCP): `email_addresses(me)`, `email_attach(me, from, name, size)`,
`email_send(me, from, to, subject, body, attachments?)`. Offered only to an account with
at least one agent that owns an address able to send.

Local agents: `teammate_email` action `send`.

## 9. Order of work

1. Spike with the canary: an upload ticket, a `curl` upload of a 7 MB file from its
   computer, and CPU and memory for a 14 MiB inbound message.
2. Worker: tickets, upload route, internal delivery, limits, counts; raised caps.
3. Cloud agent tools.
4. Relay and `teammate_email send` for local agents.
5. Outside sending through `send_email`, if in scope.
6. Docs, peer review of the implementation, live test: the canary sends the real export
   files to Violet.

## 10. Risks

- **Data leaving under the user's domain.** An agent steered by an incoming email could
  send files it can read to any address on its `send_to` list. The list is the control:
  it is in the config, which no agent can change. With internal delivery only, nothing
  can leave the user's own agents at all.
- **A ticket is a bearer secret** for 15 minutes. It can only add one file of a stated
  size to a pending send from one address, and whoever holds it cannot send or read.
  A cloud agent's sandbox, its logs and Rakazo's history will contain it.
- **Agents on one machine or one account are not separated.** Any agent on the canary's
  account can send as the canary, and any agent on a PC as any address that PC owns.
- **Bigger files** mean more storage held at Cloudflare for up to 14 days and larger
  downloads into the comms folder.
- **Outside sending** rests on a beta service with a daily quota that starts low, and a
  bounce or complaint affects the domain's reputation.

## 11. Rulings needed

1. **Scope of the first release:** internal delivery only (agents emailing each other's
   comms addresses; solves the canary case, nothing can leave), or outside recipients too.
2. **Size numbers:** 10 MiB default and 20 MiB ceiling per approved sender, as above.
3. **Recipients:** an explicit `send_to` list per address with no "anyone" option.

## 12. Changed after peer review (these override the sections above)

- **Internal mail is its own path, not "verified outside mail".** The config refuses an
  agent domain on a `senders` list, so a mailbox address could never be listed that way.
  A separate `receiveInternal` takes the sending address from the mailbox itself, skips
  the DMARC and Message-ID steps, and reuses every cap, limit and count of `receive`. The
  recipient declares which mailbox addresses it accepts with a new list, `agents_from`;
  an address on it is shown as listed and its files download by themselves. The note
  says the email is from another agent of the user's mailbox, still not from the user.
- **`send_to` limits damage; it does not prove who sent.** Any agent on an account can
  name another agent of that account, and any agent on a PC can use an address that PC
  owns. Each send records the account or machine that made it, shown to the recipient
  and in the status.
- **Upload.** `PUT /up/<ticket>` is forwarded as a stream to the Durable Object's own
  fetch, never passed as an argument; it writes 512 KiB pieces as they arrive. The ticket
  is stored hashed, is consumed before the body is read, and is bound to the sender, file
  name, exact size and a SHA-256 the agent declares (it computes it on its computer with
  one command); a body of another length or hash is discarded. Every bad ticket gets the
  same answer. Budget is reserved when a ticket is issued (at most 10 tickets and 50 MiB
  in flight per address, 2 uploads at once per mailbox) and released by an expiry index.
- **Received mail keeps one path for big files.** The handler writes attachment pieces
  to storage itself, one at a time, and then passes only their record, so a large email
  is never copied whole between the handler and storage.
- **Sizes.** 10 MiB default; the per-sender ceiling is 15 MiB for outside senders (a
  25 MiB message holds about 18 MiB of files) and 20 MiB for mailbox addresses. Held
  files are deleted from the mailbox as soon as the PC has them, as today.
- **`send_to`** holds exact addresses only, at most 20, each either an address of this
  mailbox or, if outside sending is on, any address. A plus tag is allowed on a listed
  address. Both validators share one table of cases tested in both languages, and a bad
  entry switches off sending for that address only.
- **A send carries a request id**, so a retried tool call does not send twice.
- **A cloud agent recipient gets no attachments**, as with received mail today.
- **Local sending**, when built: the relay re-checks that the named sender is an address
  of a registered agent on this machine, copies each file to a private staging file
  before uploading and hashes the copy, and refuses links. The file checks do not stop
  an agent sending something it can read; `send_to` is the control.
- **Outside sending**, when built: sent does not mean delivered, since bounces go to
  Cloudflare and not to the mailbox; Reply-To and threading headers are set; turning on
  sending adds a DMARC reject policy to the subdomain, which must be checked against
  mail the agents receive from their own domain.
- **First release:** upload ticket, internal delivery, `email_send` for cloud agents,
  the raised sizes. The spike comes first: stream a 7 MB upload, verify the hash, and
  measure memory for a 14 MiB received email.
