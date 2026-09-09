"""
signin_challenge.py -- put Google's 2-Step prompt where a person can see it.

The browser that signs into the Admin console runs headless on the VPS's
Xvfb display. When Google answers the password with "Check your phone --
tap 47", or "Enter the code sent to (...) ...23", that prompt is rendered
to a virtual framebuffer nobody is looking at. The job simply stops making
progress for up to 600 seconds and then reports a timeout, and the operator
has no way to know a phone was buzzing the whole time.

The transcript IS the UI for these jobs -- every log() line lands in the
job log the console streams live -- so the fix is to read the prompt off
the page and log it. No new channel, no new endpoint.

Text, not selectors: Google's sign-in DOM is obfuscated and re-generated,
and a class name scraped today is a silent no-op next month. The visible
text is the thing the prompt is actually made of, and it is what a person
would read off the screen anyway.
"""

from __future__ import annotations

import re
from typing import Callable

# Phrases that mean "a human has to do something on another device".
# Lowercased before matching; both apostrophes because Google uses U+2019.
CHALLENGE_PHRASES = (
    "check your phone",
    "tap yes",
    "2-step verification",
    "verify it's you",
    "verify it’s you",
    "verification code",
    "enter the code",
    "get a code",
    "google authenticator",
    "security key",
    "use your passkey",
    "touch your security key",
    "confirm your recovery",
    "approve this sign-in",
    "open the gmail app",
    "yubikey",
)

# Chrome/Google chrome-plating that carries no instruction.
#
# WHOLE lines, not prefixes. As a prefix pattern "google" swallowed
# "Google sent a notification to your Pixel 7 ... then tap 47", which is
# the single most useful line on the whole page.
_NOISE = frozenset("""
google
sign in
next
cancel
back
help
privacy
terms
resend it
try another way
more ways to verify
show more options
learn more
""".split("\n"))
_NOISE_RE = re.compile(r"^(english|français|deutsch|español)\b.*$|^\W+$", re.I)


def _lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if line and line.lower() not in _NOISE and not _NOISE_RE.match(line):
            out.append(line)
    return out


def describe(text: str, max_lines: int = 6) -> str:
    """The prompt as one readable string, or "" if this is not a challenge.

    Whole-page text in, because that is what a caller can get from any
    browser page without knowing anything about its structure.
    """
    if not text:
        return ""
    if not any(p in text.lower() for p in CHALLENGE_PHRASES):
        return ""
    kept = [ln for ln in _lines(text) if len(ln) <= 160][:max_lines]
    return " / ".join(kept)


def from_page(page) -> str:
    """describe() for a Playwright page. Never raises: a page mid-navigation
    throws on inner_text, and a diagnostic must not kill the sign-in it is
    diagnosing."""
    try:
        return describe(page.inner_text("body"))
    except Exception:  # noqa: BLE001
        return ""


# Set by a long-running job that wants the prompt somewhere other than its
# own transcript -- full_setup puts it in the progress file the UI polls, so
# a browser sitting on the setup page can say "your phone is asking for 47"
# while it happens. A module-level hook rather than an argument threaded
# through three call sites, because the Watcher is constructed deep inside
# dwd_helper and gcloud_browser_auth and every caller in between would have
# to carry a parameter it has no interest in.
#
# Called with the challenge text, or "" the moment it clears.
REPORTER: "Callable[[str], None] | None" = None


def _report(text: str) -> None:
    if REPORTER is None:
        return
    try:
        REPORTER(text)
    except Exception:  # noqa: BLE001 - a reporter must never break a sign-in
        pass


class Watcher:
    """Logs a challenge once, and again only when it changes.

    The sign-in loops poll every 1-2 seconds. Logging the prompt on every
    pass would bury it in three hundred identical lines, which is the same
    as not showing it.
    """

    def __init__(self, log) -> None:
        self._log = log
        self._last = ""

    def check(self, pages) -> str:
        for pg in pages:
            found = from_page(pg)
            if not found:
                continue
            if found != self._last:
                self._last = found
                self._log(f"  [2-STEP] {found}")
                self._log("  [2-STEP] this is waiting for you on a device -- "
                          "the browser here is headless and cannot do it.")
                _report(found)
            return found
        if self._last:
            # It cleared: somebody answered it. Say so, or the banner it
            # raised stays up for the rest of the run.
            _report("")
        self._last = ""
        return ""
