"""A tiny in-memory stand-in for the Gmail API client's chained call style."""
from __future__ import annotations

from types import SimpleNamespace

from googleapiclient.errors import HttpError


def http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="x"), b"{}")


class _Call:
    calls_without_retries = 0

    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        if num_retries < 1:
            _Call.calls_without_retries += 1
        return self.fn()


class FakeGmail:
    def __init__(self, messages: dict[str, dict], history: list[dict] | None = None,
                 history_id: str = "100", history_404: bool = False):
        self._messages = messages
        self._history = history or []
        self.history_id = history_id
        self.history_404 = history_404
        self._labels = [{"id": "INBOX", "name": "INBOX"}]
        self.modified: list[tuple[str, dict]] = []
        self.list_queries: list[str] = []
        self.history_calls: list[dict] = []
        self.watch_bodies: list[dict] = []

    # chain ------------------------------------------------------------------
    def users(self):
        return self

    def labels(self):  # noqa: F811 - method name mirrors the API
        return _Labels(self)

    def history(self):  # noqa: F811
        return _History(self)

    def messages(self):  # noqa: F811
        return _Messages(self)

    def watch(self, userId, body):
        self.watch_bodies.append(body)
        return _Call(lambda: {"historyId": self.history_id, "expiration": "1900000000000"})

    def getProfile(self, userId):
        return _Call(lambda: {"historyId": self.history_id})


class _Labels:
    def __init__(self, g):
        self.g = g

    def list(self, userId):
        return _Call(lambda: {"labels": list(self.g._labels)})

    def create(self, userId, body):
        def run():
            lab = {"id": f"Label_{len(self.g._labels)}", "name": body["name"]}
            if "color" in body:
                lab["color"] = body["color"]
            self.g._labels.append(lab)
            return lab
        return _Call(run)


class _History:
    def __init__(self, g):
        self.g = g

    def list(self, userId, startHistoryId, historyTypes, labelId, pageToken=None, maxResults=None):
        self.g.history_calls.append({"historyTypes": historyTypes, "labelId": labelId})

        def run():
            if self.g.history_404:
                raise http_error(404)
            return {"history": self.g._history, "historyId": self.g.history_id}
        return _Call(run)


class _Messages:
    def __init__(self, g):
        self.g = g

    def get(self, userId, id, format, metadataHeaders):
        def run():
            if id not in self.g._messages:
                raise http_error(404)
            return self.g._messages[id]
        return _Call(run)

    def list(self, userId, q, pageToken=None, maxResults=None):
        def run():
            self.g.list_queries.append(q)
            return {"messages": [{"id": i} for i in self.g._messages]}
        return _Call(run)

    def modify(self, userId, id, body):
        def run():
            self.g.modified.append((id, body))
            m = self.g._messages.get(id)
            if m is not None:  # behave like Gmail: the message's labels really change
                kept = [l for l in m["labelIds"] if l not in body.get("removeLabelIds", [])]
                m["labelIds"] = kept + [l for l in body.get("addLabelIds", []) if l not in kept]
            return {}
        return _Call(run)


def msg(mid: str, frm="a@b.com", subject="hi", labels=("INBOX", "UNREAD"), snippet="s", unsub=""):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if unsub:
        headers.append({"name": "List-Unsubscribe", "value": unsub})
    return {"id": mid, "labelIds": list(labels), "snippet": snippet, "payload": {"headers": headers}}
