"""Gmail API: credentials, labels, watch, history, fetch, apply."""
from __future__ import annotations

import logging
import math
import time
from typing import Callable

from googleapiclient.errors import HttpError

from .classifier import ALL_LABELS

log = logging.getLogger(__name__)

STARRED = "STARRED"
IMPORTANT = "IMPORTANT"
INBOX = "INBOX"
META_HEADERS = ["From", "To", "Subject", "Date", "List-Unsubscribe"]
# Messages carrying any of these are never touched.
UNTOUCHABLE = {"SPAM", "TRASH", "DRAFT"}
# googleapiclient retries DNS failures, socket timeouts, 429 and 5xx with
# exponential backoff (~1+2+4+8 s) — but ONLY when num_retries is passed.
# This WSL box's resolver intermittently drops queries (5 s stalls, outright
# ServerNotFoundError); without this one blip killed the first real dry run.
RETRIES = 4


class AuthError(RuntimeError):
    """Token missing, revoked or expired: needs a human, not a restart."""


def load_credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from .config import SCOPES, TOKEN_FILE

    if not TOKEN_FILE.exists():
        raise AuthError(f"{TOKEN_FILE} missing; run: .venv/bin/gmail-triage-auth")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthError(f"token refresh failed ({e}); re-run gmail-triage-auth") from e
    return creds


def build_service(creds, timeout: int = 30):
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build

    # httplib2 defaults to no timeout; a hung socket would otherwise stall the
    # main loop until the systemd watchdog kills us.
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
    return build("gmail", "v1", http=http, cache_discovery=False)


# Gmail's label palette is fixed; these are all members of it (a value outside
# it is a 400). One color per family; low-priority labels are grey so they fade.
_FAMILY = {  # label prefix -> (background, text)
    "Jobs": ("#16a766", "#ffffff"),
    "Money": ("#fad165", "#000000"),
    "Security": ("#cc3a21", "#ffffff"),
    "Newsletters": ("#cccccc", "#000000"),
}
LABEL_COLORS: dict[str, tuple[str, str]] = {  # name -> (background, text)
    **{l: _FAMILY[l.split(" › ")[0]] for l in ALL_LABELS if l.split(" › ")[0] in _FAMILY},
    "Security › Codes": ("#efa093", "#000000"),  # used once, then noise: paler red
    "Business": ("#ffad47", "#000000"),
    "Government": ("#434343", "#ffffff"),
    "Education": ("#1c4587", "#ffffff"),
    "Personal": ("#4a86e8", "#ffffff"),
    "Appointments": ("#a479e2", "#ffffff"),
    "Health": ("#f691b2", "#000000"),
    "Orders": ("#a4c2f4", "#000000"),
    "Travel": ("#2da2bb", "#ffffff"),
    "Notifications": ("#cccccc", "#000000"),
    "Promos": ("#cccccc", "#000000"),
}


def color_body(name: str) -> dict:
    bg, fg = LABEL_COLORS[name]
    return {"color": {"backgroundColor": bg, "textColor": fg}}


def ensure_labels(svc, create: bool = True) -> dict[str, str]:
    """Map label name ("Jobs › Skip") -> Gmail label id, creating missing ones.

    Names are flat (no "/", so Gmail never nests them). An existing label with
    the same name is reused as is, e.g. the owner's own "Personal"; only a
    newly created label gets its color. With create=False (dry run) missing
    labels map to a placeholder id.
    """
    existing = {l["name"]: l["id"] for l in svc.users().labels().list(userId="me").execute(num_retries=RETRIES).get("labels", [])}
    for name in ALL_LABELS:
        if name in existing:
            continue
        if not create:
            log.info("dry-run: would create label %s", name)
            existing[name] = f"DRYRUN:{name}"
            continue
        body = {"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show", **color_body(name)}
        existing[name] = svc.users().labels().create(userId="me", body=body).execute(num_retries=RETRIES)["id"]
        log.info("created label %s", name)
    return {name: existing[name] for name in ALL_LABELS}


def watch(svc, topic_path: str) -> dict:
    body = {"topicName": topic_path, "labelIds": ["INBOX"], "labelFilterBehavior": "include"}
    resp = svc.users().watch(userId="me", body=body).execute(num_retries=RETRIES)
    log.info("watch renewed: historyId=%s expires=%s", resp.get("historyId"),
             time.strftime("%Y-%m-%d %H:%M", time.localtime(int(resp["expiration"]) / 1000)))
    return resp


def current_history_id(svc) -> str:
    return str(svc.users().getProfile(userId="me").execute(num_retries=RETRIES)["historyId"])


class HistoryTooOld(Exception):
    pass


def new_inbox_ids_since(svc, start_history_id: str) -> tuple[list[str], str]:
    """Message ids added to INBOX since start_history_id, plus the newest historyId.

    Raises HistoryTooOld when Gmail answers 404 (history no longer retained).
    """
    ids: list[str] = []
    seen: set[str] = set()
    latest = start_history_id
    page = None
    while True:
        try:
            resp = svc.users().history().list(
                userId="me", startHistoryId=start_history_id, historyTypes=["messageAdded"],
                labelId="INBOX", pageToken=page, maxResults=500,
            ).execute(num_retries=RETRIES)
        except HttpError as e:
            if e.resp.status == 404:
                raise HistoryTooOld(start_history_id) from e
            raise
        latest = str(resp.get("historyId", latest))
        for h in resp.get("history", []):
            for added in h.get("messagesAdded", []):
                m = added["message"]
                if "INBOX" in m.get("labelIds", []) and m["id"] not in seen:
                    seen.add(m["id"])
                    ids.append(m["id"])
        page = resp.get("nextPageToken")
        if not page:
            return ids, latest


def list_ids(svc, query: str, limit: int | None = None) -> list[str]:
    ids: list[str] = []
    page = None
    while True:
        resp = svc.users().messages().list(userId="me", q=query, pageToken=page, maxResults=500).execute(num_retries=RETRIES)
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page or (limit and len(ids) >= limit):
            return ids[:limit] if limit else ids


def gap_days(last_success_epoch: float | None, now: float | None = None) -> int:
    """Days to look back after a history 404; +1 so the boundary day is covered."""
    now = time.time() if now is None else now
    if not last_success_epoch:
        return 7
    return max(1, math.ceil((now - last_success_epoch) / 86400)) + 1


def fetch_metadata(svc, ids: list[str], ping: Callable[[], None] = lambda: None) -> list[dict]:
    """messages.get format=metadata for each id. Deleted-in-between ids are dropped."""
    out: list[dict] = []
    for mid in ids:
        try:
            m = svc.users().messages().get(
                userId="me", id=mid, format="metadata", metadataHeaders=META_HEADERS,
            ).execute(num_retries=RETRIES)
        except HttpError as e:
            if e.resp.status == 404:
                continue
            raise
        out.append(m)
        ping()
    return out


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def to_item(msg: dict) -> dict:
    return {
        "id": msg["id"],
        "from": header(msg, "From"),
        "subject": header(msg, "Subject"),
        "snippet": msg.get("snippet", ""),
        "list_unsubscribe": header(msg, "List-Unsubscribe"),
    }


def needs_triage(msg: dict, triage_label_ids: set[str]) -> bool:
    labels = set(msg.get("labelIds", []))
    return "INBOX" in labels and not (labels & UNTOUCHABLE) and not (labels & triage_label_ids)


def modify_body(decision: dict, label_ids: dict[str, str]) -> dict:
    """The ONLY shape of change we ever make: add triage labels (+ STARRED),
    set IMPORTANT one way or the other, and, when the decision says `archive`
    (the owner opted that label in via config), take the message out of INBOX.

    IMPORTANT is the one label we remove by default (Gmail's own guess is
    noise): never mark read, unstar, delete or touch spam.
    """
    add = [label_ids[l] for l in decision["labels"]]
    if decision["star"]:
        add.append(STARRED)
    remove = [INBOX] if decision.get("archive") else []
    if decision["important"]:
        add.append(IMPORTANT)
    else:
        remove.insert(0, IMPORTANT)
    return {"addLabelIds": add, "removeLabelIds": remove} if remove else {"addLabelIds": add}


def apply(svc, msg_id: str, body: dict, archive_ok: bool = False) -> None:
    """Send one change, refusing any shape other than modify_body's.

    Removing INBOX is refused unless the caller says the owner configured
    archive_labels, and never for a message being starred or marked important.
    """
    assert set(body) in ({"addLabelIds"}, {"addLabelIds", "removeLabelIds"}), body
    add, remove = body["addLabelIds"], body.get("removeLabelIds")
    assert remove is None or remove in ([IMPORTANT], [INBOX], [IMPORTANT, INBOX]), body
    assert (IMPORTANT in add) != (IMPORTANT in (remove or [])), body
    assert not set(add) & {"TRASH", "SPAM", "UNREAD", "INBOX"}, body
    if INBOX in (remove or []):
        assert archive_ok and not set(add) & {STARRED, IMPORTANT}, body
    svc.users().messages().modify(userId="me", id=msg_id, body=body).execute(num_retries=RETRIES)
