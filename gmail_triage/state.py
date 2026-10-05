"""state.json: the last fully processed historyId, and bookkeeping timestamps."""
from __future__ import annotations

import json
import os
from pathlib import Path


class State:
    def __init__(self, path: Path, persist: bool = True):
        self.path = path
        self.persist = persist  # False in --dry-run: advance in memory only
        self.data: dict = json.loads(path.read_text()) if path.exists() else {}

    @property
    def history_id(self) -> str | None:
        return self.data.get("history_id")

    @property
    def last_success(self) -> float | None:
        return self.data.get("last_success")

    @property
    def last_watch(self) -> float:
        return self.data.get("last_watch", 0.0)

    def update(self, **kv) -> None:
        self.data.update(kv)
        if not self.persist:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)
