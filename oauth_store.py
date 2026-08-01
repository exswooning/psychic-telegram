"""
oauth_store.py
==============
OAuth credentials for a whole tenant, obtained by an admin clicking "Allow"
instead of by anyone creating a service account.

Why this exists alongside the service-account path
--------------------------------------------------
Domain-wide delegation is the right shape for a back-office tool run by
someone comfortable in the Cloud console. It is the wrong shape for a product:
it needs a GCP project, a service account, a downloaded JSON key (which Google
now blocks by default on new organisations), and a comma-separated scope
string pasted into the Admin Console -- none of which has an API, and several
of which need roles a normal admin does not have.

OAuth moves all of that into one consent screen. An administrator signs in,
reads what is being requested, and approves it for their domain. That is a
step a non-technical person can genuinely complete.

What it costs
-------------
* Drive and Gmail scopes are *restricted*. Using them against domains other
  than your own requires Google app verification plus an annual CASA security
  assessment. Until then the consent screen shows an unverified-app warning
  and is capped at 100 users. An app marked **Internal** to a single
  organisation is exempt from all of it -- which is what makes this testable
  today.
* A refresh token is long-lived and grants exactly the scopes consented to.
  It is stored here mode 0600. Treat the token file the way you would have
  treated the JSON key.

Token layout: one file per tenant, so revoking one side does not disturb the
other.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Optional


class TokenStore:
    """Reads and writes the per-tenant OAuth token files."""

    def __init__(self, directory: str = "./oauth"):
        self.dir = directory

    def _path(self, tenant: str) -> str:
        return os.path.join(self.dir, f"{tenant}-token.json")

    def exists(self, tenant: str) -> bool:
        p = self._path(tenant)
        return os.path.exists(p) and os.path.getsize(p) > 0

    def load(self, tenant: str) -> Optional[dict]:
        if not self.exists(tenant):
            return None
        with open(self._path(tenant), encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, tenant: str, data: dict) -> None:
        os.makedirs(self.dir, exist_ok=True)
        os.chmod(self.dir, stat.S_IRWXU)          # 0700
        path = self._path(tenant)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)   # 0600

    def clear(self, tenant: str) -> None:
        """Forget a tenant's token locally. Note this does NOT revoke it at
        Google -- that is done from the domain's admin console, and saying so
        matters because 'disconnected' should not imply 'access removed'."""
        if os.path.exists(self._path(tenant)):
            os.remove(self._path(tenant))

    def describe(self, tenant: str) -> dict:
        data = self.load(tenant)
        if not data:
            return {"connected": False}
        return {
            "connected": True,
            "account": data.get("account", ""),
            "domain": data.get("domain", ""),
            "scopes": data.get("scopes", []),
            "obtained": data.get("obtained", ""),
        }


def build_flow(client_config: dict, scopes: list[str], redirect_uri: str):
    """
    An installed-app OAuth flow.

    `access_type=offline` plus `prompt=consent` is what returns a refresh
    token; without both, a second authorisation of the same account silently
    yields no refresh token and the migration dies hours later when the first
    access token expires.
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(client_config, scopes=scopes)
    flow.redirect_uri = redirect_uri
    return flow


def authorization_url(flow, login_hint: str = "") -> str:
    kwargs = {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if login_hint:
        # Nudges Google to preselect the right admin account, which matters a
        # lot when someone is signed into several.
        kwargs["login_hint"] = login_hint
    url, _state = flow.authorization_url(**kwargs)
    return url


def credentials_to_dict(creds, account: str = "", domain: str = "") -> dict:
    from datetime import datetime, timezone

    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
        "account": account,
        "domain": domain,
        "obtained": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def credentials_from_dict(data: dict):
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )
