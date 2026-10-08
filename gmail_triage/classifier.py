"""Classify a batch of messages with Haiku.

Contract
  input : [{id, from, subject, snippet, list_unsubscribe}]
  output: [{id, labels: [...], star: bool, reason}]   (strict JSON, validated)
  handed on with `star` settled by star_policy() and `important` added.

Invalid output is retried once for the affected messages, then those messages
are skipped. We never guess a label. A backend *failure* (CLI not logged in,
network, timeout) raises ClassifierError instead, so the caller does not
advance historyId and the messages are retried later.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Callable, Iterable

log = logging.getLogger(__name__)

# These are the exact Gmail label names. " › " is a flat separator, not
# nesting: Gmail treats "/" as a parent/child path, so no name may contain one.
PRIMARY = [
    "Jobs › Applied",
    "Jobs › Reply",
    "Jobs › Interview",
    "Jobs › Rejected",
    "Jobs › Alerts",
    "Jobs › Skip",
    "Money",
    "Money › Invoices",
    "Money › Receipts",
    "Money › Statements",
    "Money › Trading",
    "Money › Transfers",
    "Money › Taxes",
    "Money › Crypto",
    "Business",
    "Government",
    "Education",
    "Security",
    "Security › Codes",
    "Personal",
    "Appointments",
    "Health",
    "Orders",
    "Travel",
    "Notifications",
    "Newsletters",
    "Newsletters › Crypto",
    "Newsletters › AI",
    "Promos",
]
SUSPICIOUS = "Security › Suspicious"
ALL_LABELS = PRIMARY + [SUSPICIOUS]

# A star means the owner has something to do. Where the label alone settles
# that, the label wins over the model's `star`; the model only decides for the
# rest (bills, business, government, education, personal, ...).
ALWAYS_STAR = {"Jobs › Reply", "Jobs › Interview"}
NEVER_STAR = {
    "Jobs › Applied", "Jobs › Rejected", "Jobs › Alerts", "Jobs › Skip",
    "Money › Receipts", "Money › Statements",
    "Security", "Security › Codes",
    "Orders", "Notifications", "Newsletters", "Newsletters › Crypto", "Newsletters › AI", "Promos",
}
# Gmail's importance marker is ours to set: everything starred, plus mail worth
# the owner's eyes with nothing to do. Never the model's call.
ALWAYS_IMPORTANT = {"Jobs › Rejected", "Personal"}

SYSTEM_PROMPT = "You are an email triage classifier. You output strict JSON only: no prose, no code fences."

# The prompt is generic. Everything about one particular owner (who they are,
# their job targets, the senders they deal with) comes from their profile
# (profile.py, ~/.config/gmail-triage/profile.toml) and is slotted in under
# the label it refines. Nothing personal belongs in this file.
GROUPS = [
    ("Jobs", "", PRIMARY[:6]),
    ("Money", " (the owner's own money; a company's marketing is Promos even if it is a bank or broker)", PRIMARY[6:14]),
    ("Other", "", PRIMARY[14:] + [SUSPICIOUS]),
]

DEFINITIONS = {
    "Jobs › Applied": 'AUTOMATED application confirmations only ("we received your application", "thank you for applying") and candidate-account setup on job sites (Workday, Greenhouse, Lever, iCIMS, Ashby). One-time codes from job sites are Security › Codes.',
    "Jobs › Reply": "a real person at an employer or recruiter wrote to the owner: a question, an assessment or take-home, a screening request. Not an auto-confirmation.",
    "Jobs › Interview": "an interview invitation or a request to schedule one (including scheduling links from ATS tools on the employer's behalf).",
    "Jobs › Rejected": 'a rejection ("we have decided to move forward with other candidates", "not moving forward", "position has been filled"), automated or not.',
    "Jobs › Alerts": 'job alerts and job-alert digests ("your job alert for ...", "See your latest job matches") that fit the owner\'s job targets; never Notifications. If the owner states no targets, every job alert is Jobs › Alerts. LOCATION RULE: if the email does not state the location, the location is NOT a reason to skip.',
    "Jobs › Skip": 'job alerts that EXPLICITLY break one of the owner\'s stated job rules (never skip on a guess, e.g. about location). If the owner states no rules, never use this label. Job-board marketing and upsells (e.g. "Dream Job" promotions, Glassdoor/Indeed news) are Promos, not Jobs.',
    "Money": "other money matters that fit none of the Money › labels (e.g. insurance policy documents, loan or credit-limit changes to the owner's account, refunds, rebate confirmations).",
    "Money › Invoices": "bills and invoices: an amount owed or billed, a bill or invoice ready to view, upcoming charges, minimum payment due (utilities, credit-card payment due, subscription invoices).",
    "Money › Receipts": 'something the owner already paid or bought: receipts, payment confirmations, order totals, "thank you for your purchase" (store, game and app purchases, food orders, PayPal receipts, "payment received").',
    "Money › Statements": "bank, credit-card and brokerage statements or eStatement notices.",
    "Money › Trading": "stock and fund trading: orders filled or cancelled, price/volume alerts, currency-exchange requests, maturing instruments, corporate actions.",
    "Money › Transfers": "money moving between people or accounts: e-transfers and peer-to-peer payments sent or received, deposits, withdrawals, direct deposits.",
    "Money › Taxes": "tax slips and forms, tax-agency mail and notices of assessment, tax-filing software mail about the owner's return.",
    "Money › Crypto": "mail from crypto exchanges and wallets (Coinbase, Kraken, Binance, etc.) about the owner's money: deposits, withdrawals, trades, balances, statements. Their login/new-device/password alerts are Security; their codes are Security › Codes; their marketing (yield offers, contests, rewards, new products) is Promos.",
    "Business": "a business or side project the owner runs, as named in the owner's note, other than its invoices (those are Money › Invoices): payment-processor account and compliance requests, payouts, disputes, refunds, customer mail, hosting/domain/app-store notices for it. With no owner's note here, never use this label.",
    "Government": "mail from government agencies other than tax (benefits, passport, driver's licence, health card, elections, courts).",
    "Education": "schools, colleges, universities and online learning platforms the owner studies with: registrar, admissions and enrolment mail, course, assignment and lab notices, grades, transcripts, certificates and course progress. Mail personally written by an instructor or school staff is Education, not Personal. Their marketing and upsells are Promos; tuition bills are Money › Invoices.",
    "Security": 'new or successful logins, new devices, passkeys added, OAuth/app-access grants (Google "Security alert", "You shared some Google Account data with X", GitHub "third-party OAuth application added"), OAuth-client housekeeping, password resets or changes, account-security settings reminders. These are ALWAYS Security, never Notifications, even when no action is needed.',
    "Security › Codes": 'one-time security/verification/MFA codes, magic sign-in links, and "confirm your email" sign-up verifications, from any sender.',
    "Personal": 'mail personally written by an actual human to the owner (not about jobs, the owner\'s business or the owner\'s schooling). Never automated mail, even if it names a person or group (e.g. "invitation from Org X" is Notifications).',
    "Appointments": "bookings, confirmations and reminders for a specific date/time (doctor, dentist, vet, services), and calendar reminders including birthdays.",
    "Health": "health, medical, pharmacy and pet/vet mail that is not a booking: prescriptions ready, test results, clinic notices, vet records or documentation.",
    "Orders": "shipping and delivery of things the owner ordered: order shipped, out for delivery, delayed, delivered, ready for pickup, returns. (The payment receipt itself is Money › Receipts.)",
    "Travel": "flights, hotels, car rentals, trips and itineraries booked by the owner. Travel deals and ads are Promos.",
    "Notifications": 'automated account or service notices that need no action: subscription/membership started, changed or cancelled, platform or organization invitations, app activity ("1 new request", "new comment"), usage/weekly reports, product release notes for tools the owner uses, welcome/onboarding mail.',
    "Newsletters": "editorial newsletters and author subscriptions (Substack, beehiiv, etc.) not about crypto or AI. Label a newsletter by its publication's usual beat, not one issue's topic.",
    "Newsletters › Crypto": "crypto newsletters and research subscriptions. Not exchanges: those are Money › Crypto.",
    "Newsletters › AI": "AI news and AI newsletters, and AI product update/tips mail (Claude, OpenAI, etc.) that is not about the owner's account or billing.",
    "Promos": 'marketing, sales, discounts, loyalty points and rewards, wishlist sales, surveys and feedback requests, "how did we do?", terms-of-service and privacy-policy updates.',
    SUSPICIOUS: "lookalikes/phishing where a brand named in the subject does not match the sending domain. Bulk-mail senders (*.ccsend.com / Constant Contact, Mailchimp, beehiiv, Substack) are normal for newsletters and are NOT a mismatch on their own. Security › Suspicious may appear alone or together with exactly one primary label.",
}

TIE_BREAKS = "TIE-BREAKS: phishing check first; then Jobs; then Security/Security › Codes (a login alert or code is Security even from a bank, exchange or job site); then marketing is Promos regardless of sender; then the most specific Money › label; plain Money and plain Notifications are last resorts."

STAR_RULE = 'STAR (star=true) only mail the owner will have to come back to because there is something to DO: reply, schedule, pay, submit or fix. Jobs › Reply and Jobs › Interview are always starred. Otherwise star only: human-written personal mail, a bill with a payment due that is not automatic, and Money, Business, Government or Education mail that explicitly asks the owner for action (e.g. Stripe "[Action required]", disputes, customer questions, a broker\'s "your action required"). Everything else star=false. Never star mail that only informs, however urgent it sounds: ALL Security and Security › Codes mail (logins, new devices, passkeys, app-access grants, security reminders: the owner triggered them and the label is enough), rejections, application confirmations, job alerts, receipts, statements, "upcoming invoice" notices, shipping updates, notifications, newsletters and promos.'

OUTPUT_RULE = """OUTPUT: a JSON array with one object per input email, same ids:
[{"id": "<id>", "labels": ["<label>", ...], "star": true|false, "reason": "<max 12 words>"}]"""

EMAILS_MARK = "\n\nEMAILS:\n"


def build_rules(owner: str = "", notes: dict[str, str] | None = None, general: str = "") -> str:
    """The whole prompt up to the emails. With no arguments it is the generic one."""
    notes = notes or {}
    unknown = sorted(set(notes) - set(ALL_LABELS))
    if unknown:
        raise ValueError(f"notes for unknown labels {unknown}")
    out = [f"Classify each email into Gmail labels for the account owner{f' ({owner})' if owner else ''}.", ""]
    out.append('LABELS (exactly one primary label per email). A sub-label REPLACES its parent: output "Security › Codes", '
               'never "Security" and "Security › Codes" together; likewise for Money › and Newsletters › labels.')
    if notes:
        out.append("An OWNER'S NOTE under a label is the owner's own specifics: it refines that label's definition "
                   "and wins where the two conflict.")
    for title, blurb, labels in GROUPS:
        out += ["", title + blurb]
        for label in labels:
            out.append(f"- {label}: {DEFINITIONS[label]}")
            if label in notes:
                out.append(f"  OWNER'S NOTE: {notes[label]}")
    out += ["", TIE_BREAKS, "", STAR_RULE]
    if general:
        out += ["", f"OWNER'S GENERAL NOTES: {general}"]
    out += ["", OUTPUT_RULE]
    return "\n".join(out) + EMAILS_MARK


RULES = build_rules()


class ClassifierError(RuntimeError):
    """The backend could not produce any output (not the same as bad output)."""


def build_prompt(items: list[dict], rules: str = RULES) -> str:
    return rules + json.dumps(items, ensure_ascii=False, indent=1)


def extract_json(text: str):
    """Parse the model's text as JSON, tolerating a stray ```json fence."""
    t = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, flags=re.S)
    if m:
        t = m.group(1)
    return json.loads(t)


def validate_decision(d, expected_ids: set[str]) -> str | None:
    """Return None if `d` is a valid decision, else a short reason it is not."""
    if not isinstance(d, dict):
        return "not an object"
    if set(d) != {"id", "labels", "star", "reason"}:
        return f"keys {sorted(d)}"
    if d["id"] not in expected_ids:
        return f"unknown id {d['id']!r}"
    labels = d["labels"]
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels):
        return "labels not a non-empty list of strings"
    if len(set(labels)) != len(labels):
        return "duplicate labels"
    unknown = [x for x in labels if x not in ALL_LABELS]
    if unknown:
        return f"unknown labels {unknown}"
    primaries = [x for x in labels if x in PRIMARY]
    if len(primaries) > 1:
        return f"more than one primary label {primaries}"
    if SUSPICIOUS not in labels and len(primaries) != 1:
        return "no primary label"
    if not isinstance(d["star"], bool):
        return "star not a bool"
    if not isinstance(d["reason"], str):
        return "reason not a string"
    return None


def star_policy(labels: list[str], star: bool) -> bool:
    if SUSPICIOUS in labels or NEVER_STAR & set(labels):
        return False
    return star or bool(ALWAYS_STAR & set(labels))


def important_policy(labels: list[str], starred: bool) -> bool:
    if SUSPICIOUS in labels:
        return False
    return starred or bool(ALWAYS_IMPORTANT & set(labels))


def archive_policy(labels: list[str], starred: bool, important: bool, archive_labels: frozenset[str]) -> bool:
    """Take the message out of the inbox? Only for labels the owner listed in
    config, and never mail that is starred, important or suspicious: those are
    the ones meant to be seen."""
    if starred or important or SUSPICIOUS in labels:
        return False
    return bool(archive_labels & set(labels))


def parse_output(text: str, expected_ids: set[str]) -> tuple[dict[str, dict], dict[str, str]]:
    """Split model output into (valid decisions by id, invalid reason by id).

    Any id that is missing, duplicated or malformed lands in the invalid map.
    """
    try:
        data = extract_json(text)
    except (json.JSONDecodeError, ValueError) as e:
        return {}, {i: f"unparseable JSON ({e.__class__.__name__})" for i in expected_ids}
    if not isinstance(data, list):
        return {}, {i: "top level is not a list" for i in expected_ids}

    good: dict[str, dict] = {}
    bad: dict[str, str] = {}
    seen: set[str] = set()
    for d in data:
        why = validate_decision(d, expected_ids)
        did = d.get("id") if isinstance(d, dict) else None
        if isinstance(did, str) and did in seen:
            bad[did] = "duplicate id in output"
            good.pop(did, None)
            continue
        if isinstance(did, str):
            seen.add(did)
        if why is None:
            star = star_policy(d["labels"], d["star"])
            good[did] = {"id": did, "labels": d["labels"], "star": star,
                         "important": important_policy(d["labels"], star), "reason": d["reason"].strip()}
        elif isinstance(did, str) and did in expected_ids:
            bad[did] = why
    for i in expected_ids - seen:
        bad[i] = "missing from output"
    return good, bad


# --------------------------------------------------------------------------- backends

Ping = Callable[[], None]


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    # Never let the CLI fall back to API billing: OAuth subscription login only.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # Haiku's extended thinking is pure latency here: measured 2026-09-28 on 20
    # real messages, 8,124 thinking tokens and 104 s vs 0 and ~12 s without,
    # with identical labels (20/20).
    env["MAX_THINKING_TOKENS"] = "0"
    return env


def cli_command(model: str) -> list[str]:
    # Measured 2026-09-28: without these flags a call carries ~27.8k input
    # tokens of Claude Code system prompt/tools/CLAUDE.md; with them, ~424.
    # (--bare is NOT usable: it refuses OAuth and requires an API key.)
    return [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt", SYSTEM_PROMPT,
    ]


def run_cli(prompt: str, model: str, timeout: float, ping: Ping, cwd: str | None = None) -> str:
    """Run `claude -p`, feeding the prompt on stdin; ping the watchdog while waiting."""
    try:
        proc = subprocess.Popen(
            cli_command(model), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=_cli_env(), cwd=cwd,
        )
    except FileNotFoundError as e:
        raise ClassifierError("claude CLI not found on PATH") from e
    proc.stdin.write(prompt)
    proc.stdin.close()
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            ping()
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise ClassifierError(f"claude -p timed out after {timeout:.0f}s")
    out, err = proc.stdout.read(), proc.stderr.read()
    if proc.returncode != 0:
        raise ClassifierError(f"claude -p exit {proc.returncode}: {(err or out).strip()[:300]}")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError as e:
        raise ClassifierError(f"claude -p printed non-JSON envelope: {out[:200]!r}") from e
    if envelope.get("is_error"):
        raise ClassifierError(f"claude -p error: {str(envelope.get('result'))[:300]}")
    usage = envelope.get("usage") or {}
    log.info("claude -p: in=%s cache_read=%s out=%s", usage.get("input_tokens"),
             usage.get("cache_read_input_tokens"), usage.get("output_tokens"))
    return envelope.get("result") or ""


def load_api_key(env_file) -> str:
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise ClassifierError(f"ANTHROPIC_API_KEY not found in {env_file}")


def run_api(prompt: str, model: str, timeout: float, api_key: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
    try:
        msg = client.messages.create(
            model=model, max_tokens=4096, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        raise ClassifierError(f"anthropic API: {e}") from e
    log.info("api: in=%s out=%s", msg.usage.input_tokens, msg.usage.output_tokens)
    return "".join(b.text for b in msg.content if b.type == "text")


# --------------------------------------------------------------------------- orchestration

class Classifier:
    def __init__(self, cfg, ping: Ping = lambda: None, runner: Callable[[str], str] | None = None,
                 rules: str = RULES):
        self.cfg = cfg
        self.ping = ping
        self.rules = rules
        if runner is not None:
            self._run = runner
        elif cfg.backend == "cli":
            from .config import SECRETS_DIR
            # Run from a directory with no CLAUDE.md so nothing project-level is discovered.
            SECRETS_DIR.mkdir(parents=True, exist_ok=True)
            self._run = lambda p: run_cli(p, cfg.cli_model, cfg.timeout_seconds, self.ping, cwd=str(SECRETS_DIR))
        else:
            from .config import ENV_FILE
            key = load_api_key(ENV_FILE)
            self._run = lambda p: run_api(p, cfg.api_model, cfg.timeout_seconds, key)

    def classify(self, items: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
        """Classify items in chunks. Returns (decisions by id, skipped reason by id)."""
        decisions: dict[str, dict] = {}
        skipped: dict[str, str] = {}
        for chunk in _chunks(items, self.cfg.max_batch):
            good, bad = self._classify_chunk(chunk)
            decisions.update(good)
            skipped.update(bad)
            self.ping()
        return decisions, skipped

    def _classify_chunk(self, chunk: list[dict]) -> tuple[dict[str, dict], dict[str, str]]:
        ids = {m["id"] for m in chunk}
        good, bad = parse_output(self._run(build_prompt(chunk, self.rules)), ids)
        if bad:
            log.warning("invalid classifier output for %d/%d, retrying once: %s",
                        len(bad), len(ids), sorted(set(bad.values()))[:3])
            retry = [m for m in chunk if m["id"] in bad]
            good2, bad2 = parse_output(self._run(build_prompt(retry, self.rules)), set(bad))
            good.update(good2)
            bad = bad2
        return good, bad


def _chunks(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]
