# gmail-triage

A small daemon that labels, stars and marks important each new Gmail message
about 15–20 seconds after it arrives, using Claude Haiku as the classifier.

It only reads headers and Gmail's snippet (never the body), it has no public
endpoint, and it cannot delete or mark anything read. It takes mail out of the
inbox only for labels you list yourself (off by default).

```
Gmail users.watch ──► Pub/Sub topic ──► pull subscription ──► daemon (streaming pull)
                                                              │ debounce 5 s
                                                              ▼
                                        history.list from state.json historyId
                                        messages.get format=metadata
                                        Haiku (claude -p | API) → strict JSON, validated
                                        messages.modify addLabelIds (+STARRED), IMPORTANT set or cleared
```

This is a personal tool, published as-is. It runs one mailbox, on Linux with
systemd, and the label set is the one its author uses. Fork it if yours differs.

How it fits together, with diagrams and what each design decision gives and
costs: [ARCHITECTURE.md](ARCHITECTURE.md).

## What it does to your mailbox

- **Creates 30 labels** the first time it runs (below), coloured. A label you
  already have with the same name is reused.
- **Adds** one of those labels to each new inbox message, and `STARRED` when
  the star rules say so.
- **Sets Gmail's importance marker both ways.** Gmail's own guess is replaced:
  `IMPORTANT` is added or removed on every message it triages.
- **Archives only what you opt in.** `daemon.archive_labels` in `config.toml`
  is empty by default, so nothing leaves the inbox. List labels there, e.g.
  `["Jobs › Alerts", "Promos"]`, and new mail given one of them is taken out of
  the inbox after it is labelled. It stays unread under its label. Mail that is
  starred, important or `Security › Suspicious` always stays in the inbox.
  Restart the service after changing it.
- **Nothing else.** `gmail.apply()` asserts that the only labels ever removed
  are `IMPORTANT` and, when you configured archiving, `INBOX`, and that nothing
  adds TRASH, SPAM, UNREAD or INBOX. Mail in spam, trash or drafts, and mail
  that already has one of the labels, is skipped.
- **What leaves your machine:** each message's From, Subject, List-Unsubscribe
  header and Gmail snippet are sent to Anthropic for classification, along
  with your profile (below).

`--dry-run` shows what it would do and writes nothing.

## Labels

Flat Gmail labels; ` › ` is only a naming convention, not nesting.

| Group | Labels |
|---|---|
| Jobs | `Jobs › Applied`, `Reply`, `Interview`, `Rejected`, `Alerts`, `Skip` |
| Money | `Money`, `Money › Invoices`, `Receipts`, `Statements`, `Trading`, `Transfers`, `Taxes`, `Crypto` |
| Security | `Security`, `Security › Codes`, `Security › Suspicious` |
| Life | `Personal`, `Appointments`, `Health`, `Orders`, `Travel`, `Business`, `Government`, `Education` |
| Bulk | `Notifications`, `Newsletters`, `Newsletters › Crypto`, `Newsletters › AI`, `Promos` |

Definitions and tie-breaks: `DEFINITIONS` in [`gmail_triage/classifier.py`](gmail_triage/classifier.py).
The set is fixed in code (`classifier.PRIMARY`, colours in `gmail.LABEL_COLORS`);
changing it means editing both, and the tests will tell you what else to touch.

### Stars and important

Both are decided in code from the label, not left to the model.

- **Star** = something to do, mail you will come back to. `Jobs › Reply` and
  `Jobs › Interview` always; the labels in `classifier.NEVER_STAR` never (all
  Security mail, rejections, receipts, statements, newsletters, promos...);
  Haiku decides for the rest (bills, business, government, personal...).
- **Important** = everything starred, plus `classifier.ALWAYS_IMPORTANT`
  (`Jobs › Rejected`, `Personal`). Nothing else.

## Make it yours: the profile

The prompt in the repo is generic. What it should know about *you* goes in
`~/.config/gmail-triage/profile.toml`, outside the repo:

```toml
owner = "lives in Lisbon; looking for a backend developer job; runs a small print shop called Inkwell"

[notes]
"Jobs › Alerts" = "targets: backend or platform developer roles in Python or Go; Lisbon or remote within the EU."
"Jobs › Skip"   = "any title containing Senior, Staff or Manager; frontend-only roles; aggregator postings."
"Business"      = "the owner's Inkwell print shop (Stripe is its payment processor)."
```

Each note is pasted into the prompt directly under the label it refines.
Start from [`config/profile.example.toml`](config/profile.example.toml).

- No profile: the generic rules apply. `Jobs › Skip` and `Business` are never
  used, and every job alert is `Jobs › Alerts`.
- A profile with a typo in a label name stops the daemon at startup (exit 78)
  rather than being quietly ignored.
- Restart the service after editing it.

## Requirements

- Linux with systemd user services (or run it in the foreground yourself)
- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- A Google account, the `gcloud` CLI, and a Google Cloud billing account
  (Pub/Sub requires one to be linked)
- For classification, one of: [Claude Code](https://code.claude.com/docs) signed
  in on the same machine, or an Anthropic API key

## Setup

### 1. Install
```
git clone https://github.com/duplonicus/gmail-triage.git && cd gmail-triage
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest -q
git config core.hooksPath .githooks        # secret-scanning pre-commit hook
cp config/config.example.toml config/config.toml
```
Set a globally-unique `project_id` in `config/config.toml`.

### 2. Google Cloud project
```
gcloud auth login
gcloud billing accounts list
BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX scripts/setup_gcloud.sh
```
The script is idempotent. It creates the project, links billing, enables the
Gmail and Pub/Sub APIs, creates the topic and a pull subscription (1-day
retention), lets `gmail-api-push@system.gserviceaccount.com` publish to the
topic, and creates a budget alert of 1 unit of your billing currency.

Cost: a Gmail notification is a few hundred bytes and one arrives per mailbox
change, so a personal mailbox is a very small Pub/Sub volume. Check the
current free tier on [Google's pricing page](https://cloud.google.com/pubsub/pricing)
rather than trusting a number here; the budget alert is there in case.

### 3. OAuth consent screen and client (Cloud Console, your project selected)
1. **console.cloud.google.com/auth/overview** → **Get started**.
   App name `Gmail Triage`, support email = you → Next.
   Audience: **External** → Next. Contact email = you → Next. Agree → **Create**.
2. **Audience** → Test users → **+ Add users** → your Gmail address → Save.
3. **Data Access** → **Add or remove scopes** → paste into "Manually add scopes":
   `https://www.googleapis.com/auth/gmail.modify` and
   `https://www.googleapis.com/auth/pubsub` → Add to table → Update → **Save**.
4. **Clients** → **+ Create client** → Application type **Desktop app**, name
   `gmail-triage` → Create → **Download JSON** → save as
   `~/.config/gmail-triage/client_secret.json`.
5. **Audience** → Publishing status → **Publish app** → Confirm (status: *In production*).
   In *Testing*, refresh tokens expire after 7 days. In production but
   unverified, sign-in shows "Google hasn't verified this app" →
   **Advanced → Go to Gmail Triage (unsafe)**. That is expected: the app is
   yours and only you use it.

The console's layout changes; these steps were last walked through in
September 2026.

### 4. Authorize
```
.venv/bin/gmail-triage-auth
```
Open the printed URL in a browser on the same machine (under WSL, a Windows
browser works: the `localhost:8765` redirect reaches WSL). The token is saved
to `~/.config/gmail-triage/token.json`, mode 600.

### 5. Profile and classifier backend
```
cp config/profile.example.toml ~/.config/gmail-triage/profile.toml   # then edit
```
Pick a backend in `config/config.toml` (next section).

### 6. Dry run, then install the service
```
.venv/bin/gmail-triage --dry-run --backfill 3d --once && tail -50 logs/triage.log
scripts/install_service.sh
systemctl --user enable --now gmail-triage
```
Read the dry-run log before going live: it is the cheapest way to see whether
the labels suit your mail and what your profile still needs to say.

## Classifier backends (`classifier.backend` in `config/config.toml`)

- **`cli`** (default): runs `claude -p --model haiku`, the unmodified Claude
  Code binary, under whatever login you gave it. gmail-triage never reads or
  stores Claude credentials, and it strips `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` from the subprocess so a stray key can't turn into
  API billing. The flags `--tools "" --strict-mcp-config --setting-sources ""
  --disable-slash-commands --no-session-persistence --system-prompt …` cut the
  per-call input from 27,789 to 424 tokens (measured 2026-09-28, CLI 2.1.283,
  trivial prompt). A real 8-message batch: ~1.5k input tokens, ~9 s API time,
  ~13 s wall including CLI startup.
- **`api`**: the Anthropic SDK with `claude-haiku-4-5-20251001`, key read from
  `~/.config/gmail-triage/.env` (`ANTHROPIC_API_KEY=...`). Billed per token.
  The author runs `cli`; `api` is covered by the same tests but has had far
  less real use.

**Before you choose `cli` with a Claude subscription**, read Anthropic's
[Claude Code legal page](https://code.claude.com/docs/en/legal-and-compliance).
As of October 2026 it says subscription sign-in is for "ordinary use of Claude
Code", that plan limits "assume ordinary, individual usage of Claude Code and
the Agent SDK", and that developers building products should use API keys.
This tool is one person running the stock CLI on their own mailbox with their
own login, which is how the author reads "ordinary, individual usage" — but
that is a reading, not a ruling, and the account at stake is yours. If in
doubt, use `api`.

Two things that may change under `cli`: the docs say `--bare` will become the
default for `claude -p`, and bare mode does not use a subscription login; and
how programmatic use counts against plan limits is Anthropic's to change.

Invalid model output is retried once for just the affected messages; those are
then logged as `SKIP` and left unlabelled. A label is never guessed. A backend
*failure* (not logged in, timeout) does not advance `historyId`; the sync is
retried every 60 s.

## Run

| | |
|---|---|
| `systemctl --user status gmail-triage` | service state (the STATUS line shows the last sync) |
| `journalctl --user -u gmail-triage -f` | operational log |
| `tail -f logs/triage.log` | one line per message: time, from, subject, labels, star, important, reason |
| `.venv/bin/gmail-triage --dry-run --once` | log decisions for pending mail, change nothing |
| `.venv/bin/gmail-triage --backfill 30d --once` | triage untriaged inbox mail from the last 30 days |

Stop the service before running the CLI by hand against real state, or use
`--dry-run` (which never writes `state.json`).

## Back up the mailbox

`.venv/bin/gmail-triage-backup ~/backup/gmail` saves every message as a raw
`.eml` file plus an `index.jsonl` row with its labels, size, sender and
attachment names. It only reads from Gmail.

- It is paced at 120 messages a minute (`--per-minute`), because Gmail starts
  refusing raw downloads a little above that. A mailbox of 25,000 takes about
  three and a half hours.
- It is resumable: run the same command again and it fetches only what is
  missing. `complete.json` appears when a run finishes. `--force` starts over.
- `--query "newer_than:30d"` limits it to a Gmail search.

`.venv/bin/gmail-triage-census ~/backup/gmail` then reads that backup (not
Gmail) and writes `census.md` and `senders.csv`: size and count by sender,
year and category, the largest messages, and which messages are delete
candidates. A message is a candidate only if it is bulk mail (has an
unsubscribe header), sits in Promotions or Social, has no document attached,
is not in a thread you wrote in, is not starred, and carries no triage label
other than Promos or Newsletters. The census reports; it deletes nothing.

## Clean up an old inbox

The daemon only handles new mail. For the backlog there is a one-off tool you
run by hand. Without `--apply` it only reports; with it, a restore file is
written to `~/.config/gmail-triage/backups/` before anything changes.

| | |
|---|---|
| `.venv/bin/gmail-triage-cleanup archive --older-than 30d --mark-read` | take inbox mail older than 30 days out of the inbox; starred mail stays |
| `.venv/bin/gmail-triage-cleanup trash ~/backup/gmail --senders approved.txt` | move the census's delete candidates to Trash, only for the senders you list |
| `.venv/bin/gmail-triage-cleanup restore <restore file>` | undo one of the above |

`trash --people-only` is the blunt version: every automated message goes,
from any sender, and what stays is mail a person wrote, your own mail and the
threads you wrote in, starred mail, anything with a document attached, and
mail with a non-junk triage label. "A person wrote it" is a guess from the
sender's address and Gmail's tab; put anything you cannot lose in `--keep`.
`--drop drop.txt` names senders whose mail goes even though it looks personal,
and `--chats` lets saved chat logs go.

`approved.txt` is one sender address a line, picked from `senders.csv`.
`--wide-senders wide.txt` names senders that are nothing but newsletters or
job alerts: their bulk mail goes even from the Updates tab, where receipts and
statements also live, so name them with care. `--older-than 30d` leaves recent
mail alone. `--keep keep.txt` lists addresses or `@domains` that are never
trashed, whatever the other lists say. Gmail
empties Trash after 30 days, which is when the storage is freed. Archiving
frees none: archived mail stays in All Mail.

**This changes your real mailbox, at your own risk.** Run the backup first,
read the report before adding `--apply`, and keep the restore file. Mail that
Gmail has emptied from Trash cannot be restored by this tool, only re-imported
from your backup by hand.

## Failure behaviour

- Hung anywhere (socket, `claude` subprocess) → no `WATCHDOG=1` for 120 s → systemd kills and restarts it.
- Pub/Sub stream dies → the daemon exits 1 → restart in 10 s.
- Missed notification → the 15-minute safety sweep runs `history.list` anyway.
- Down for more than about a week → `history.list` returns 404 → falls back to
  `in:inbox newer_than:<gap>d has:nouserlabels`.
- OAuth token revoked or missing, bad `config.toml` or `profile.toml` → exit 78,
  which the unit excludes from `Restart=always` (a restart can't fix it).

## Development

`.venv/bin/pytest -q`. The tests run against an in-memory fake of the Gmail
client and never touch the network. [CLAUDE.md](CLAUDE.md) lists the
invariants the tests enforce.

## Licence

[MIT](LICENSE). The software is provided as is, with no warranty: the authors
are not responsible for lost, mislabelled, archived or deleted mail.
