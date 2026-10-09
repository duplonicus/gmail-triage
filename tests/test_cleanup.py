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


WIDE_NAMES = {**NAMES, "Label_4": "Jobs › Alerts", "Label_5": "Jobs › Interview", "Label_6": "Notifications"}
JOBS = "Jobs <alerts@jobs.example>"


def wide_row(mid, labels=("CATEGORY_UPDATES", "INBOX"), **kw):
    return row(mid, labels=labels, sender=JOBS, **kw)


def test_wide_sender_loses_only_the_tab_and_alert_label_protection():
    rows = [
        wide_row("updates"),                                             # Updates tab: goes
        wide_row("no_tab", labels=("INBOX",)),                           # no category at all: goes
        wide_row("alert", labels=("CATEGORY_UPDATES", "Label_4")),       # labelled Jobs › Alerts: goes
        wide_row("notif", labels=("CATEGORY_UPDATES", "Label_6")),       # labelled Notifications: goes
        wide_row("interview", labels=("CATEGORY_UPDATES", "Label_5")),   # a real label still keeps
        wide_row("receipt", labels=("CATEGORY_UPDATES", "Label_2")),
        wide_row("not_bulk", unsubscribe=False),
        wide_row("doc", attachments=["offer.pdf"]),
        wide_row("starred", labels=("CATEGORY_UPDATES", "STARRED")),
        wide_row("thread", thread="t9"),
        wide_row("mine", labels=("SENT",), thread="t9", unsubscribe=False),
    ]
    wide = {"alerts@jobs.example"}
    plan = K.plan_trash(rows, WIDE_NAMES, set(), set(), set(), wide=wide)
    assert plan["ids"] == ["updates", "no_tab", "alert", "notif"]
    # the same sender merely approved, not wide, loses nothing outside Promotions/Social
    assert K.plan_trash(rows, WIDE_NAMES, wide, set(), set())["ids"] == []


def test_wide_does_not_leak_to_other_senders():
    rows = [wide_row("w"), row("other", labels=("CATEGORY_UPDATES",))]
    plan = K.plan_trash(rows, WIDE_NAMES, {"deals@shop.example"}, set(), set(), wide={"alerts@jobs.example"})
    assert plan["ids"] == ["w"]


@pytest.mark.parametrize("who, kept", [
    ("id@proxy.example", True),            # exact address
    ("a.b@mail.registrar.example", True),  # subdomain of a kept domain
    ("x@registrar.example", True),
    ("x@notregistrar.example", False),     # a suffix that is not a subdomain
    ("other@proxy.example", False),        # address entries do not cover the domain
])
def test_keep_list_matches_addresses_and_domains(who, kept):
    assert K.is_kept_sender(who, {"id@proxy.example", "@registrar.example"}) is kept


def test_keep_list_beats_approved_and_wide():
    rows = [row("a"), wide_row("b"), row("c", sender="Reg <x@mail.registrar.example>")]
    approved, wide = {"deals@shop.example", "x@mail.registrar.example"}, {"alerts@jobs.example"}
    assert K.plan_trash(rows, WIDE_NAMES, approved, set(), set(), wide=wide)["ids"] == ["a", "b", "c"]
    keep = {"deals@shop.example", "@jobs.example", "@registrar.example"}
    assert K.plan_trash(rows, WIDE_NAMES, approved, set(), set(), wide=wide, keep=keep)["ids"] == []


# people only -------------------------------------------------------------------

def auto(mid, sender="Shop <noreply@shop.example>", labels=("CATEGORY_UPDATES", "INBOX"), unsubscribe=False, **kw):
    return row(mid, sender=sender, labels=labels, unsubscribe=unsubscribe, **kw)


@pytest.mark.parametrize("r, person", [
    (auto("a", sender="Ann <ann@gmail.com>"), True),                                    # personal provider, any tab
    (auto("a", sender="Ann <ann@gmail.com>", unsubscribe=True), False),                 # ...but not a mailing
    (auto("a", sender="Bo <bo.lee@firm.example>", labels=("CATEGORY_PERSONAL",)), True),
    (auto("a", sender="Bo <bo.lee@firm.example>", labels=("INBOX",)), True),            # no tab at all
    (auto("a", sender="Bo <bo.lee@firm.example>"), False),                              # Gmail filed it under Updates
    (auto("a", sender="Firm <noreply@firm.example>", labels=("CATEGORY_PERSONAL",)), False),
    (auto("a", sender="Firm <no-reply@firm.example>", labels=("CATEGORY_PERSONAL",)), False),
    (auto("a", sender="Firm <billing.team@firm.example>", labels=("CATEGORY_PERSONAL",)), False),
    (auto("a", sender="Firm <alerts+x1@firm.example>", labels=("CATEGORY_PERSONAL",)), False),
    (auto("a", sender="Sid <sidney@firm.example>", labels=("CATEGORY_PERSONAL",)), True),   # "id" only as a whole word
    (auto("a", sender="Al <alhelper@firm.example>", labels=("CATEGORY_PERSONAL",)), True),  # "help" only as a whole word
])
def test_is_person(r, person):
    assert C.is_person(r) is person


def test_people_only_takes_every_automated_message_and_keeps_the_rest():
    rows = [
        auto("notice"),                                                       # no unsubscribe, Updates tab: goes
        auto("promo", labels=("CATEGORY_PROMOTIONS",), unsubscribe=True),     # goes
        auto("alert", labels=("CATEGORY_UPDATES", "Label_4")),                # junk label: goes
        auto("friend", sender="Ann <ann@gmail.com>"),
        auto("colleague", sender="Bo <bo.lee@firm.example>", labels=("CATEGORY_PERSONAL",)),
        auto("contract", attachments=["contract.pdf"]),
        auto("starred", labels=("CATEGORY_UPDATES", "STARRED")),
        auto("receipt", labels=("CATEGORY_UPDATES", "Label_2")),
        auto("thread", thread="t1"),
        auto("mine", labels=("SENT",), thread="t1"),
    ]
    plan = K.plan_trash(rows, WIDE_NAMES, set(), set(), set(), people_only=True)
    assert plan["ids"] == ["notice", "promo", "alert"]
    replied = C.replied_threads(rows)
    kept = {r["id"]: C.keep_reasons_people(r, WIDE_NAMES, replied) for r in rows if r["id"] not in plan["ids"]}
    assert kept == {"friend": ["person"], "colleague": ["person"], "contract": ["document"], "starred": ["starred"],
                    "receipt": ["labelled"], "thread": ["replied"], "mine": ["yours", "replied"]}
    # without the switch, no sender is approved, so nothing goes
    assert K.plan_trash(rows, WIDE_NAMES, set(), set(), set())["ids"] == []


def test_drop_and_chats_remove_only_the_person_reason():
    ann = "Ann <ann@gmail.com>"
    rows = [
        auto("mail", sender=ann),
        auto("chat", sender=ann, labels=("CHAT",)),
        auto("photo_doc", sender=ann, attachments=["scan.pdf"]),      # dropped sender, but a document
        auto("starred", sender=ann, labels=("CHAT", "STARRED")),
        auto("bo", sender="Bo <bo@gmail.com>"),
        auto("bo_chat", sender="Bo <bo@gmail.com>", labels=("CHAT",)),
    ]
    plan = lambda **kw: K.plan_trash(rows, WIDE_NAMES, set(), set(), set(), people_only=True, **kw)["ids"]
    assert plan() == []
    assert plan(chats=True) == ["chat", "bo_chat"]
    assert plan(drop={"ann@gmail.com"}) == ["mail", "chat"]
    assert plan(drop={"ann@gmail.com"}, chats=True) == ["mail", "chat", "bo_chat"]
    # outside people-only mode they do nothing
    assert K.plan_trash(rows, WIDE_NAMES, set(), set(), set(), drop={"ann@gmail.com"}, chats=True)["ids"] == []


def test_people_only_still_obeys_keep_list_cutoff_and_live_stars():
    rows = [auto("a"), auto("b", sender="Reg <noreply@registrar.example>"), auto("c"), auto("d")]
    rows[2]["internal_date"] = 5000
    plan = K.plan_trash(rows, WIDE_NAMES, set(), {"d"}, set(), people_only=True,
                        keep={"@registrar.example"}, before_ms=row()["internal_date"] + 1)
    assert plan["ids"] == ["a", "c"]
    assert K.plan_trash(rows, WIDE_NAMES, set(), {"d"}, set(), people_only=True, before_ms=5001)["ids"] == ["c"]


def test_age_cutoff_keeps_mail_at_or_after_it():
    rows = [row("old"), row("edge"), row("new")]
    rows[0]["internal_date"], rows[1]["internal_date"], rows[2]["internal_date"] = 999, 1000, 1001
    plan = K.plan_trash(rows, NAMES, {"deals@shop.example"}, set(), set(), before_ms=1000)
    assert plan["ids"] == ["old"]


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
