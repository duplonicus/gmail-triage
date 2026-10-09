"""One-off mailbox cleanup, run by the owner by hand. The daemon never does this.

    gmail-triage-cleanup archive --older-than 30d [--mark-read] [--apply]
    gmail-triage-cleanup trash BACKUP_DIR --senders approved.txt [--apply]
    gmail-triage-cleanup restore RESTORE_FILE

archive  takes inbox mail older than the cutoff out of the inbox. Starred mail stays.
trash    moves the census's delete candidates to Trash, only for senders listed
         in the approved file (one address a line, # for comments). Gmail empties
         Trash after 30 days.
restore  undoes one archive or trash run from its restore file.

Without --apply, archive and trash only report what they would do. With it,
the restore file is written BEFORE anything changes.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import gmail as G
from .backup import LABELS, read_index
from .census import keep_reasons, replied_threads, sender
from .config import SECRETS_DIR
from .daemon import parse_days

log = logging.getLogger(__name__)

RESTORE_DIR = SECRETS_DIR / "backups"
CHUNK = 1000  # batchModify's limit
# Every change this module can make. batch_modify() refuses anything else.
ARCHIVE = {"removeLabelIds": ["INBOX"]}
ARCHIVE_READ = {"removeLabelIds": ["INBOX", "UNREAD"]}
TRASH = {"addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]}
UNARCHIVE = {"addLabelIds": ["INBOX"]}
UNREAD = {"addLabelIds": ["UNREAD"]}
UNTRASH = {"removeLabelIds": ["TRASH"]}
ALLOWED = (ARCHIVE, ARCHIVE_READ, TRASH, UNARCHIVE, UNREAD, UNTRASH)


def batch_modify(svc, ids: list[str], body: dict) -> None:
    """Idempotent, so a run that died part-way is finished by repeating it."""
    assert body in ALLOWED, body
    for i in range(0, len(ids), CHUNK):
        svc.users().messages().batchModify(userId="me", body={"ids": ids[i:i + CHUNK], **body}).execute(num_retries=G.RETRIES)


def plan_archive(svc, days: int, mark_read: bool) -> dict:
    base = f"in:inbox older_than:{days}d -is:starred"
    ids = G.list_ids(svc, base)
    unread = set(G.list_ids(svc, base + " is:unread")) & set(ids) if mark_read else set()
    return {"action": "archive", "older_than_days": days, "mark_read": mark_read, "ids": ids, "unread": sorted(unread)}


def read_senders(path: Path) -> set[str]:
    lines = (line.split("#", 1)[0].strip().lower() for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line}


def plan_trash(rows: list[dict], label_names: dict[str, str], approved: set[str],
               starred_now: set[str], inbox_now: set[str]) -> dict:
    """Delete candidates from approved senders. `starred_now` is the live
    starred set, so a star added after the backup still protects."""
    replied = replied_threads(rows)
    ids = [r["id"] for r in rows
           if sender(r) in approved and r["id"] not in starred_now and "TRASH" not in r["labels"]
           and not keep_reasons(r, label_names, replied)]
    return {"action": "trash", "ids": ids, "inbox": sorted(set(ids) & inbox_now)}


def save_restore(plan: dict, directory: Path = RESTORE_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan['action']}-{time.strftime('%Y-%m-%d-%H%M%S')}.json"
    path.write_text(json.dumps({"created": time.strftime("%Y-%m-%d %H:%M:%S"), **plan}))
    return path


def apply(svc, plan: dict) -> None:
    if plan["action"] == "archive":
        batch_modify(svc, plan["ids"], ARCHIVE_READ if plan["mark_read"] else ARCHIVE)
    elif plan["action"] == "trash":
        batch_modify(svc, plan["ids"], TRASH)
    else:
        raise ValueError(plan["action"])


def restore(svc, plan: dict) -> None:
    if plan["action"] == "archive":
        batch_modify(svc, plan["ids"], UNARCHIVE)
        batch_modify(svc, plan["unread"], UNREAD)
    elif plan["action"] == "trash":
        batch_modify(svc, plan["ids"], UNTRASH)
        batch_modify(svc, plan["inbox"], UNARCHIVE)
    else:
        raise ValueError(plan["action"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gmail-triage-cleanup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("archive")
    a.add_argument("--older-than", type=parse_days, required=True, metavar="30d")
    a.add_argument("--mark-read", action="store_true", help="also mark the archived mail read")
    t = sub.add_parser("trash")
    t.add_argument("backup", type=Path, help="a directory written by gmail-triage-backup")
    t.add_argument("--senders", type=Path, required=True, help="file of approved sender addresses")
    for p in (a, t):
        p.add_argument("--apply", action="store_true", help="make the change (default: report only)")
    r = sub.add_parser("restore")
    r.add_argument("file", type=Path)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    try:
        svc = G.build_service(G.load_credentials())
    except G.AuthError as e:
        log.error("%s", e)
        return 78
    if args.cmd == "restore":
        plan = json.loads(args.file.read_text())
        restore(svc, plan)
        log.info("restored %d messages from %s", len(plan["ids"]), args.file)
        return 0
    if args.cmd == "archive":
        plan = plan_archive(svc, args.older_than, args.mark_read)
        log.info("archive: %d inbox messages older than %d days (%d unread would be marked read)",
                 len(plan["ids"]), args.older_than, len(plan["unread"]))
    else:
        rows = read_index(args.backup)
        names = json.loads((args.backup / LABELS).read_text(encoding="utf-8"))
        approved = read_senders(args.senders)
        plan = plan_trash(rows, names, approved, set(G.list_ids(svc, "is:starred")), set(G.list_ids(svc, "in:inbox")))
        by_id = {r["id"]: r for r in rows}
        log.info("trash: %d messages, %.1f MB, from %d approved senders", len(plan["ids"]),
                 sum(by_id[i]["size"] for i in plan["ids"]) / 1e6, len(approved))
    if not args.apply:
        log.info("report only; add --apply to do it")
        return 0
    path = save_restore(plan)
    log.info("restore file: %s", path)
    apply(svc, plan)
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
