"""Backup (resume, atomicity, index rows) and census (the keep rules)."""
from __future__ import annotations

import json
from email.message import EmailMessage

import pytest

from gmail_triage import backup as B
from gmail_triage import census as C


def raw_message(subject="hi", sender="Ann <ann@example.com>", unsubscribe=False, attachment=None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "me@example.com", subject
    if unsubscribe:
        m["List-Unsubscribe"] = "<https://example.com/u>"
    m.set_content("body")
    if attachment:
        m.add_attachment(b"data", maintype="application", subtype="octet-stream", filename=attachment)
    return m.as_bytes()


def resource(mid, labels=("INBOX",), thread=None):
    return {"id": mid, "threadId": thread or mid, "labelIds": list(labels), "internalDate": "1700000000000", "sizeEstimate": 1234}


class Mailbox:
    def __init__(self, ids, gone=()):
        self.ids, self.gone, self.fetched = list(ids), set(gone), []

    def fetch(self, mid):
        self.fetched.append(mid)
        if mid in self.gone:
            return None
        return resource(mid), raw_message(subject=f"msg {mid}")


# backup ---------------------------------------------------------------------

def test_backup_writes_every_message_and_marks_complete(tmp_path):
    box = Mailbox(["a1", "b2", "c3"])
    summary = B.run(tmp_path, box.ids, box.fetch, workers=2)
    assert summary["listed"] == summary["in_index"] == summary["fetched_this_run"] == 3
    assert summary["missing"] == 0
    assert {r["id"] for r in B.read_index(tmp_path)} == {"a1", "b2", "c3"}
    for mid in box.ids:
        assert B.eml_path(tmp_path, mid).read_bytes() == raw_message(subject=f"msg {mid}")
    assert json.loads((tmp_path / B.COMPLETE).read_text())["in_index"] == 3
    assert not list(tmp_path.rglob("*.tmp"))


def test_second_run_fetches_only_what_is_missing(tmp_path):
    B.run(tmp_path, ["a1", "b2"], Mailbox(["a1", "b2"]).fetch)
    box = Mailbox(["a1", "b2", "c3"])
    summary = B.run(tmp_path, box.ids, box.fetch)
    assert box.fetched == ["c3"]
    assert summary["fetched_this_run"] == 1 and summary["in_index"] == 3
    assert len(B.read_index(tmp_path)) == 3  # no duplicate rows


def test_force_refetches_everything(tmp_path):
    B.run(tmp_path, ["a1", "b2"], Mailbox(["a1", "b2"]).fetch)
    box = Mailbox(["a1", "b2"])
    B.run(tmp_path, box.ids, box.fetch, force=True)
    assert sorted(box.fetched) == ["a1", "b2"]
    assert len(B.read_index(tmp_path)) == 2


def test_a_run_that_dies_leaves_no_completion_marker_and_resumes(tmp_path):
    B.run(tmp_path, ["a1"], Mailbox(["a1"]).fetch)
    assert (tmp_path / B.COMPLETE).exists()

    def dying(mid):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        B.run(tmp_path, ["a1", "b2"], dying, workers=1)
    assert not (tmp_path / B.COMPLETE).exists()
    box = Mailbox(["a1", "b2"])
    assert B.run(tmp_path, box.ids, box.fetch)["missing"] == 0
    assert box.fetched == ["b2"]


def test_torn_last_index_line_is_refetched(tmp_path):
    B.run(tmp_path, ["a1"], Mailbox(["a1"]).fetch)
    with open(tmp_path / B.INDEX, "a") as f:
        f.write('{"id": "b2", "thr')  # killed mid-write
    box = Mailbox(["a1", "b2"])
    B.run(tmp_path, box.ids, box.fetch)
    assert box.fetched == ["b2"]
    assert {r["id"] for r in B.read_index(tmp_path)} == {"a1", "b2"}


def test_message_deleted_in_between_is_counted_not_fatal(tmp_path):
    box = Mailbox(["a1", "b2"], gone={"b2"})
    summary = B.run(tmp_path, box.ids, box.fetch)
    assert summary["gone_this_run"] == 1 and summary["missing"] == 1 and summary["in_index"] == 1


def test_pacer_spaces_calls_and_never_bursts_after_a_pause():
    now, slept = [0.0], []

    def sleep(s):
        slept.append(round(s, 3))
        now[0] += s

    p = B.Pacer(60, clock=lambda: now[0], sleep=sleep)  # one a second
    p.wait(); p.wait(); p.wait()
    assert slept == [1.0, 1.0]
    now[0] += 10  # idle: the unused allowance is not saved up
    p.wait(); p.wait()
    assert slept == [1.0, 1.0, 1.0]


class _Get:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def users(self): return self
    def messages(self): return self
    def get(self, **kw): return self

    def execute(self, num_retries=0):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return dict(out)


def test_fetch_raw_waits_out_a_rate_limit_and_decodes():
    import base64
    from fakes import http_error
    ok = {**resource("a1"), "raw": base64.urlsafe_b64encode(b"hello").decode()}
    svc, naps = _Get([http_error(403), http_error(429), ok]), []
    res, raw = B.fetch_raw(svc, "a1", sleep=naps.append)
    assert raw == b"hello" and "raw" not in res and naps == [30, 30]


def test_fetch_raw_404_is_none_and_other_errors_raise():
    from fakes import http_error
    from googleapiclient.errors import HttpError
    assert B.fetch_raw(_Get([http_error(404)]), "a1") is None
    with pytest.raises(HttpError):
        B.fetch_raw(_Get([http_error(500)]), "a1", sleep=lambda s: None)
    svc = _Get([http_error(403)] * (B.RATE_LIMIT_WAITS + 1))
    with pytest.raises(HttpError):
        B.fetch_raw(svc, "a1", sleep=lambda s: None)
    assert svc.calls == B.RATE_LIMIT_WAITS + 1


def test_index_row_reads_headers_and_attachments():
    raw = raw_message(subject="Your contract", unsubscribe=True, attachment="contract.pdf")
    row = B.index_row(resource("a1", labels=("STARRED", "INBOX"), thread="t1"), raw)
    assert row == {
        "id": "a1", "thread": "t1", "labels": ["INBOX", "STARRED"], "internal_date": 1700000000000,
        "size": 1234, "from": "Ann <ann@example.com>", "to": "me@example.com",
        "subject": "Your contract", "list_unsubscribe": True, "attachments": ["contract.pdf"],
    }


def test_index_row_survives_garbage():
    row = B.index_row(resource("a1"), b"\xff\xfe not an email \x00\x01")
    assert row["id"] == "a1" and row["attachments"] == [] and row["list_unsubscribe"] is False


# census ---------------------------------------------------------------------

NAMES = {"Label_1": "Promos", "Label_2": "Money › Receipts", "Label_3": "Newsletters › AI"}


def row(mid="m1", labels=("CATEGORY_PROMOTIONS", "INBOX"), unsubscribe=True, attachments=(), thread=None,
        sender="Shop <deals@shop.example>", size=1000):
    return {"id": mid, "thread": thread or mid, "labels": list(labels), "internal_date": 1700000000000, "size": size,
            "from": sender, "to": "me@example.com", "subject": "s", "list_unsubscribe": unsubscribe,
            "attachments": list(attachments)}


def reasons(r, replied=frozenset(), **kw):
    return C.keep_reasons(r, NAMES, set(replied), **kw)


def test_bulk_promo_with_nothing_else_is_a_candidate():
    assert reasons(row()) == []
    assert reasons(row(labels=("CATEGORY_SOCIAL",))) == []
    assert reasons(row(labels=("CATEGORY_PROMOTIONS", "Label_1", "Label_3"))) == []
    assert reasons(row(attachments=["logo.png", "banner.JPG"])) == []


@pytest.mark.parametrize("r, why", [
    (row(unsubscribe=False), ["not bulk"]),
    (row(labels=("CATEGORY_UPDATES",)), ["category"]),
    (row(labels=("CATEGORY_PERSONAL",)), ["category"]),
    (row(labels=("INBOX",)), ["category"]),
    (row(attachments=["invoice.pdf"]), ["document"]),
    (row(attachments=["logo.png", "terms.docx"]), ["document"]),
    (row(attachments=["noextension"]), ["document"]),
    (row(labels=("CATEGORY_PROMOTIONS", "STARRED")), ["starred"]),
    (row(labels=("CATEGORY_PROMOTIONS", "Label_2")), ["labelled"]),
    (row(labels=("CATEGORY_PROMOTIONS", "Label_99")), ["labelled"]),  # a label we cannot name is kept
    (row(labels=("CATEGORY_PROMOTIONS", "SENT")), ["yours"]),
    (row(labels=("CATEGORY_PROMOTIONS", "DRAFT")), ["yours"]),
])
def test_each_rule_keeps_on_its_own(r, why):
    assert reasons(r) == why


def test_a_thread_the_owner_wrote_in_is_kept():
    promo = row("m1", thread="t1")
    reply = row("m2", thread="t1", labels=("SENT",), unsubscribe=False)
    replied = C.replied_threads([promo, reply, row("m3")])
    assert replied == {"t1"}
    assert reasons(promo, replied) == ["replied"]
    assert reasons(row("m3"), replied) == []


def test_star_check_lists_only_starred_mail_the_rules_would_delete():
    junk_looking = row("m1", labels=("CATEGORY_PROMOTIONS", "STARRED"))
    personal = row("m2", labels=("CATEGORY_PERSONAL", "STARRED"), unsubscribe=False)
    unstarred = row("m3")
    assert [r["id"] for r in C.star_check([junk_looking, personal, unstarred], NAMES)] == ["m1"]
    assert reasons(junk_looking) == ["starred"]  # and the star still protects it


def test_by_sender_totals_add_up_and_a_star_does_not_shield_the_sender():
    rows = [
        row("m1", size=3_000_000),
        row("m2", size=1_000_000, labels=("CATEGORY_PROMOTIONS", "STARRED")),
        row("m3", sender="Ann <ann@example.com>", unsubscribe=False, labels=("CATEGORY_PERSONAL",), size=500_000),
    ]
    senders = C.by_sender(rows, NAMES)
    assert [s["sender"] for s in senders] == ["deals@shop.example", "ann@example.com"]
    shop, ann = senders
    assert (shop["messages"], shop["candidates"], shop["kept"], shop["starred"]) == (2, 1, 1, 1)
    assert (shop["mb"], shop["candidate_mb"]) == (4.0, 3.0)
    assert (ann["candidates"], ann["kept"]) == (0, 1)
    assert sum(s["messages"] for s in senders) == len(rows)
    assert sum(s["candidates"] + s["kept"] for s in senders) == len(rows)


def test_census_main_writes_report_and_csv(tmp_path):
    (tmp_path / B.INDEX).write_text("\n".join(json.dumps(r) for r in [row("m1"), row("m2", unsubscribe=False)]) + "\n")
    (tmp_path / B.LABELS).write_text(json.dumps(NAMES))
    assert C.main([str(tmp_path)]) == 0
    assert "Delete candidates: 1 messages" in (tmp_path / "census.md").read_text()
    assert (tmp_path / "senders.csv").read_text().splitlines()[1].startswith("deals@shop.example,Shop,2,")


def test_census_main_without_a_backup_fails(tmp_path):
    assert C.main([str(tmp_path)]) == 1
