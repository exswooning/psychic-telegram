"""
gcloud_signout.py -- a tenant admin's gcloud sign-in must not outlive its migration.

Live (2026-10-04) this box still held two it had no use for: the default config
signed in as a service account of a project no account's keys belong to, and a
setup that died in August had left an admin of a long-finished tenant signed in
under /tmp. Neither was ever going to be used again on purpose -- but a Cloud setup
PREFERS an existing gcloud identity (full_setup.py, phase 1), so a stale one could
act in the wrong organisation.

So: approving a migration as complete signs every one of them out (Final Report),
and the next tenant's setup signs in fresh as that tenant's own admin, the way
gcloud_browser_auth already does when nothing is signed in.
"""

from __future__ import annotations

import logging

import glob
import os
import subprocess
import tempfile

import gcloud_browser_auth

AMBIENT = os.path.expanduser("~/.config/gcloud")

# Anything that signs in to gcloud or uses a sign-in it made. Signing out under one
# of these would break it half way; the operator approves again once it is done.
USERS_OF_A_SIGN_IN = ("full_setup.py", "separate_credentials.py", "gcp_teardown",
                      "provision_gcp.py", "gcloud auth login")


def throwaways() -> list[str]:
    """The per-setup configs gcloud_browser_auth makes (and deletes, when it is not
    killed first)."""
    return sorted(glob.glob(os.path.join(tempfile.gettempdir(), "cloudsdk-*")))


def _accounts(config: str) -> list[str]:
    try:
        out = subprocess.run(["gcloud", "auth", "list", "--format=value(account)"],
                             env=dict(os.environ, CLOUDSDK_CONFIG=config),
                             capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL)
        return [a for a in out.stdout.split() if a]
    except Exception:      # noqa: BLE001 - no gcloud means nothing is signed in
        return []


def held() -> list[dict]:
    """Every gcloud sign-in this box holds: [{config, accounts}]."""
    return [{"config": c, "accounts": _accounts(c)}
            for c in [AMBIENT, *throwaways()] if os.path.isdir(c)]


def busy() -> str:
    """What is using a gcloud sign-in right now, or ''."""
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:      # noqa: BLE001
        logging.getLogger(__name__).debug("ignored an error", exc_info=True)
        return ""
    for line in out.splitlines():
        for marker in USERS_OF_A_SIGN_IN:
            if marker in line and "gcloud_signout" not in line:
                return line.strip()[:160]
    return ""


def sign_out_all() -> list[str]:
    """Revoke and discard every gcloud sign-in on this box; the accounts signed out."""
    gone: list[str] = []
    for config in throwaways():
        gone += _accounts(config)
        gcloud_browser_auth.cleanup(config)       # revoke on Google's side, then delete
    if os.path.isdir(AMBIENT):
        signed_in = _accounts(AMBIENT)
        if signed_in:
            subprocess.run(["gcloud", "auth", "revoke", "--all", "--quiet"],
                           env=dict(os.environ, CLOUDSDK_CONFIG=AMBIENT),
                           capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            gone += signed_in
    return gone
