import dataclasses
import json
import logging

import pytest

from gmail_triage import classifier as K
from gmail_triage import gmail as G
from gmail_triage import config as C
from gmail_triage import profile as P
from gmail_triage.daemon import Triage, parse_days
from gmail_triage.state import State

from fakes import FakeGmail, msg

CFG = C.load(C.EXAMPLE_CONFIG_FILE)


def dec(i, labels=("Promos",), star=False, reason="r"):
    """A decision as the model writes it."""
    return {"id": i, "labels": list(labels), "star": star, "reason": reason}


def decided(i, labels=("Promos",), star=False, important=False):
    """A decision as parse_output hands it on."""
    return {**dec(i, labels, star), "important": important}


# --------------------------------------------------------------------------- validation

@pytest.mark.parametrize("labels", [["Promos"], ["Security › Suspicious"], ["Security › Suspicious", "Money"], ["Jobs › Skip"],
                                    ["Jobs › Interview"], ["Personal"]])
def test_valid_label_sets(labels):
    assert K.validate_decision(dec("1", labels), {"1"}) is None


@pytest.mark.parametrize("d,why", [
    (dec("1", ["Promos", "Money"]), "more than one primary"),
    (dec("1", ["Security › Suspicious", "Money", "Promos"]), "more than one primary"),
    (dec("1", ["Jobs/Applications"]), "unknown labels"),  # pre-2026-09-30 name
    (dec("1", []), "non-empty"),
    (dec("1", ["Spam"]), "unknown labels"),
    (dec("1", ["Promos", "Promos"]), "duplicate"),
    (dec("2"), "unknown id"),
    ({**dec("1"), "star": "yes"}, "star not a bool"),
    ({**dec("1"), "extra": 1}, "keys"),
    ("nope", "not an object"),
])
def test_invalid_decisions(d, why):
    assert why in K.validate_decision(d, {"1"})


def test_parse_output_partitions_missing_duplicate_and_bad():
    out = json.dumps([dec("1"), dec("2", ["Nope"]), dec("3"), dec("3"), dec("99")])
    good, bad = K.parse_output(out, {"1", "2", "3", "4"})
    assert set(good) == {"1"}
    assert set(bad) == {"2", "3", "4"}
    assert bad["3"] == "duplicate id in output"
    assert bad["4"] == "missing from output"


def test_parse_output_tolerates_code_fence_but_not_prose():
    good, bad = K.parse_output("```json\n" + json.dumps([dec("1")]) + "\n```", {"1"})
    assert set(good) == {"1"} and not bad
    good, bad = K.parse_output("Here you go: " + json.dumps([dec("1")]), {"1"})
    assert not good and set(bad) == {"1"}


def test_retry_once_then_skip_never_guess():
    calls = []

    def runner(prompt):
        calls.append(prompt)
        return json.dumps([dec("1")])  # never answers for "2"

    c = K.Classifier(CFG, runner=runner)
    good, bad = c.classify([{"id": "1"}, {"id": "2"}])
    assert len(calls) == 2  # original + exactly one retry
    assert '"id": "2"' in calls[1] and '"id": "1"' not in calls[1]  # retry only the bad ones
    assert set(good) == {"1"} and set(bad) == {"2"}


def test_retry_recovers():
    answers = iter(["garbage", json.dumps([dec("1")])])
    good, bad = K.Classifier(CFG, runner=lambda p: next(answers)).classify([{"id": "1"}])
    assert set(good) == {"1"} and not bad


def test_cli_env_strips_api_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "y")
    env = K._cli_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["MAX_THINKING_TOKENS"] == "0"


def test_cli_command_disables_tools_mcp_settings():
    cmd = K.cli_command("haiku")
    assert cmd[:2] == ["claude", "-p"]
    for flag in ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]:
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--bare" not in cmd  # --bare refuses OAuth -> would need an API key


# --------------------------------------------------------------------------- gmail side

EXPECTED_LABELS = [
    "Jobs › Applied", "Jobs › Reply", "Jobs › Interview", "Jobs › Rejected", "Jobs › Alerts", "Jobs › Skip",
    "Money", "Money › Invoices", "Money › Receipts", "Money › Statements", "Money › Trading",
    "Money › Transfers", "Money › Taxes", "Money › Crypto",
    "Business", "Government", "Education", "Security", "Security › Codes", "Personal", "Appointments", "Health",
    "Orders", "Travel", "Notifications", "Newsletters", "Newsletters › Crypto", "Newsletters › AI", "Promos",
    "Security › Suspicious",
]


def test_label_names_are_flat_and_untriaged():
    assert K.ALL_LABELS == EXPECTED_LABELS
    for name in K.ALL_LABELS:
        assert "/" not in name and not name.startswith("Triage"), name
        assert f"\n- {name}: " in K.RULES, name  # the prompt defines every label exactly


def test_every_label_has_a_color():
    assert set(G.LABEL_COLORS) == set(K.ALL_LABELS)


def test_ensure_labels_creates_flat_colored_labels_and_is_idempotent():
    g = FakeGmail({})
    ids = G.ensure_labels(g)
    created = {l["name"]: l for l in g._labels[1:]}
    assert list(created) == EXPECTED_LABELS  # no parents, nothing else
    assert created["Jobs › Interview"]["color"] == {"backgroundColor": "#16a766", "textColor": "#ffffff"}
    assert created["Promos"]["color"] == {"backgroundColor": "#cccccc", "textColor": "#000000"}
    assert set(ids) == set(K.ALL_LABELS)
    n = len(g._labels)
    assert G.ensure_labels(g) == ids and len(g._labels) == n


def test_ensure_labels_reuses_owners_existing_label():
    g = FakeGmail({})
    g._labels.append({"id": "Label_4", "name": "Personal"})
    assert G.ensure_labels(g)["Personal"] == "Label_4"
    assert [l["name"] for l in g._labels].count("Personal") == 1


def test_modify_body_adds_labels_and_sets_important_both_ways():
    ids = {l: f"L_{l}" for l in K.ALL_LABELS}
    assert G.modify_body(decided("1", ["Jobs › Interview"], star=True, important=True), ids) == {
        "addLabelIds": ["L_Jobs › Interview", "STARRED", "IMPORTANT"]}
    assert G.modify_body(decided("1", ["Jobs › Rejected"], important=True), ids) == {
        "addLabelIds": ["L_Jobs › Rejected", "IMPORTANT"]}
    assert G.modify_body(decided("1", ["Promos"]), ids) == {
        "addLabelIds": ["L_Promos"], "removeLabelIds": ["IMPORTANT"]}


@pytest.mark.parametrize("body", [
    {"addLabelIds": ["L"], "removeLabelIds": ["UNREAD"]},
    {"addLabelIds": ["L"], "removeLabelIds": ["INBOX"]},  # archive_ok defaults to False
    {"addLabelIds": ["L"], "removeLabelIds": ["IMPORTANT", "INBOX"]},
    {"addLabelIds": ["L"], "removeLabelIds": ["INBOX", "IMPORTANT"]},
    {"addLabelIds": ["L"], "removeLabelIds": ["STARRED"]},
    {"addLabelIds": ["L"], "removeLabelIds": ["IMPORTANT", "UNREAD"]},
    {"addLabelIds": ["L"], "removeLabelIds": []},
    {"addLabelIds": ["L", "IMPORTANT"], "removeLabelIds": ["IMPORTANT"]},
    {"removeLabelIds": ["IMPORTANT"]},
    {"addLabelIds": ["L"], "ids": ["x"]},
    {"addLabelIds": ["TRASH"]},
    {"addLabelIds": ["SPAM"]},
    {"addLabelIds": ["UNREAD"]},
    {"addLabelIds": ["INBOX"]},
])
def test_apply_refuses_destructive_changes(body):
    with pytest.raises(AssertionError):
        G.apply(FakeGmail({}), "1", body)


# --------------------------------------------------------------------------- archive (opt-in)

ARCHIVE = frozenset({"Jobs › Alerts", "Jobs › Skip"})
CFG_ARCHIVE = dataclasses.replace(CFG, archive_labels=ARCHIVE)


def test_archive_is_off_unless_configured():
    assert CFG.archive_labels == frozenset()
    assert 'archive_labels = []' in C.EXAMPLE_CONFIG_FILE.read_text()
    for label in K.ALL_LABELS:
        assert K.archive_policy([label], False, False, CFG.archive_labels) is False


@pytest.mark.parametrize("labels,starred,important,expected", [
    (["Jobs › Alerts"], False, False, True),
    (["Jobs › Skip"], False, False, True),
    (["Jobs › Reply"], False, False, False),     # not listed
    (["Promos"], False, False, False),
    (["Jobs › Alerts"], True, False, False),     # something to do: stays visible
    (["Jobs › Alerts"], False, True, False),
    (["Security › Suspicious", "Jobs › Alerts"], False, False, False),
])
def test_archive_policy(labels, starred, important, expected):
    assert K.archive_policy(labels, starred, important, ARCHIVE) is expected


def test_modify_body_archives_only_when_the_decision_says_so():
    ids = {l: f"L_{l}" for l in K.ALL_LABELS}
    d = decided("1", ["Jobs › Alerts"])
    assert G.modify_body({**d, "archive": True}, ids) == {
        "addLabelIds": ["L_Jobs › Alerts"], "removeLabelIds": ["IMPORTANT", "INBOX"]}
    assert G.modify_body({**d, "archive": False}, ids) == G.modify_body(d, ids) == {
        "addLabelIds": ["L_Jobs › Alerts"], "removeLabelIds": ["IMPORTANT"]}


@pytest.mark.parametrize("body", [
    {"addLabelIds": ["L", "STARRED"], "removeLabelIds": ["IMPORTANT", "INBOX"]},
    {"addLabelIds": ["L", "IMPORTANT"], "removeLabelIds": ["INBOX"]},
    {"addLabelIds": ["L"], "removeLabelIds": ["INBOX", "UNREAD"]},
    {"addLabelIds": ["L"], "removeLabelIds": ["IMPORTANT", "INBOX", "STARRED"]},
    {"addLabelIds": ["L", "INBOX"], "removeLabelIds": ["IMPORTANT", "INBOX"]},
])
def test_apply_refuses_bad_archive_shapes_even_when_archiving_is_on(body):
    with pytest.raises(AssertionError):
        G.apply(FakeGmail({}), "1", body, archive_ok=True)


def run_three(tmp_path, cfg, dry_run=False):
    """An alert, a recruiter reply and a promo arrive; returns what was sent to Gmail."""
    g = FakeGmail({i: msg(i) for i in "abc"}, history=[added(i) for i in "abc"], history_id="77")
    labels = {"a": ["Jobs › Alerts"], "b": ["Jobs › Reply"], "c": ["Promos"]}
    t, ids, _ = make(tmp_path, g, echo_runner(lambda m: labels[m["id"]]), dry_run=dry_run, cfg=cfg)
    t.sync()
    return g, ids


def test_configured_label_leaves_the_inbox_and_nothing_else_does(tmp_path):
    g, ids = run_three(tmp_path, CFG_ARCHIVE)
    assert dict(g.modified) == {
        "a": {"addLabelIds": [ids["Jobs › Alerts"]], "removeLabelIds": ["IMPORTANT", "INBOX"]},
        "b": {"addLabelIds": [ids["Jobs › Reply"], "STARRED", "IMPORTANT"]},
        "c": {"addLabelIds": [ids["Promos"]], "removeLabelIds": ["IMPORTANT"]},
    }
    assert "INBOX" not in g._messages["a"]["labelIds"]
    assert "UNREAD" in g._messages["a"]["labelIds"]  # archived, not marked read
    assert "INBOX" in g._messages["b"]["labelIds"] and "INBOX" in g._messages["c"]["labelIds"]


def test_default_config_never_removes_inbox(tmp_path):
    g, _ = run_three(tmp_path, CFG)
    assert len(g.modified) == 3
    assert all("INBOX" not in body.get("removeLabelIds", []) for _, body in g.modified)
    assert all("INBOX" in g._messages[i]["labelIds"] for i in "abc")


def test_dry_run_archives_nothing(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="test-triage"):
        g, _ = run_three(tmp_path, CFG_ARCHIVE, dry_run=True)
    assert g.modified == []
    lines = [r.getMessage() for r in caplog.records if r.name == "test-triage"]
    assert [("archived=yes" in l) for l in lines] == [True, False, False]


def config_with(tmp_path, daemon_line):
    f = tmp_path / "config.toml"
    f.write_text(C.EXAMPLE_CONFIG_FILE.read_text().replace("archive_labels = []", daemon_line), encoding="utf-8")
    return f


def test_config_loads_archive_labels(tmp_path):
    cfg = C.load(config_with(tmp_path, 'archive_labels = ["Jobs › Alerts", "Jobs › Skip"]'))
    assert cfg.archive_labels == ARCHIVE


def test_config_without_the_key_means_no_archiving(tmp_path):
    assert C.load(config_with(tmp_path, "")).archive_labels == frozenset()


@pytest.mark.parametrize("line", ['archive_labels = ["Jobs/Alerts"]', 'archive_labels = ["INBOX"]',
                                  'archive_labels = "Promos"', "archive_labels = [1]"])
def test_bad_archive_labels_are_a_config_error(tmp_path, line):
    with pytest.raises(C.ConfigError, match="archive_labels"):
        C.load(config_with(tmp_path, line))


@pytest.mark.parametrize("labels,expected", [
    (["INBOX"], True),
    (["INBOX", "IMPORTANT"], True),  # Gmail's IMPORTANT marker is ignored
    (["INBOX", "SPAM"], False),
    (["INBOX", "TRASH"], False),
    (["SENT"], False),
    (["INBOX", "L_Promos"], False),  # already triaged
])
def test_needs_triage(labels, expected):
    assert G.needs_triage({"labelIds": labels}, {"L_Promos"}) is expected


def test_gap_days():
    now = 1_000_000.0
    assert G.gap_days(now - 3600, now) == 2
    assert G.gap_days(now - 10 * 86400 - 1, now) == 12
    assert G.gap_days(None, now) == 7


def test_parse_days():
    assert parse_days("30d") == 30 and parse_days("7") == 7


# --------------------------------------------------------------------------- pipeline

def make(tmp_path, g, runner, dry_run=False, history_id="50", cfg=None):
    cfg = cfg or CFG
    state = State(tmp_path / "state.json", persist=not dry_run)
    state.update(history_id=history_id, last_success=None)
    ids = G.ensure_labels(g, create=not dry_run)
    t = Triage(g, cfg, K.Classifier(cfg, runner=runner), ids, state, dry_run,
               logging.getLogger("test-triage"), ping=lambda: None)
    return t, ids, state


def echo_runner(labels_for):
    def run(prompt):
        items = json.loads(prompt.split(K.EMAILS_MARK)[1])
        return json.dumps([dec(m["id"], labels_for(m)) for m in items])
    return run


def added(mid, labels=("INBOX",)):
    return {"messagesAdded": [{"message": {"id": mid, "labelIds": list(labels)}}]}


def test_sync_labels_new_inbox_mail_and_advances_history(tmp_path):
    g = FakeGmail(
        {"a": msg("a", subject="Your receipt"), "b": msg("b"), "s": msg("s", labels=("SENT",))},
        history=[added("a"), added("b"), added("a"), added("s", ("SENT",))], history_id="77",
    )
    t, ids, state = make(tmp_path, g, echo_runner(lambda m: ["Money"] if "receipt" in m["subject"] else ["Promos"]))
    assert t.sync() == 2
    assert g.modified == [("a", {"addLabelIds": [ids["Money"]], "removeLabelIds": ["IMPORTANT"]}),
                              ("b", {"addLabelIds": [ids["Promos"]], "removeLabelIds": ["IMPORTANT"]})]
    assert json.loads((tmp_path / "state.json").read_text())["history_id"] == "77"


def test_sync_classifier_failure_does_not_advance_history(tmp_path):
    g = FakeGmail({"a": msg("a")}, history=[added("a")], history_id="77")

    def boom(prompt):
        raise K.ClassifierError("not logged in")

    t, _, state = make(tmp_path, g, boom)
    with pytest.raises(K.ClassifierError):
        t.sync()
    assert state.history_id == "50" and g.modified == []


def test_sync_404_falls_back_to_messages_list(tmp_path):
    g = FakeGmail({"a": msg("a")}, history_404=True, history_id="900")
    t, ids, state = make(tmp_path, g, echo_runner(lambda m: ["Promos"]))
    assert t.sync() == 1
    assert g.list_queries == ["in:inbox newer_than:7d has:nouserlabels"]
    assert state.history_id == "900"


def test_dry_run_changes_nothing(tmp_path):
    g = FakeGmail({"a": msg("a")}, history=[added("a")], history_id="77")
    t, _, state = make(tmp_path, g, echo_runner(lambda m: ["Promos"]), dry_run=True)
    assert t.sync() == 1
    assert g.modified == []
    assert [l["name"] for l in g._labels] == ["INBOX"]  # no labels created
    assert not (tmp_path / "state.json").exists()


def test_invalid_output_is_skipped_not_labeled(tmp_path):
    g = FakeGmail({"a": msg("a"), "b": msg("b")}, history=[added("a"), added("b")], history_id="77")
    runner = lambda p: json.dumps([dec("a")])  # "b" never valid
    t, ids, state = make(tmp_path, g, runner)
    assert t.sync() == 1
    assert [m for m, _ in g.modified] == ["a"]
    assert state.history_id == "77"  # skip is final: logged, not retried forever


# --------------------------------------------------------------------------- self-triggered notifications

def test_watch_filters_to_inbox_with_api_enum_casing():
    g = FakeGmail({})
    G.watch(g, "projects/p/topics/t")
    assert g.watch_bodies == [{"topicName": "projects/p/topics/t", "labelIds": ["INBOX"],
                               "labelFilterBehavior": "include"}]


def test_own_label_and_star_changes_are_an_empty_batch(tmp_path):
    # What our own messages.modify produces in history: labelsAdded, no messagesAdded.
    ours = {"labelsAdded": [{"message": {"id": "a", "labelIds": ["INBOX", "STARRED"]},
                             "labelIds": ["STARRED"]}]}
    g = FakeGmail({"a": msg("a")}, history=[ours], history_id="78")
    calls = []
    t, _, state = make(tmp_path, g, lambda p: calls.append(p) or "[]")
    assert t.sync() == 0
    assert calls == [] and g.modified == []
    assert g.history_calls == [{"historyTypes": ["messageAdded"], "labelId": "INBOX"}]
    assert state.history_id == "78"  # advances past it, so it is never re-read


def test_every_gmail_call_retries_transient_network_errors(tmp_path):
    from fakes import _Call
    _Call.calls_without_retries = 0
    g = FakeGmail({"a": msg("a")}, history=[added("a")], history_id="77")
    t, _, _ = make(tmp_path, g, echo_runner(lambda m: ["Promos"]))
    t.sync()
    G.watch(g, "projects/p/topics/t")
    G.current_history_id(g)
    G.list_ids(g, "in:inbox")
    assert g.modified and _Call.calls_without_retries == 0


def test_triage_log_columns_survive_pipes_in_subject(tmp_path, caplog):
    g = FakeGmail({"a": msg("a", frm="X | Y <x@y.com>", subject="Your link | 2026-09-27 02:52")},
                  history=[added("a")], history_id="77")
    t, _, _ = make(tmp_path, g, lambda p: json.dumps([dec("a", ["Security"], reason="a | b")]))
    with caplog.at_level(logging.INFO, logger="test-triage"):
        t.sync()
    line = [r.getMessage() for r in caplog.records if r.name == "test-triage"][0]
    assert line.split(" | ") == ["X ¦ Y <x@y.com>", "Your link ¦ 2026-09-27 02:52", "Security", "star=no", "important=no", "archived=no", "a ¦ b"]


# --------------------------------------------------------------------------- star policy

ALWAYS_STAR = {"Jobs › Reply", "Jobs › Interview"}
NEVER_STAR = {
    "Jobs › Applied", "Jobs › Rejected", "Jobs › Alerts", "Jobs › Skip",
    "Money › Receipts", "Money › Statements",
    "Security", "Security › Codes",
    "Orders", "Notifications", "Newsletters", "Newsletters › Crypto", "Newsletters › AI", "Promos",
}
MODEL_DECIDES = {
    "Money", "Money › Invoices", "Money › Trading", "Money › Transfers", "Money › Taxes", "Money › Crypto",
    "Business", "Government", "Education", "Personal", "Appointments", "Health", "Travel",
}


def test_star_sets_partition_the_primary_labels():
    assert K.ALWAYS_STAR == ALWAYS_STAR
    assert K.NEVER_STAR == NEVER_STAR
    assert not ALWAYS_STAR & NEVER_STAR
    assert set(K.PRIMARY) - ALWAYS_STAR - NEVER_STAR == MODEL_DECIDES


@pytest.mark.parametrize("label", K.PRIMARY)
@pytest.mark.parametrize("said", [True, False])
def test_star_is_settled_by_the_label_where_the_label_settles_it(label, said):
    good, bad = K.parse_output(json.dumps([dec("1", [label], star=said)]), {"1"})
    want = True if label in ALWAYS_STAR else False if label in NEVER_STAR else said
    assert not bad and good["1"]["star"] is want


@pytest.mark.parametrize("labels", [["Security › Suspicious"], ["Security › Suspicious", "Jobs › Interview"],
                                    ["Security › Suspicious", "Money › Invoices"]])
def test_suspicious_mail_is_never_starred(labels):
    good, _ = K.parse_output(json.dumps([dec("1", labels, star=True)]), {"1"})
    assert good["1"]["star"] is False


# --------------------------------------------------------------------------- important policy

ALWAYS_IMPORTANT = {"Jobs › Rejected", "Personal"}


def test_important_labels():
    assert K.ALWAYS_IMPORTANT == ALWAYS_IMPORTANT
    assert ALWAYS_IMPORTANT <= set(K.PRIMARY)


@pytest.mark.parametrize("label", K.PRIMARY)
@pytest.mark.parametrize("said", [True, False])
def test_important_is_starred_mail_plus_the_always_important_labels(label, said):
    good, _ = K.parse_output(json.dumps([dec("1", [label], star=said)]), {"1"})
    assert good["1"]["important"] is (good["1"]["star"] or label in ALWAYS_IMPORTANT)


@pytest.mark.parametrize("labels", [["Security › Suspicious"], ["Security › Suspicious", "Personal"],
                                    ["Security › Suspicious", "Jobs › Interview"]])
def test_suspicious_mail_is_never_important(labels):
    good, _ = K.parse_output(json.dumps([dec("1", labels, star=True)]), {"1"})
    assert good["1"]["important"] is False


def test_sync_marks_important_and_clears_gmails_own_guess(tmp_path):
    g = FakeGmail({"a": msg("a"), "b": msg("b"), "c": msg("c")},
                  history=[added("a"), added("b"), added("c")], history_id="77")
    labels = {"a": "Jobs › Rejected", "b": "Newsletters › Crypto", "c": "Jobs › Interview"}
    t, _, _ = make(tmp_path, g, echo_runner(lambda m: [labels[m["id"]]]))
    t.sync()
    bodies = dict(g.modified)
    assert bodies["a"] == {"addLabelIds": [t.label_ids["Jobs › Rejected"], "IMPORTANT"]}
    assert bodies["b"] == {"addLabelIds": [t.label_ids["Newsletters › Crypto"]], "removeLabelIds": ["IMPORTANT"]}
    assert bodies["c"] == {"addLabelIds": [t.label_ids["Jobs › Interview"], "STARRED", "IMPORTANT"]}


# --------------------------------------------------------------------------- owner profile

def test_generic_rules_cover_every_label_once_and_carry_no_owner():
    assert set(K.DEFINITIONS) == set(K.ALL_LABELS)
    assert [l for _, _, labels in K.GROUPS for l in labels] == K.PRIMARY + [K.SUSPICIOUS]
    assert K.RULES == K.build_rules() == P.Profile().rules
    assert K.RULES.startswith("Classify each email into Gmail labels for the account owner.\n")
    assert "OWNER'S" not in K.RULES
    assert K.RULES.count(K.EMAILS_MARK) == 1 and K.RULES.endswith(K.EMAILS_MARK)


def test_owner_notes_sit_directly_under_their_label():
    rules = K.build_rules("keeps bees", {"Business": "the Hive shop", "Jobs › Skip": "night shifts"}, "trust Ada")
    lines = rules.split("\n")
    assert lines[0] == "Classify each email into Gmail labels for the account owner (keeps bees)."
    for label, note in [("Business", "the Hive shop"), ("Jobs › Skip", "night shifts")]:
        i = lines.index(f"- {label}: {K.DEFINITIONS[label]}")
        assert lines[i + 1] == f"  OWNER'S NOTE: {note}"
    assert rules.count("  OWNER'S NOTE: ") == 2
    assert "OWNER'S GENERAL NOTES: trust Ada" in lines
    # the generic text is untouched: removing the owner's lines gives it back
    assert len(lines) == len(K.RULES.split("\n")) + 5


def test_notes_for_a_label_that_does_not_exist_are_refused():
    with pytest.raises(ValueError, match="Jobs/Skip"):
        K.build_rules(notes={"Jobs/Skip": "x"})


def test_missing_profile_is_the_generic_prompt(tmp_path):
    assert P.load(tmp_path / "profile.toml") == P.Profile()


def test_example_profile_loads_and_every_note_names_a_label():
    prof = P.load(C.PROJECT_DIR / "config" / "profile.example.toml")
    assert prof.owner and prof.general and prof.notes
    assert set(prof.notes) <= set(K.ALL_LABELS)
    assert prof.rules.count("  OWNER'S NOTE: ") == len(prof.notes)


def test_profile_text_is_flattened_to_one_line_each(tmp_path):
    f = tmp_path / "profile.toml"
    f.write_text('owner = """a\n  b"""\n[notes]\n"Promos" = """x\n\ny"""\n"Travel" = "  "\n', encoding="utf-8")
    assert P.load(f) == P.Profile(owner="a b", notes={"Promos": "x y"})


@pytest.mark.parametrize("text,why", [
    ('owner = "a"\nnickname = "b"\n', "unknown keys"),
    ('[notes]\n"Jobs/Skip" = "x"\n', "not labels"),
    ('[notes]\n"Promos" = 3\n', "must map"),
    ("owner = 3\n", "must be strings"),
    ("owner = \n", "profile.toml"),
])
def test_bad_profile_is_a_config_error_not_a_guess(tmp_path, text, why):
    f = tmp_path / "profile.toml"
    f.write_text(text, encoding="utf-8")
    with pytest.raises(P.ProfileError, match=why):
        P.load(f)


def test_classifier_sends_the_owners_rules_including_on_retry():
    rules = K.build_rules("keeps bees")
    calls = []

    def runner(prompt):
        calls.append(prompt)
        return "garbage"

    K.Classifier(CFG, runner=runner, rules=rules).classify([{"id": "1"}])
    assert len(calls) == 2 and all(c.startswith(rules) for c in calls)


# --------------------------------------------------------------------------- nothing machine-specific is committed

def test_missing_config_says_what_to_do(tmp_path):
    with pytest.raises(C.ConfigError, match="copy config.example.toml"):
        C.load(tmp_path / "config.toml")


def test_service_installer_fills_in_this_checkout(tmp_path):
    import os
    import subprocess

    template = (C.PROJECT_DIR / "systemd" / "gmail-triage.service.in").read_text()
    assert "/home/" not in template and template.count("@REPO@") == 2
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "systemctl").chmod(0o755)
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    for _ in range(2):  # idempotent
        subprocess.run([str(C.PROJECT_DIR / "scripts" / "install_service.sh")], check=True, env=env, capture_output=True)
    unit = (tmp_path / "cfg" / "systemd" / "user" / "gmail-triage.service").read_text()
    assert "@REPO@" not in unit
    assert f"WorkingDirectory={C.PROJECT_DIR}\n" in unit
    assert f"ExecStart={C.PROJECT_DIR}/.venv/bin/python -m gmail_triage\n" in unit
