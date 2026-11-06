"""Service-account credentials. The key is read at run time and never printed or written anywhere.

Where the key can come from, first match wins:
  GOOGLE_SERVICE_ACCOUNT_FILE   path to the JSON key file
  GOOGLE_SERVICE_ACCOUNT_JSON   the JSON itself (e.g. from a CI secret)
  GOOGLE_SA_KEYCHAIN_ITEM       name of a macOS Keychain item holding the JSON (plain or base64)

A service account is a robot Google account. It sees nothing until a folder
is shared with its email address, which is what keeps this tool's reach to
the one folder tree it is meant to file.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess

SCOPES = [
    "https://www.googleapis.com/auth/drive",          # only reaches folders shared with the service account
    "https://www.googleapis.com/auth/spreadsheets",
]


class MissingCredentials(RuntimeError):
    pass


def _parse(raw: str) -> dict:
    raw = raw.strip()
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception as e:
            raise MissingCredentials("service-account key is neither JSON nor base64 JSON") from e
    info = json.loads(raw)
    if info.get("type") != "service_account":
        raise MissingCredentials("the key is not a service-account key (type must be service_account)")
    return info


def load_key_info(env=os.environ, keychain_reader=None) -> dict:
    if env.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        with open(env["GOOGLE_SERVICE_ACCOUNT_FILE"], encoding="utf-8") as fh:
            return _parse(fh.read())
    if env.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return _parse(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
    item = env.get("GOOGLE_SA_KEYCHAIN_ITEM")
    if item:
        reader = keychain_reader or _keychain
        return _parse(reader(item))
    raise MissingCredentials("no Google credentials: set GOOGLE_SERVICE_ACCOUNT_FILE, "
                             "GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SA_KEYCHAIN_ITEM")


def _keychain(item: str) -> str:
    out = subprocess.run(["security", "find-generic-password", "-s", item, "-w"], capture_output=True, text=True)
    if out.returncode != 0:
        raise MissingCredentials(f"Keychain item {item!r} not found")
    return out.stdout


def authorized_session(info: dict):
    """A requests session that adds (and refreshes) the service account's bearer token."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds)


def service_account_email(info: dict) -> str:
    return info.get("client_email", "")
