"""Run-scoped review of known Google setup notices. Never a blanket consent.

The UI publishes the exact account and notice before the browser can continue.
Only the same unchanged Google page can consume the resulting single-use reply.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

SDK_CLIENT = "32555940559.apps.googleusercontent.com"


def detect(url: str, body: str, email: str) -> dict | None:
    try:
        location = urlsplit(url)
        trusted = (location.scheme == "https" and location.hostname in ("accounts.google.com", "console.cloud.google.com")
                   and location.port in (None, 443) and not location.username)
    except ValueError:
        return None
    if not trusted or not email or not re.search(
            r"(?<![\w.+-])" + re.escape(email) + r"(?![\w-]|\.[\w-])", body, re.I):
        return None
    low = body.lower()
    if (location.hostname == "accounts.google.com" and location.path.endswith("/speedbump/workspacetermsofservice")
            and "welcome to your new account" in low and "i understand" in low):
        category, title, button = ("workspace_welcome", "Review Google's new-account notice", "I understand")
        start = low.index("welcome to your new account")
        end = low.rfind("i understand") + len("i understand")
        content = body[start:end]
    elif (location.hostname == "accounts.google.com" and location.path.startswith("/signin/oauth/")
          and parse_qs(location.query).get("client_id") == [SDK_CLIENT]
          and "google cloud sdk wants access to your google account" in low
          and "this will allow google cloud sdk to" in low):
        category, title, button = ("cloud_sdk_access", "Review Google Cloud SDK access", "Allow")
        start = low.index("google cloud sdk wants access to your google account")
        end = low.find("make sure you trust google cloud sdk", start)
        content = body[start:end if end >= 0 else None]
    elif (location.hostname == "console.cloud.google.com"
          and location.path in ("/projectcreate", "/welcome/new", "/")
          and "create and manage your google cloud" in low
          and "country" in low and "terms of service" in low
          and "i agree to the google cloud platform terms of service" in low
          and "agree and continue" in low):
        category, title, button = ("cloud_console_terms", "Review Google Cloud Platform terms", "Agree and continue")
        content = body
    else:
        return None
    # No OAuth URL/query, terminal code, or executable HTML is published.
    content = re.sub(r"https?://\S+", "[Google link]", content)
    content = " ".join(content.split())
    if len(content) > 12000:
        return None  # never approve a truncated view of permissions or terms
    identity = {"category": category, "adminEmail": email.lower(),
                "path": location.path, "text": content}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {**identity, "noticeHash": fingerprint, "title": title, "buttonLabel": button}


def current_notice(page, email):
    """Cloud terms must be read from the visible account-specific dialog only."""
    if urlsplit(page.url).hostname == "console.cloud.google.com":
        dialog = page.get_by_role("dialog", name="Google Cloud", exact=True)
        if dialog.count() != 1 or not dialog.is_visible():
            return None
        body = dialog.inner_text()
    else:
        body = page.inner_text("body")
    return detect(page.url, body, email)


def request(notice: dict, seconds: float = 600) -> dict:
    return {**notice, "kind": "review", "requestId": secrets.token_hex(24),
            "expiresAt": time.time() + min(max(seconds, 1), 600)}


def write_decision(progress_path: str, request_id: str, notice_hash: str,
                   decision: str) -> None:
    """Called only inside the authenticated, audited API write gate."""
    progress = json.loads(Path(progress_path).read_text())
    current = progress.get("handoff") or {}
    expires = current.get("expiresAt", 0)
    if (current.get("kind") != "review" or current.get("requestId") != request_id
            or current.get("noticeHash") != notice_hash
            or not isinstance(expires, (float, int)) or not math.isfinite(expires)
            or expires <= time.time() or not current.get("adminEmail")):
        raise ValueError("This setup notice changed or expired. Review the current notice.")
    if decision not in ("approve", "decline"):
        raise ValueError("Unknown setup notice decision")
    payload = {"requestId": request_id, "noticeHash": notice_hash,
               "adminEmail": current["adminEmail"], "decision": decision}
    path = progress_path + ".notice-response"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ValueError("A response has already been submitted for this notice.") from exc
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream)


def await_review(page, email, proc, publish, response_path, timeout, log,
                 clock=time.time, sleep=time.sleep):
    """Wait without clicking; approve only a fresh, matching, still-visible page."""
    notice = current_notice(page, email)
    if not notice:
        return "changed"
    pending = request(notice, timeout)
    # Use one clock consistently, including tests with a controlled clock.
    pending["expiresAt"] = clock() + min(max(timeout, 1), 600)
    path = Path(response_path)
    path.unlink(missing_ok=True)  # no reply from an earlier setup can apply
    publish(pending)
    log("  waiting for account-specific review: " + pending["title"] + " (" + email + ")")
    try:
        while clock() < pending["expiresAt"] and (proc is None or proc.poll() is None):
            if path.exists():
                try:
                    reply = json.loads(path.read_text())
                except (OSError, ValueError):
                    reply = {}
                path.unlink(missing_ok=True)  # single use, including rejected replies
                matches = all(reply.get(k) == pending[k]
                              for k in ("requestId", "noticeHash", "adminEmail"))
                if matches and reply.get("decision") == "decline":
                    return "declined"
                if matches and reply.get("decision") == "approve":
                    fresh = current_notice(page, email)
                    if not fresh or fresh["noticeHash"] != pending["noticeHash"]:
                        return "changed"
                    scope = page
                    if notice["category"] == "cloud_console_terms":
                        scope = page.get_by_role("dialog", name="Google Cloud", exact=True)
                        checkbox = scope.get_by_role("checkbox", name=re.compile(
                            r"^I agree to the Google Cloud Platform Terms of Service"))
                        if checkbox.count() != 1 or not checkbox.is_visible() or not checkbox.is_enabled():
                            return "changed"
                        checkbox.check()
                        fresh = current_notice(page, email)
                        if not fresh or fresh["noticeHash"] != pending["noticeHash"]:
                            checkbox.uncheck()
                            return "changed"
                    button = scope.get_by_role("button", name=notice["buttonLabel"], exact=True)
                    if button.count() != 1 or not button.is_visible() or not button.is_enabled():
                        return "changed"
                    button.click()
                    log("  approved setup notice continued for " + email)
                    return "approved"
                log("  ignored a stale or mismatched setup notice response")
            sleep(1)
        return "changed" if proc is not None and proc.poll() is not None else "expired"
    finally:
        path.unlink(missing_ok=True)
        publish(None)
