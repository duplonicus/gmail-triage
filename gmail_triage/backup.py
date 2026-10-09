"""Back up a mailbox to disk: one raw .eml per message plus an index.

Read-only against Gmail. Layout under DEST:

    messages/<last two chars of id>/<id>.eml   the message exactly as Gmail holds it
    index.jsonl                                one row per message (labels, size, headers)
    labels.json                                label id -> name, as of the last run
    complete.json                              written LAST; absent means unfinished

Resumable: a message is done once its row is in index.jsonl (the .eml is
written first, atomically), so repeating the command fetches only what is
missing. --force starts over.
"""
from __future__ import annotations

import argparse
import base64
import email
import email.policy
import email.utils
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from googleapiclient.errors import HttpError

from . import gmail as G

log = logging.getLogger(__name__)

INDEX = "index.jsonl"
LABELS = "labels.json"
COMPLETE = "complete.json"
WORKERS = 4
# Gmail's quota page says 6,000 units a minute per user at 20 a messages.get,
# i.e. 300 a minute (developers.google.com/workspace/gmail/api/reference/quota,
# read 2026-10-08). Measured the same day with format=raw: rateLimitExceeded
# from about 130 a minute. Stay under what was measured, so a running daemon
# keeps its share of the allowance.
PER_MINUTE = 120
RATE_LIMIT_WAITS = 10
# A 25 MB message is ~35 MB of base64 in one response.
TIMEOUT = 180


def eml_path(dest: Path, mid: str) -> Path:
    return dest / "messages" / mid[-2:] / f"{mid}.eml"


def _text(value) -> str:
    try:
        return " ".join(str(value or "").split())
    except Exception:  # a header too broken for the parser to render
        return ""


def attachment_names(msg) -> list[str]:
    names: list[str] = []
    for part in msg.walk():
        try:
            name = part.get_filename()
        except Exception:
            name = None
        if name:
            names.append(_text(name))
    return names


def index_row(resource: dict, raw: bytes) -> dict:
    """What the census needs from one message, so it never has to reopen the .eml."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        get = lambda name: _safe_header(msg, name)
        attachments = attachment_names(msg)
    except Exception:
        get, attachments = (lambda name: ""), []
    return {
        "id": resource["id"],
        "thread": resource.get("threadId", ""),
        "labels": sorted(resource.get("labelIds", [])),
        "internal_date": int(resource.get("internalDate", 0)),
        "size": int(resource.get("sizeEstimate", len(raw))),
        "from": get("From"),
        "to": get("To"),
        "subject": get("Subject"),
        "list_unsubscribe": bool(get("List-Unsubscribe")),
        "attachments": attachments,
    }


def _safe_header(msg, name: str) -> str:
    try:
        return _text(msg.get(name))
    except Exception:
        return ""


def read_index(dest: Path) -> list[dict]:
    """Rows of index.jsonl. A torn last line (killed mid-write) is dropped."""
    path = dest / INDEX
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class Pacer:
    """Spaces calls evenly across threads: at most `per_minute` of them a minute."""

    def __init__(self, per_minute: float, clock=time.monotonic, sleep=time.sleep):
        self.gap, self.clock, self.sleep = 60.0 / per_minute, clock, sleep
        self.lock = threading.Lock()
        self.next = clock()

    def wait(self) -> None:
        with self.lock:
            now = self.clock()
            at = max(self.next, now)
            self.next = at + self.gap
        if at > now:
            self.sleep(at - now)


def fetch_raw(svc, mid: str, sleep=time.sleep) -> tuple[dict, bytes] | None:
    """(resource, raw bytes), or None when the message vanished in between.

    Out of quota is waited out, not fatal: the allowance refills every minute.
    """
    for attempt in range(RATE_LIMIT_WAITS + 1):
        try:
            r = svc.users().messages().get(userId="me", id=mid, format="raw").execute(num_retries=G.RETRIES)
        except HttpError as e:
            if e.resp.status == 404:
                return None
            if e.resp.status not in (403, 429) or attempt == RATE_LIMIT_WAITS:
                raise
            sleep(30)
            continue
        return r, base64.urlsafe_b64decode(r.pop("raw"))


def run(dest: Path, ids: Iterable[str], fetch: Callable[[str], tuple[dict, bytes] | None],
        force: bool = False, workers: int = WORKERS,
        progress: Callable[[int, int], None] = lambda done, total: None) -> dict:
    """Back up `ids` into dest; returns the counts written to complete.json."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / COMPLETE).unlink(missing_ok=True)
    if force:
        (dest / INDEX).unlink(missing_ok=True)
    ids = list(dict.fromkeys(ids))
    kept = read_index(dest)
    if kept:  # rewrite, so a torn last line cannot swallow the next row
        write_atomic(dest / INDEX, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept).encode())
    have = {r["id"] for r in kept}
    todo = [i for i in ids if i not in have]
    fetched = gone = 0
    with open(dest / INDEX, "a", encoding="utf-8") as index, ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(fetch, mid): mid for mid in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            got = fut.result()
            if got is None:
                gone += 1
            else:
                resource, raw = got
                write_atomic(eml_path(dest, resource["id"]), raw)
                index.write(json.dumps(index_row(resource, raw), ensure_ascii=False) + "\n")
                index.flush()
                fetched += 1
            progress(n, len(todo))
    rows = read_index(dest)
    indexed = {r["id"] for r in rows}
    summary = {
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "listed": len(ids),
        "fetched_this_run": fetched,
        "gone_this_run": gone,
        "in_index": len(indexed),
        "missing": len(set(ids) - indexed),
        "bytes": sum(r["size"] for r in rows),
    }
    write_atomic(dest / COMPLETE, json.dumps(summary, indent=2).encode())
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gmail-triage-backup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", type=Path, help="directory to back up into")
    ap.add_argument("--query", default="", help="Gmail search to limit the backup (default: all mail)")
    ap.add_argument("--per-minute", type=float, default=PER_MINUTE, help=f"messages fetched a minute (default {PER_MINUTE})")
    ap.add_argument("--force", action="store_true", help="refetch everything, ignoring what is already there")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    try:
        creds = G.load_credentials()
    except G.AuthError as e:
        log.error("%s", e)
        return 78
    svc = G.build_service(creds, timeout=TIMEOUT)
    labels = svc.users().labels().list(userId="me").execute(num_retries=G.RETRIES).get("labels", [])
    args.dest.mkdir(parents=True, exist_ok=True)
    write_atomic(args.dest / LABELS, json.dumps({l["id"]: l["name"] for l in labels}, indent=2, ensure_ascii=False).encode())
    ids = G.list_ids(svc, args.query)
    log.info("%d messages listed", len(ids))

    # httplib2 connections are not thread-safe: one client per worker thread.
    local = threading.local()

    pacer = Pacer(args.per_minute)

    def fetch(mid: str):
        if not hasattr(local, "svc"):
            local.svc = G.build_service(creds, timeout=TIMEOUT)
        pacer.wait()
        return fetch_raw(local.svc, mid)

    def progress(done: int, total: int) -> None:
        if done % 500 == 0 or done == total:
            log.info("%d / %d", done, total)

    summary = run(args.dest, ids, fetch, force=args.force, progress=progress)
    log.info("done: %d in index (%d fetched now, %d gone), %.2f GB", summary["in_index"],
             summary["fetched_this_run"], summary["gone_this_run"], summary["bytes"] / 1e9)
    return 0 if summary["missing"] == summary["gone_this_run"] else 1


if __name__ == "__main__":
    sys.exit(main())
