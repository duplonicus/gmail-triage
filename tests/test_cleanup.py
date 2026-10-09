"""Cleanup: what gets archived or trashed, and that restore puts it all back."""
from __future__ import annotations

import copy
import json

import pytest

from gmail_triage import census as C
from gmail_triage import cleanup as K
from test_backup import NAMES, row


class Box:
    """A mailbox as {id: set of labels}, with just the calls cleanup makes."""

    def __init__(self, labels: dict[str, set[str]], queries: dict[str, list[str]] | None = None):
        self.labels = {k: set(v) for k, v in labels.items()}
        self.queries = queries or {}
        self.bodies: list[dict] = []

    def users(self): return self
    def messages(self): return self

    def batchModify(self, userId, body):
        return _Exec(lambda: self._modify(body))

    def _modify(self, body):
        self.bodies.append(body)
        assert len(body["ids"]) <= 1000
        for mid in body["ids"]:
            self.labels[mid] |= set(body.get("addLabelIds", []))
            self.labels[mid] -= set(body.get("removeLabelIds", []))
        return {}

    def list(self, userId, q, pageToken=None, maxResults=None):
        return _Exec(lambda: {"messages": [{"id": i} for i in self.queries[q]]})


class _Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self, num_retries=0): return self.fn()


# the guard -------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"addLabelIds": ["SPAM"]},
    {"removeLabelIds": ["STARRED"]},
    {"addLabelIds": ["TRASH"]},                                 # trash must also leave the inbox
    {"removeLabelIds": ["INBOX", "UNREAD", "STARRED"]},
    {"addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX", "STARRED"]},
    {},
])
def test_batch_modify_refuses_any_other_change(body):
    box = Box({"a": {"INBOX"}})
    with pytest.raises(AssertionError):
        K.batch_modify(box, ["a"], body)
    assert box.bodies == [] and box.labels == {"a": {"INBOX"}}


def test_allowed_changes_never_touch_stars_spam_or_user_labels():
    touched = {l for body in K.ALLOWED for key in ("addLabelIds", "removeLabelIds") for l in body.get(key, [])}
    assert touched == {"INBOX", "UNREAD", "TRASH"}


def test_batch_modify_chunks_at_the_api_limit_and_covers_every_id():
    ids = [f"m{i}" for i in range(2501)]
    box = Box({i: {"INBOX"} for i in ids})
    K.batch_modify(box, ids, K.ARCHIVE)
    assert [len(b["ids"]) for b in box.bodies] == [1000, 1000, 501]
    assert [i for b in box.bodies for i in b["ids"]] == ids
    assert all(labels == set() for labels in box.labels.values())


# archive ---------------------------------------------------------------------

def test_plan_archive_asks_gmail_for_old_unstarred_inbox_mail():
    q = "in:inbox older_than:30d -is:starred"
    box = Box({}, {q: ["a", "b", "c"], q + " is:unread": ["b", "c"]})
    assert K.plan_archive(box, 30, mark_read=True) == {
        "action": "archive", "older_than_days": 30, "mark_read": True, "ids": ["a", "b", "c"], "unread": ["b", "c"]}
    assert K.plan_archive(box, 30, mark_read=False)["unread"] == []


@pytest.mark.parametrize("mark_read", [True, False])
def test_archive_then_restore_leaves_every_label_as_it_was(mark_read):
    before = {
        "old_unread": {"INBOX", "UNREAD", "Label_2", "CATEGORY_UPDATES"},
        "old_read": {"INBOX", "IMPORTANT"},
        "starred": {"INBOX", "UNREAD", "STARRED"},   # not in the plan: the query excludes it
        "recent": {"INBOX", "UNREAD"},               # not in the plan: too new
    }
    box = Box(before)
    plan = {"action": "archive", "mark_read": mark_read, "ids": ["old_unread", "old_read"],
            "unread": ["old_unread"] if mark_read else []}
    K.apply(box, plan)
    assert box.labels["old_unread"] == {"Label_2", "CATEGORY_UPDATES"} | (set() if mark_read else {"UNREAD"})
    assert box.labels["old_read"] == {"IMPORTANT"}
    assert box.labels["starred"] == before["starred"] and box.labels["recent"] == before["recent"]
    K.restore(box, plan)
    assert box.labels == before


# trash -----------------------------------------------------------------------

def test_plan_trash_is_exactly_the_census_candidates_of_approved_senders():
    rows = [
        row("junk1"), row("junk2"),
        row("other", sender="News <n@other.example>"),                      # candidate, sender not approved
        row("doc", attachments=["invoice.pdf"]),                            # approved sender, has a document
        row("star_then", labels=("CATEGORY_PROMOTIONS", "STARRED")),        # starred at backup time
        row("star_now"),                                                    # starred since the backup
        row("gone", labels=("CATEGORY_PROMOTIONS", "TRASH")),               # already in Trash
        row("person", sender="Ann <ann@example.com>", unsubscribe=False, labels=("CATEGORY_PERSONAL", "INBOX")),
    ]
    approved = {"deals@shop.example", "ann@example.com"}
    plan = K.plan_trash(rows, NAMES, approved, starred_now={"star_now"}, inbox_now={"junk1", "doc", "person"})
    assert plan == {"action": "trash", "ids": ["junk1", "junk2"], "inbox": ["junk1"]}
    # and every id is a census candidate: nothing gets here by another road
    replied = C.replied_threads(rows)
    by_id = {r["id"]: r for r in rows}
    assert all(C.keep_reasons(by_id[i], NAMES, replied) == [] for i in plan["ids"])


def test_plan_trash_with_no_approved_senders_is_empty():
    assert K.plan_trash([row("junk1")], NAMES, set(), set(), set())["ids"] == []


def test_trash_then_restore_leaves_every_label_as_it_was():
    before = {"in_inbox": {"INBOX", "UNREAD", "CATEGORY_PROMOTIONS", "Label_1"},
              "archived": {"CATEGORY_PROMOTIONS"}, "bystander": {"INBOX"}}
    box = Box(before)
    plan = {"action": "trash", "ids": ["in_inbox", "archived"], "inbox": ["in_inbox"]}
    K.apply(box, plan)
    assert box.labels["in_inbox"] == {"TRASH", "UNREAD", "CATEGORY_PROMOTIONS", "Label_1"}
    assert box.labels["archived"] == {"TRASH", "CATEGORY_PROMOTIONS"}
    K.restore(box, plan)
    assert box.labels == before


def test_apply_twice_is_the_same_as_once():
    box = Box({"a": {"INBOX", "UNREAD"}, "b": {"INBOX"}})
    plan = {"action": "archive", "mark_read": True, "ids": ["a", "b"], "unread": ["a"]}
    K.apply(box, plan)
    once = copy.deepcopy(box.labels)
    K.apply(box, plan)
    assert box.labels == once


def test_unknown_action_is_refused():
    with pytest.raises(ValueError):
        K.apply(Box({}), {"action": "delete", "ids": []})
    with pytest.raises(ValueError):
        K.restore(Box({}), {"action": "delete", "ids": []})


# files -----------------------------------------------------------------------

def test_read_senders_ignores_comments_blanks_and_case(tmp_path):
    f = tmp_path / "approved.txt"
    f.write_text("# marketing\nDeals@Shop.example\n\n  n@other.example  # newsletter\n")
    assert K.read_senders(f) == {"deals@shop.example", "n@other.example"}


def test_restore_file_round_trips_the_plan(tmp_path):
    plan = {"action": "trash", "ids": ["a", "b"], "inbox": ["a"]}
    path = K.save_restore(plan, tmp_path)
    saved = json.loads(path.read_text())
    assert path.name.startswith("trash-") and {k: saved[k] for k in plan} == plan
