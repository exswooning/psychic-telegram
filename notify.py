"""
notify.py -- tell a person, if the operator has said how.

Nothing here is configured by default and nothing here can fail a run: an
unconfigured channel is skipped, a failed post is returned (never raised), and
every call has a short timeout, because this runs inside the watcher and a slow
notification server must not stall the thing that watches.

Channels, all optional, all plain HTTP from the standard library:

  INCIDENT_NTFY_TOPIC         ntfy.sh (or INCIDENT_NTFY_SERVER): a push to the
                              phone with no account and no SMTP. The topic name
                              is the secret, so make it long and unguessable.
  INCIDENT_WEBHOOK_URL        POST {"text","content","title","severity"}: reads
                              as-is in Slack and Discord incoming webhooks.
  INCIDENT_GITHUB_REPO        owner/name, with INCIDENT_GITHUB_TOKEN: opens an
                              issue labelled "incident". A Claude Code routine
                              can be set to fire on that, which is how an error
                              reaches Claude with nobody at a terminal.
"""
from __future__ import annotations

import json
import os
import urllib.request

TIMEOUT = 8.0


def _post(url: str, body: bytes, headers: dict) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:      # noqa: S310 - operator-set URL
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except Exception as exc:      # noqa: BLE001 - reported, never raised
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def channels() -> list[str]:
    out = []
    if os.getenv("INCIDENT_NTFY_TOPIC"):
        out.append("ntfy")
    if os.getenv("INCIDENT_WEBHOOK_URL"):
        out.append("webhook")
    if os.getenv("INCIDENT_GITHUB_REPO") and os.getenv("INCIDENT_GITHUB_TOKEN"):
        out.append("github")
    return out


def send(title: str, message: str, severity: str = "info", *,
         issue_body: str | None = None) -> list[dict]:
    """Post to every configured channel. `issue_body` is the long hand-off for
    the GitHub channel; the short message goes to the phone and the webhook."""
    results = []
    topic = os.getenv("INCIDENT_NTFY_TOPIC")
    if topic:
        server = os.getenv("INCIDENT_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        prio = {"error": "high", "warn": "default", "info": "low"}.get(severity, "default")
        ok, detail = _post(f"{server}/{topic}", message.encode("utf-8"),
                           {"Title": title[:200].encode("ascii", "replace").decode(),
                            "Priority": prio, "Tags": severity})
        results.append({"channel": "ntfy", "ok": ok, "detail": detail})
    hook = os.getenv("INCIDENT_WEBHOOK_URL")
    if hook:
        body = json.dumps({"title": title, "severity": severity,
                           "text": f"*{title}*\n{message}", "content": f"**{title}**\n{message}"}).encode()
        ok, detail = _post(hook, body, {"Content-Type": "application/json"})
        results.append({"channel": "webhook", "ok": ok, "detail": detail})
    repo, token = os.getenv("INCIDENT_GITHUB_REPO"), os.getenv("INCIDENT_GITHUB_TOKEN")
    if repo and token and severity in ("error", "warn"):
        body = json.dumps({"title": title[:240], "body": (issue_body or message)[:60000],
                           "labels": ["incident"]}).encode()
        ok, detail = _post(f"https://api.github.com/repos/{repo}/issues", body,
                           {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                            "Content-Type": "application/json", "User-Agent": "bitport-watcher"})
        results.append({"channel": "github", "ok": ok, "detail": detail})
    return results
