"""Census of a backup: who sent what, how big it is, and what would be safe to delete.

Reads only the files `gmail-triage-backup` wrote; never talks to Gmail and
changes nothing. Writes into the backup directory:

    senders.csv   one row per sender, biggest first
    census.md     totals, years, categories, largest messages, the star check

A message is a delete CANDIDATE only when `keep_reasons()` finds nothing to
keep it for. Deleting is a separate, deliberate step: this only reports.
"""
from __future__ import annotations

import argparse
import csv
import email.utils
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from .backup import LABELS, read_index

JUNK_CATEGORIES = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}
# Triage labels that do not make a message worth keeping.
JUNK_LABELS = {"Promos", "Newsletters"}
# A named attachment that is not a picture counts as a document.
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico", "tif", "tiff", "heic"}


def sender(row: dict) -> str:
    return email.utils.parseaddr(row.get("from", ""))[1].lower() or "(unknown)"


def has_document(row: dict) -> bool:
    return any(name.rsplit(".", 1)[-1].lower() not in IMAGE_EXTS for name in row.get("attachments", []))


def is_junk_label(name: str) -> bool:
    return name.split(" › ")[0] in JUNK_LABELS


def replied_threads(rows: list[dict]) -> set[str]:
    """Threads the owner wrote in."""
    return {r["thread"] for r in rows if "SENT" in r["labels"]}


def keep_reasons(row: dict, label_names: dict[str, str], replied: set[str], ignore_star: bool = False) -> list[str]:
    """Why this message must stay. Empty list = delete candidate."""
    labels = set(row["labels"])
    reasons = []
    if labels & {"SENT", "DRAFT"}:
        reasons.append("yours")
    if not row.get("list_unsubscribe"):
        reasons.append("not bulk")
    if not labels & JUNK_CATEGORIES:
        reasons.append("category")
    if has_document(row):
        reasons.append("document")
    if row["thread"] in replied:
        reasons.append("replied")
    if "STARRED" in labels and not ignore_star:
        reasons.append("starred")
    if any(not is_junk_label(label_names.get(l, l)) for l in labels if l.startswith("Label_")):
        reasons.append("labelled")
    return reasons


def year(row: dict) -> int:
    return time.gmtime(row["internal_date"] / 1000).tm_year


def by_sender(rows: list[dict], label_names: dict[str, str]) -> list[dict]:
    replied = replied_threads(rows)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[sender(r)].append(r)
    out = []
    for addr, msgs in groups.items():
        reasons = Counter()
        candidates = []
        for m in msgs:
            why = keep_reasons(m, label_names, replied)
            reasons.update(why)
            if not why:
                candidates.append(m)
        out.append({
            "sender": addr,
            "name": email.utils.parseaddr(msgs[0].get("from", ""))[0],
            "messages": len(msgs),
            "mb": round(sum(m["size"] for m in msgs) / 1e6, 2),
            "candidates": len(candidates),
            "candidate_mb": round(sum(m["size"] for m in candidates) / 1e6, 2),
            "kept": len(msgs) - len(candidates),
            "kept_because": ", ".join(f"{k} {v}" for k, v in reasons.most_common()),
            "starred": sum("STARRED" in m["labels"] for m in msgs),
            "first_year": min(year(m) for m in msgs),
            "last_year": max(year(m) for m in msgs),
        })
    return sorted(out, key=lambda s: (-s["candidate_mb"], -s["mb"], s["sender"]))


def star_check(rows: list[dict], label_names: dict[str, str]) -> list[dict]:
    """Starred messages the rules would delete if the star did not protect them."""
    replied = replied_threads(rows)
    return [r for r in rows if "STARRED" in r["labels"] and not keep_reasons(r, label_names, replied, ignore_star=True)]


def _table(header: list[str], lines: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in line) + " |" for line in lines]
    return "\n".join(out)


def report(rows: list[dict], label_names: dict[str, str], top: int = 40) -> str:
    senders = by_sender(rows, label_names)
    total_mb = sum(r["size"] for r in rows) / 1e6
    cand = sum(s["candidates"] for s in senders)
    cand_mb = sum(s["candidate_mb"] for s in senders)
    years: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    cats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        years[year(r)][0] += 1
        years[year(r)][1] += r["size"]
        cat = next((l[9:].lower() for l in r["labels"] if l.startswith("CATEGORY_")), "none")
        cats[cat][0] += 1
        cats[cat][1] += r["size"]
    big = sorted(rows, key=lambda r: -r["size"])[:top]
    misses = star_check(rows, label_names)
    date = lambda r: time.strftime("%Y-%m-%d", time.gmtime(r["internal_date"] / 1000))
    parts = [
        "# Mailbox census",
        f"{len(rows)} messages, {total_mb / 1000:.2f} GB, {len(senders)} senders. "
        f"Delete candidates: {cand} messages, {cand_mb / 1000:.2f} GB.",
        "## By year",
        _table(["year", "messages", "MB"], [[y, n, round(b / 1e6)] for y, (n, b) in sorted(years.items())]),
        "## By Gmail category",
        _table(["category", "messages", "MB"], [[c, n, round(b / 1e6)] for c, (n, b) in sorted(cats.items(), key=lambda x: -x[1][1])]),
        f"## Top {top} senders by delete-candidate size",
        _table(["sender", "messages", "MB", "candidates", "candidate MB", "kept because"],
               [[s["sender"], s["messages"], s["mb"], s["candidates"], s["candidate_mb"], s["kept_because"]] for s in senders[:top]]),
        f"## Top {top} senders by total size",
        _table(["sender", "messages", "MB", "candidates"],
               [[s["sender"], s["messages"], s["mb"], s["candidates"]] for s in sorted(senders, key=lambda s: -s["mb"])[:top]]),
        f"## {top} largest messages",
        _table(["MB", "date", "from", "subject", "attachments"],
               [[round(r["size"] / 1e6, 1), date(r), sender(r), r["subject"][:60], len(r["attachments"])] for r in big]),
        "## Star check",
        f"{sum('STARRED' in r['labels'] for r in rows)} starred; the rules would delete {len(misses)} of them without the star.",
        _table(["date", "from", "subject"], [[date(r), sender(r), r["subject"][:70]] for r in misses]) if misses else "",
    ]
    return "\n\n".join(p for p in parts if p) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gmail-triage-census", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", type=Path, help="a directory written by gmail-triage-backup")
    args = ap.parse_args(argv)
    rows = read_index(args.dest)
    if not rows:
        print(f"no index in {args.dest}; run gmail-triage-backup first", file=sys.stderr)
        return 1
    label_names = json.loads((args.dest / LABELS).read_text(encoding="utf-8"))
    senders = by_sender(rows, label_names)
    with open(args.dest / "senders.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(senders[0]))
        w.writeheader()
        w.writerows(senders)
    (args.dest / "census.md").write_text(report(rows, label_names), encoding="utf-8")
    print(f"{len(rows)} messages, {len(senders)} senders -> {args.dest / 'census.md'}, senders.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
