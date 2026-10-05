"""One-time OAuth flow: writes ~/.config/gmail-triage/token.json (chmod 600)."""
from __future__ import annotations

import os

from .config import CLIENT_SECRET_FILE, SCOPES, SECRETS_DIR, TOKEN_FILE


def main() -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_SECRET_FILE.exists():
        print(f"Put the Desktop OAuth client JSON at {CLIENT_SECRET_FILE} first.")
        return 1
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)
    os.chmod(CLIENT_SECRET_FILE, 0o600)
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
    # WSL: don't try to launch a browser from Linux; open the printed URL in
    # Windows. The localhost redirect reaches WSL (mirrored networking).
    creds = flow.run_local_server(port=8765, open_browser=False, access_type="offline", prompt="consent")
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)
    print(f"Saved {TOKEN_FILE} (600). Refresh token present: {bool(creds.refresh_token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
