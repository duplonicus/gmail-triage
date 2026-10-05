#!/usr/bin/env bash
# One-time Google Cloud setup for gmail-triage. Idempotent: safe to re-run.
# Prereq: gcloud auth login, and a billing account. Links it only when told to:
#   BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX scripts/setup_gcloud.sh
set -euo pipefail
cd "$(dirname "$0")/.."

GCLOUD="${GCLOUD:-$(command -v gcloud || echo "$HOME/google-cloud-sdk/bin/gcloud")}"
[ -f config/config.toml ] || { echo "config/config.toml missing: cp config/config.example.toml config/config.toml and set project_id"; exit 2; }
PROJECT=$(sed -n 's/^project_id *= *"\(.*\)".*/\1/p' config/config.toml)
TOPIC=$(sed -n 's/^topic *= *"\(.*\)".*/\1/p' config/config.toml)
SUB=$(sed -n 's/^subscription *= *"\(.*\)".*/\1/p' config/config.toml)

step() { printf '\n== %s\n' "$*"; }

step "project $PROJECT"
"$GCLOUD" projects describe "$PROJECT" >/dev/null 2>&1 || "$GCLOUD" projects create "$PROJECT" --name="Gmail Triage"
"$GCLOUD" config set project "$PROJECT"

step "billing"
if [ "$("$GCLOUD" billing projects describe "$PROJECT" --format='value(billingEnabled)')" != "True" ]; then
  if [ -z "${BILLING_ACCOUNT:-}" ]; then
    echo "Billing is not linked. Pub/Sub requires it. Re-run with BILLING_ACCOUNT=<id> from:"
    echo "  $GCLOUD billing accounts list"
    exit 2
  fi
  "$GCLOUD" billing projects link "$PROJECT" --billing-account="$BILLING_ACCOUNT"
fi
BILLING_ACCOUNT=$("$GCLOUD" billing projects describe "$PROJECT" --format='value(billingAccountName)')
BILLING_ACCOUNT=${BILLING_ACCOUNT#billingAccounts/}

step "APIs"
# billingbudgets: the budget API is called with this project as quota project.
"$GCLOUD" services enable gmail.googleapis.com pubsub.googleapis.com billingbudgets.googleapis.com

step "budget on $BILLING_ACCOUNT"
# 1 unit of the billing account's own currency; alerts email the account's
# billing admins/users (default IAM recipients) at 50/90/100% of actual spend.
if "$GCLOUD" billing budgets list --billing-account="$BILLING_ACCOUNT" \
     --format='value(displayName)' | grep -qx "gmail-triage"; then
  echo "budget exists"
else
  "$GCLOUD" billing budgets create --billing-account="$BILLING_ACCOUNT" \
    --display-name="gmail-triage" --budget-amount=1 \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
fi

step "topic $TOPIC"
"$GCLOUD" pubsub topics describe "$TOPIC" >/dev/null 2>&1 || "$GCLOUD" pubsub topics create "$TOPIC"
"$GCLOUD" pubsub topics add-iam-policy-binding "$TOPIC" \
  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role=roles/pubsub.publisher >/dev/null

step "pull subscription $SUB"
# 1-day retention: a notification only says "look at history"; older ones are
# worthless because startup catch-up re-reads history from state.json anyway.
"$GCLOUD" pubsub subscriptions describe "$SUB" >/dev/null 2>&1 || \
  "$GCLOUD" pubsub subscriptions create "$SUB" --topic="$TOPIC" \
    --ack-deadline=60 --message-retention-duration=1d --expiration-period=never

step "done"
echo "Next: Cloud Console consent screen + Desktop OAuth client (README), then .venv/bin/gmail-triage-auth"
