"""Paths and config.toml loading."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_DIR / "config" / "config.toml"  # yours, gitignored
EXAMPLE_CONFIG_FILE = PROJECT_DIR / "config" / "config.example.toml"
STATE_FILE = PROJECT_DIR / "state.json"
LOG_DIR = PROJECT_DIR / "logs"

SECRETS_DIR = Path.home() / ".config" / "gmail-triage"
TOKEN_FILE = SECRETS_DIR / "token.json"
CLIENT_SECRET_FILE = SECRETS_DIR / "client_secret.json"
ENV_FILE = SECRETS_DIR / ".env"
PROFILE_FILE = SECRETS_DIR / "profile.toml"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/pubsub",
]


@dataclass(frozen=True)
class Config:
    project_id: str
    topic: str
    subscription: str
    backend: str
    cli_model: str
    api_model: str
    timeout_seconds: int
    max_batch: int
    debounce_seconds: float
    safety_sweep_minutes: float
    watch_renew_hours: float
    archive_labels: frozenset[str] = frozenset()

    @property
    def topic_path(self) -> str:
        return f"projects/{self.project_id}/topics/{self.topic}"

    @property
    def subscription_path(self) -> str:
        return f"projects/{self.project_id}/subscriptions/{self.subscription}"


class ConfigError(ValueError):
    """config.toml is missing or wrong: needs a human, not a restart."""


def load(path: Path = CONFIG_FILE) -> Config:
    if not path.exists():
        raise ConfigError(f"{path} missing; copy {EXAMPLE_CONFIG_FILE.name} to {path.name} and set project_id")
    raw = tomllib.loads(path.read_text())
    g, c, d = raw["google"], raw["classifier"], raw["daemon"]
    if c["backend"] not in ("cli", "api"):
        raise ConfigError(f"classifier.backend must be 'cli' or 'api', got {c['backend']!r}")
    from .classifier import ALL_LABELS  # the label names live with the classifier

    archive = d.get("archive_labels", [])
    if not isinstance(archive, list) or not all(isinstance(l, str) for l in archive):
        raise ConfigError("daemon.archive_labels must be a list of label names")
    unknown = sorted(set(archive) - set(ALL_LABELS))
    if unknown:
        raise ConfigError(f"daemon.archive_labels has unknown labels: {unknown}")
    return Config(
        project_id=g["project_id"],
        topic=g["topic"],
        subscription=g["subscription"],
        backend=c["backend"],
        cli_model=c["cli_model"],
        api_model=c["api_model"],
        timeout_seconds=int(c["timeout_seconds"]),
        max_batch=int(c["max_batch"]),
        debounce_seconds=float(d["debounce_seconds"]),
        safety_sweep_minutes=float(d["safety_sweep_minutes"]),
        watch_renew_hours=float(d["watch_renew_hours"]),
        archive_labels=frozenset(archive),
    )
