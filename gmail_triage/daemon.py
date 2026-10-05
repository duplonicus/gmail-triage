"""gmail-triage daemon: Gmail watch -> Pub/Sub streaming pull -> history.list -> Haiku -> modify.

  gmail-triage                 run forever (systemd: Type=notify, watchdog)
  gmail-triage --once          catch up from the saved historyId, then exit
  gmail-triage --backfill 30d  triage untriaged inbox mail from the last 30 days first
  gmail-triage --dry-run       log decisions, change nothing (no labels, no stars, no importance, no state)
"""
from __future__ import annotations

import argparse
import logging
import queue
import re
import sys
import time
from logging.handlers import RotatingFileHandler

from . import config as C
from . import gmail as G
from . import profile as P
from . import sdnotify
from .classifier import Classifier, ClassifierError
from .state import State

log = logging.getLogger("gmail_triage")
EX_CONFIG = 78  # systemd unit has RestartPreventExitStatus=78: needs a human, don't loop


def setup_logging() -> logging.Logger:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("googleapiclient.discovery_cache", "google.cloud.pubsub_v1", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    C.LOG_DIR.mkdir(exist_ok=True)
    tl = logging.getLogger("triage")
    tl.propagate = False
    tl.setLevel(logging.INFO)
    h = RotatingFileHandler(C.LOG_DIR / "triage.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    tl.addHandler(h)
    return tl


def _clip(s: str, n: int) -> str:
    # " | " is the triage.log column separator; real subjects contain it.
    s = " ".join(s.split()).replace("|", "¦")
    return s if len(s) <= n else s[: n - 1] + "…"


class Triage:
    def __init__(self, svc, cfg: C.Config, classifier: Classifier, label_ids: dict[str, str],
                 state: State, dry_run: bool, tlog: logging.Logger, ping=sdnotify.watchdog):
        self.svc, self.cfg, self.classifier = svc, cfg, classifier
        self.label_ids, self.state, self.dry_run, self.tlog, self.ping = label_ids, state, dry_run, tlog, ping
        self.triage_ids = set(label_ids.values())

    def process_ids(self, ids: list[str]) -> int:
        """Classify and label these messages. Raises ClassifierError/HttpError on failure."""
        if not ids:
            return 0
        msgs = [m for m in G.fetch_metadata(self.svc, ids, self.ping) if G.needs_triage(m, self.triage_ids)]
        if not msgs:
            return 0
        by_id = {m["id"]: m for m in msgs}
        decisions, skipped = self.classifier.classify([G.to_item(m) for m in msgs])
        prefix = "DRY " if self.dry_run else ""
        for mid, m in by_id.items():
            frm, subj = _clip(G.header(m, "From"), 60), _clip(G.header(m, "Subject"), 90)
            if mid in skipped:
                self.tlog.info("%sSKIP | %s | %s | invalid classifier output: %s", prefix, frm, subj, skipped[mid])
                continue
            d = decisions[mid]
            if not self.dry_run:
                G.apply(self.svc, mid, G.modify_body(d, self.label_ids))
            self.tlog.info("%s%s | %s | %s | star=%s | important=%s | %s", prefix, frm, subj,
                           ",".join(d["labels"]), "yes" if d["star"] else "no",
                           "yes" if d["important"] else "no", _clip(d["reason"], 100))
            self.ping()
        log.info("triaged %d, skipped %d", len(decisions), len(skipped))
        return len(decisions)

    def sync(self) -> int:
        """Process everything since the saved historyId, then advance it."""
        start = self.state.history_id
        try:
            ids, latest = G.new_inbox_ids_since(self.svc, start)
        except G.HistoryTooOld:
            days = G.gap_days(self.state.last_success)
            q = f"in:inbox newer_than:{days}d has:nouserlabels"
            log.warning("historyId %s too old (404); falling back to messages.list q=%r", start, q)
            latest = G.current_history_id(self.svc)
            ids = G.list_ids(self.svc, q)
        n = self.process_ids(ids)
        self.state.update(history_id=latest, last_success=time.time())
        return n

    def backfill(self, days: int) -> int:
        ids = G.list_ids(self.svc, f"in:inbox newer_than:{days}d")
        log.info("backfill: %d inbox messages in the last %dd (already-triaged ones are skipped)", len(ids), days)
        total = 0
        for i in range(0, len(ids), self.cfg.max_batch):
            total += self.process_ids(ids[i:i + self.cfg.max_batch])
        return total


def parse_days(s: str) -> int:
    m = re.fullmatch(r"(\d+)d?", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"expected e.g. 30d, got {s!r}")
    return int(m.group(1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gmail-triage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="log decisions, change nothing")
    ap.add_argument("--backfill", type=parse_days, metavar="30d", help="triage inbox mail from the last N days first")
    ap.add_argument("--once", action="store_true", help="catch up once and exit (no Pub/Sub)")
    args = ap.parse_args(argv)

    tlog = setup_logging()
    try:
        cfg = C.load()
        profile = P.load()
        creds = G.load_credentials()
    except (C.ConfigError, P.ProfileError, G.AuthError) as e:
        log.error("%s", e)
        return EX_CONFIG
    svc = G.build_service(creds)
    label_ids = G.ensure_labels(svc, create=not args.dry_run)
    state = State(C.STATE_FILE, persist=not args.dry_run)
    try:
        classifier = Classifier(cfg, ping=sdnotify.watchdog, rules=profile.rules)
    except ClassifierError as e:
        log.error("%s", e)
        return EX_CONFIG
    t = Triage(svc, cfg, classifier, label_ids, state, args.dry_run, tlog)
    if args.dry_run:
        log.info("DRY RUN: no labels, stars or state will be written")
    if profile == P.Profile():
        log.info("no owner profile at %s: using the generic rules", C.PROFILE_FILE)
    else:
        log.info("owner profile: %d label notes from %s", len(profile.notes), C.PROFILE_FILE)

    if not state.history_id:
        hid = G.current_history_id(svc)
        log.info("first run: starting from current historyId %s (use --backfill for older mail)", hid)
        state.update(history_id=hid, last_success=time.time())

    if args.backfill:
        t.backfill(args.backfill)

    if args.once:
        n = t.sync()
        log.info("--once: triaged %d", n)
        return 0

    return run_forever(t, cfg, creds, state)


def run_forever(t: Triage, cfg: C.Config, creds, state: State) -> int:
    from google.auth.exceptions import RefreshError
    from google.cloud import pubsub_v1

    notes: queue.Queue = queue.Queue()

    def on_message(msg):
        notes.put(msg.data)
        msg.ack()  # the notification only says "something changed"; state lives in historyId

    subscriber = pubsub_v1.SubscriberClient(credentials=creds)
    future = subscriber.subscribe(cfg.subscription_path, callback=on_message,
                                  flow_control=pubsub_v1.types.FlowControl(max_messages=100))
    G.watch(t.svc, cfg.topic_path)
    state.update(last_watch=time.time())

    sdnotify.ready("listening")
    log.info("listening on %s", cfg.subscription_path)

    pending_since: float | None = time.monotonic()  # startup catch-up runs through the same path
    next_retry = 0.0
    last_sweep = time.monotonic()
    try:
        while True:
            sdnotify.watchdog()
            if future.done():
                log.error("Pub/Sub streaming pull ended: %r; exiting for systemd restart", future.exception())
                return 1
            try:
                notes.get(timeout=1)
                if pending_since is None:
                    pending_since = time.monotonic()
                while not notes.empty():
                    notes.get_nowait()
            except queue.Empty:
                pass

            now = time.monotonic()
            if now - last_sweep >= cfg.safety_sweep_minutes * 60 and pending_since is None:
                pending_since = now - cfg.debounce_seconds
            if pending_since is not None and now - pending_since >= cfg.debounce_seconds and now >= next_retry:
                try:
                    t.sync()
                    pending_since = None
                    last_sweep = now
                    sdnotify.status(f"ok; last sync {time.strftime('%H:%M:%S')}")
                except RefreshError as e:
                    log.error("OAuth token refresh failed (%s); re-run gmail-triage-auth", e)
                    return EX_CONFIG
                except Exception as e:  # noqa: BLE001 - keep the daemon alive, retry
                    log.exception("sync failed (%s); retrying in 60s", e.__class__.__name__)
                    next_retry = now + 60
                    sdnotify.status(f"sync failing: {e.__class__.__name__}")

            if time.time() - state.last_watch >= cfg.watch_renew_hours * 3600:
                try:
                    G.watch(t.svc, cfg.topic_path)
                    state.update(last_watch=time.time())
                except Exception:  # noqa: BLE001
                    log.exception("watch renewal failed; retrying next loop in 10 min")
                    state.data["last_watch"] = time.time() - cfg.watch_renew_hours * 3600 + 600
    finally:
        future.cancel()
        subscriber.close()
