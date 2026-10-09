"""One-off mailbox cleanup, run by the owner by hand. The daemon never does this.

    gmail-triage-cleanup archive --older-than 30d [--mark-read] [--apply]
    gmail-triage-cleanup trash BACKUP_DIR --people-only --older-than 30d --keep keep.txt [--apply]
    gmail-triage-cleanup trash BACKUP_DIR --senders approved.txt
                               [--wide-senders wide.txt] [--older-than 30d] [--keep keep.txt] [--apply]
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
from .census import keep_reasons, keep_reasons_people, replied_threads, sender
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


def is_kept_sender(who: str, keep: set[str] | frozenset[str]) -> bool:
    """`keep` holds full addresses and "@domain" entries; a domain covers its subdomains."""
    domain = who.rsplit("@", 1)[-1]
    return who in keep or any(k.startswith("@") and (domain == k[1:] or domain.endswith("." + k[1:])) for k in keep)


def plan_trash(rows: list[dict], label_names: dict[str, str], approved: set[str],
               starred_now: set[str], inbox_now: set[str],
               wide: frozenset[str] | set[str] = frozenset(), before_ms: int | None = None,
               keep: frozenset[str] | set[str] = frozenset(), people_only: bool = False,
               drop: frozenset[str] | set[str] = frozenset(), chats: bool = False) -> dict:
    """Delete candidates from approved senders. `starred_now` is the live
    starred set, so a star added after the backup still protects.

    Senders in `wide` are judged with keep_reasons(wide=True). With
    `before_ms`, only mail received before that moment is taken. A sender
    matching `keep` is never trashed, whatever the other lists say.

    `people_only` switches to keep_reasons_people(): every sender is in
    scope and only mail a person wrote (or the other reasons there) stays.
    There, senders in `drop` are not treated as people, and with `chats`
    neither are saved chat logs (Gmail's CHAT label).
    """
    replied = replied_threads(rows)
    ids = []
    for r in rows:
        who = sender(r)
        if is_kept_sender(who, keep) or not (people_only or who in approved or who in wide):
            continue
        if r["id"] in starred_now or "TRASH" in r["labels"]:
            continue
        if before_ms is not None and r["internal_date"] >= before_ms:
            continue
        if people_only:
            dropped = who in drop or (chats and "CHAT" in r["labels"])
            why = keep_reasons_people(r, label_names, replied, as_person=not dropped)
        else:
            why = keep_reasons(r, label_names, replied, wide=who in wide)
        if not why:
            ids.append(r["id"])
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
    t.add_argument("--senders", type=Path, help="file of approved sender addresses")
    t.add_argument("--people-only", action="store_true",
                   help="trash all automated mail, from any sender; keep what people wrote")
    t.add_argument("--drop", type=Path, help="with --people-only: senders whose mail goes although a person may have written it")
    t.add_argument("--chats", action="store_true", help="with --people-only: saved chat logs go too")
    t.add_argument("--wide-senders", type=Path, help="file of pure-bulk senders (newsletters, job alerts): "
                   "their bulk mail goes whatever Gmail tab it is in")
    t.add_argument("--older-than", type=parse_days, metavar="30d", help="leave mail newer than this alone")
    t.add_argument("--keep", type=Path, help="file of addresses or @domains that are never trashed")
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
        if not args.senders and not args.people_only:
            ap.error("trash needs --senders or --people-only")
        approved = read_senders(args.senders) if args.senders else set()
        wide = read_senders(args.wide_senders) if args.wide_senders else set()
        before = int((time.time() - args.older_than * 86400) * 1000) if args.older_than else None
        plan = plan_trash(rows, names, approved, set(G.list_ids(svc, "is:starred")), set(G.list_ids(svc, "in:inbox")),
                          wide=wide, before_ms=before, keep=read_senders(args.keep) if args.keep else set(),
                          people_only=args.people_only, chats=args.chats,
                          drop=read_senders(args.drop) if args.drop else set())
        by_id = {r["id"]: r for r in rows}
        log.info("trash: %d messages, %.1f MB, from %d approved and %d wide senders", len(plan["ids"]),
                 sum(by_id[i]["size"] for i in plan["ids"]) / 1e6, len(approved), len(wide))
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
