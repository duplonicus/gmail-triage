"""The owner's profile: who the mail belongs to, in their own words.

The classifier prompt in classifier.py is generic. Anything specific to one
person lives in ~/.config/gmail-triage/profile.toml, outside the repo:

  owner   = "one line about the owner"
  general = "anything that applies across labels"
  [notes]
  "Jobs › Alerts" = "what a matching job looks like"

No profile file means the generic prompt. See config/profile.example.toml.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .classifier import ALL_LABELS, build_rules
from .config import PROFILE_FILE


class ProfileError(ValueError):
    """profile.toml is there but wrong: needs a human, not a restart."""


@dataclass(frozen=True)
class Profile:
    owner: str = ""
    general: str = ""
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def rules(self) -> str:
        return build_rules(self.owner, self.notes, self.general)


def load(path: Path = PROFILE_FILE) -> Profile:
    if not path.exists():
        return Profile()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{path}: {e}") from e
    extra = sorted(set(raw) - {"owner", "general", "notes"})
    if extra:
        raise ProfileError(f"{path}: unknown keys {extra}")
    owner, general, notes = raw.get("owner", ""), raw.get("general", ""), raw.get("notes", {})
    if not isinstance(owner, str) or not isinstance(general, str):
        raise ProfileError(f"{path}: owner and general must be strings")
    if not isinstance(notes, dict) or not all(isinstance(v, str) for v in notes.values()):
        raise ProfileError(f"{path}: [notes] must map a label name to a string")
    unknown = sorted(set(notes) - set(ALL_LABELS))
    if unknown:
        raise ProfileError(f"{path}: [notes] has names that are not labels: {unknown}")
    clean = lambda s: " ".join(s.split())  # noqa: E731 - one line each in the prompt
    return Profile(clean(owner), clean(general), {k: clean(v) for k, v in notes.items() if v.strip()})
