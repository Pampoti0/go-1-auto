"""Generate a Google Ads OAuth refresh token for ads_agent.py.

Usage:
  python generate_ads_token.py --env-file .env

The script uses GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET from .env by
default, opens the OAuth consent flow, writes a local ads_token.json backup, and
updates GOOGLE_ADS_REFRESH_TOKEN in .env.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/adwords"]


def _load_env(env_file: str) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=True)
    except ImportError:
        pass


def _flow_from_config(client_secrets: str) -> InstalledAppFlow:
    secrets = Path(client_secrets)
    if secrets.exists():
        return InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)

    client_id = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")
    if client_id and client_secret:
        return InstalledAppFlow.from_client_config({
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            },
        }, SCOPES)

    raise SystemExit(
        f"Missing {client_secrets}, and GOOGLE_ADS_CLIENT_ID/GOOGLE_ADS_CLIENT_SECRET "
        "were not found in .env."
    )


def _sync_env_var(key: str, value: str, env_file: str) -> None:
    path = Path(env_file)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{key}={value}"
    out: list[str] = []
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}=") or stripped.startswith(f"#{key}="):
            if not done:
                out.append(new_line)
                done = True
            continue
        out.append(line)
    if not done:
        if out and out[-1].strip():
            out.append("")
        out.append(new_line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Google Ads OAuth refresh token")
    parser.add_argument("--env-file", default=".env", help="Path to local .env file")
    parser.add_argument("--client-secrets", default="google_ads_client_secret.json",
                        help="OAuth Desktop client JSON; optional if GOOGLE_ADS_CLIENT_ID/SECRET exist in .env")
    parser.add_argument("--token", default="ads_token.json", help="Local backup token JSON path")
    parser.add_argument("--no-browser", action="store_true", help="Print the auth URL instead of opening a browser")
    args = parser.parse_args()

    _load_env(args.env_file)
    flow = _flow_from_config(args.client_secrets)
    creds = flow.run_local_server(
        port=0,
        open_browser=not args.no_browser,
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    if not creds.refresh_token:
        raise SystemExit(
            "No refresh_token returned. Remove the old grant from your Google Account, "
            "then run this script again with prompt=consent."
        )

    token_path = Path(args.token)
    token_path.write_text(json.dumps(json.loads(creds.to_json()), ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")
    _sync_env_var("GOOGLE_ADS_REFRESH_TOKEN", creds.refresh_token, args.env_file)
    print(f"Wrote {token_path} and updated GOOGLE_ADS_REFRESH_TOKEN in {args.env_file}")


if __name__ == "__main__":
    main()
