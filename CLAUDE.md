# gmail-triage

Gmail watch → Pub/Sub pull → Haiku → labels/stars/important. Setup and ops: README.md.

## Invariants (tests enforce them — keep it that way)
- Only ever ADD labels (`classifier.ALL_LABELS`, `STARRED`), with ONE exception: the daemon owns Gmail's `IMPORTANT` marker and adds or removes it on each message it triages. Never archive, mark read, unstar, delete, touch spam. `gmail.apply()` asserts this.
- Stars and importance are settled in code, not by the prompt: `classifier.star_policy()` / `important_policy()`. The model's `star` only counts for labels in neither `ALWAYS_STAR` nor `NEVER_STAR`.
- Never guess a label: invalid classifier output → retry once → SKIP (logged).
- `historyId` advances only after a batch is fully processed; a classifier/backend failure leaves it where it was.
- `cli` backend must strip `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` (subscription only, never API billing).
- `--dry-run` writes nothing: no labels created, no modify, no state.json.
- A bad `config.toml` or `profile.toml` is exit 78 (needs a human), never a silent fallback to the generic prompt. A missing profile is fine and means generic.

## Dev
- `uv venv && uv pip install -e '.[dev]'`, `.venv/bin/pytest -q`
- Secrets and the owner's profile live in `~/.config/gmail-triage/` (token.json, client_secret.json, .env, profile.toml) — never in the repo. `config/config.toml` is local and gitignored; the committed one is `config.example.toml`.
- This repo is public. The prompt in `classifier.py` stays generic: anything about one person (who they are, job targets, the senders they deal with) goes in their `profile.toml` as a note under a label, never in `DEFINITIONS`, tests, docs or commit messages.
- Pre-commit hook: `git config core.hooksPath .githooks`.
- Label names are flat Gmail names: never "/" (Gmail nests on it), never a "Triage" prefix; " › " separates category and item. An existing Gmail label with the same name is reused, not duplicated. Colors: `gmail.LABEL_COLORS` (Gmail's fixed palette; applied on create only).
