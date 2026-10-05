#!/usr/bin/env bash
# Install (or refresh) the systemd user unit for this checkout. Idempotent.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
# -f: an earlier install may have left a symlink into the repo here.
rm -f "$UNIT_DIR/gmail-triage.service"
sed "s|@REPO@|$REPO|g" "$REPO/systemd/gmail-triage.service.in" > "$UNIT_DIR/gmail-triage.service"
systemctl --user daemon-reload
echo "Installed $UNIT_DIR/gmail-triage.service for $REPO"
echo "Start it:  systemctl --user enable --now gmail-triage"
