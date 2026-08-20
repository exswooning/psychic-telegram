"""
gcloud_browser_auth.py
=======================
Authenticates `gcloud` itself, non-interactively, using the SAME admin
email/password already typed into Quick Setup's Sign-in-with-Google step --
so a fresh tenant with no service-account key on file yet can still be
fully self-served: one sign-in, and this VPS creates the Cloud project too.

Why this exists
----------------
provision_gcp.py needs an identity with org-level GCP project-creation
rights. Before this module existed, the only way to give it one was for an
operator to run `gcloud auth login` by hand, on their OWN machine, and keep
that identity live forever -- which meant either every tenant's project got
created (and billed) under the operator's own account, or the admin had to
run provision_gcp.py locally themselves and upload the resulting key. Both
put a manual step between "type your password" and "it's done".

This closes that gap the same way dwd_helper.py already closes it for
domain-wide delegation: drive a REAL browser through Google's own sign-in,
using credentials that are already in hand for this exact request, so the
Cloud project ends up owned by the tenant's OWN admin -- not the operator --
and the OAuth grant this needs is no broader than what a human running
`gcloud auth login` would have granted by hand anyway.

Ephemeral by design
--------------------
Every call gets its own throwaway `CLOUDSDK_CONFIG` directory (so two
tenants' credentials can never cross-contaminate a shared `~/.config/gcloud`
on this multi-tenant box), and the caller is expected to call cleanup()
once provisioning for that tenant is done -- which revokes the OAuth grant
on Google's side, not just forgets it locally, mirroring the "password is
never kept" rule the rest of this codebase already follows for the admin
password itself.

Best-effort, same as dwd_helper.py
------------------------------------
Google actively fingerprints automation and may still answer with a
captcha, a "confirm it's you" prompt, or a security-key challenge -- none
of which are reliably scriptable. When that happens this reports a clear
timeout with a screenshot saved for diagnosis rather than hanging, exactly
like dwd_helper.py's own sign-in loop; connect over VNC (see connect_vps.sh)
to finish it by hand, then re-run.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


def log(msg: str) -> None:
    # stderr, not stdout -- see dwd_helper.log()'s comment: full_setup.py
    # --json prints its final structured result to stdout, and a caller
    # capturing that stream would otherwise get it interleaved with these
    # progress lines, making the whole thing unparseable.
    print(f"[gcloud-auth] {msg}", file=sys.stderr, flush=True)


_AUTH_URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/auth\S+")


def login(email: str, password: str, timeout: int = 180) -> tuple[bool, str, str]:
    """Returns (ok, detail, cloudsdk_config_dir).

    cloudsdk_config_dir is '' on failure (already cleaned up). On success it
    is the caller's responsibility to point every subsequent `gcloud`
    subprocess at it via env={"CLOUDSDK_CONFIG": cloudsdk_config_dir, ...}
    and to call cleanup() when done with it.
    """
    if not shutil.which("gcloud"):
        return False, "gcloud is not installed", ""

    cloudsdk_config = tempfile.mkdtemp(prefix="cloudsdk-")
    env = dict(os.environ, CLOUDSDK_CONFIG=cloudsdk_config)

    # --no-launch-browser and a real stdin pipe.
    #
    # On a headless host gcloud cannot receive the OAuth redirect, so it
    # falls back to printing a URL and BLOCKING on a code read from stdin --
    # which it did even without the flag, since there was no browser it
    # could launch itself. Asking for that flow explicitly makes the
    # behaviour deterministic rather than dependent on what gcloud infers
    # about the environment.
    #
    # stdin=PIPE is the half that was missing: with stdin inherited from a
    # detached service there is nothing to read, so gcloud got EOF and
    # reported `gcloud crashed (EOFError): EOF when reading a line` --
    # which reads like a gcloud bug and is really "nobody pasted the code".
    # _drive_browser now writes it here.
    proc = subprocess.Popen(
        ["gcloud", "auth", "login", "--quiet", "--no-launch-browser"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env=env,
    )

    url = None
    buf = ""
    deadline = time.time() + 20
    while time.time() < deadline and url is None and proc.poll() is None:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.1)
            continue
        buf += line
        m = _AUTH_URL_RE.search(line)
        if m:
            url = m.group(0)

    if not url:
        proc.kill()
        rest = proc.stdout.read() if proc.stdout else ""
        shutil.rmtree(cloudsdk_config, ignore_errors=True)
        return False, (buf + rest).strip()[-300:] or "gcloud printed no sign-in URL", ""

    log(f"driving browser sign-in for {email}")
    _drive_browser(proc, url, email, password, timeout)

    try:
        rc = proc.wait(timeout=max(timeout - 20, 10))
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    rest = proc.stdout.read() if proc.stdout else ""
    full_out = (buf + rest).strip()

    if rc == 0 and "You are now logged in as" in full_out:
        log(f"signed in as {email}")
        return True, full_out[-300:], cloudsdk_config

    shutil.rmtree(cloudsdk_config, ignore_errors=True)
    detail = full_out[-300:] if full_out else "sign-in did not complete in time"
    return False, detail, ""


def cleanup(cloudsdk_config_dir: str) -> None:
    """Revokes the OAuth grant on Google's side, then discards the local
    config. Best-effort: a failed revoke must not block whatever the
    caller does next -- the directory is thrown away either way, and a
    grant that outlives its one use is a much smaller problem than a
    provisioning run that can't finish because cleanup raised."""
    if not cloudsdk_config_dir:
        return
    try:
        env = dict(os.environ, CLOUDSDK_CONFIG=cloudsdk_config_dir)
        subprocess.run(["gcloud", "auth", "revoke", "--all", "--quiet"],
                       capture_output=True, timeout=30,
                       stdin=subprocess.DEVNULL, env=env)
    except Exception:      # noqa: BLE001
        pass
    finally:
        shutil.rmtree(cloudsdk_config_dir, ignore_errors=True)


# Same two selectors dwd_helper.py already found and pinned: Google's email
# box is `#identifierId`, type="text" -- NOT type="email" -- and the
# password box needs the is_visible() guard because a hidden one is also
# present on the identifier page.
_EMAIL_SEL = '#identifierId, input[name="identifier"], input[type="email"]'
_PW_SEL = 'input[type="password"][name="Passwd"], input[type="password"]'
_CONSENT_LABELS = ("Allow", "Continue", "I agree", "Got it")

# Where Google shows the verification code at the end of the out-of-band
# sign-in flow. It has moved between a readonly <input>, a <textarea>, and a
# plain element, so all three are tried before falling back to scraping the
# page text.
_CODE_SEL = ('input#code', 'input[readonly][value^="4/"]', 'textarea',
             'input[type="text"][readonly]')
_CODE_RE = re.compile(r"\b4/[0-9A-Za-z_\-]{20,}")


def _extract_auth_code(pg) -> str:
    """The code gcloud is waiting for on stdin, from the consent result page.

    On a headless host `gcloud auth login` cannot receive a redirect, so it
    prints a URL and blocks reading a code from stdin. Driving the browser
    through consent is only half the flow -- without this the sign-in
    completes in the browser and gcloud sits there until it gets EOF and
    reports `gcloud crashed (EOFError): EOF when reading a line`, which
    reads like a gcloud bug rather than a missing paste.
    """
    for sel in _CODE_SEL:
        try:
            loc = pg.locator(sel)
            for i in range(min(loc.count(), 3)):
                val = (loc.nth(i).input_value() if "input" in sel or "textarea" in sel
                       else loc.nth(i).inner_text())
                m = _CODE_RE.search(val or "")
                if m:
                    return m.group(0)
        except Exception:      # noqa: BLE001 - try the next shape
            continue
    try:
        m = _CODE_RE.search(pg.inner_text("body"))
        if m:
            return m.group(0)
    except Exception:      # noqa: BLE001
        pass
    return ""


def _fill_visible(pg, selector: str, value: str) -> bool:
    loc = pg.locator(selector)
    for i in range(min(loc.count(), 4)):
        box = loc.nth(i)
        try:
            if not box.is_visible() or not box.is_enabled():
                continue
            box.click()
            # type() rather than fill(): the sign-in form listens for real
            # key events to enable Next, and a programmatic value set can
            # leave the button disabled -- see dwd_helper.py's own note.
            box.type(value, delay=50)
            pg.keyboard.press("Enter")
            return True
        except Exception:      # noqa: BLE001 - try the next match
            continue
    return False


def _drive_browser(proc, url: str, email: str, password: str, timeout: int) -> None:
    """Types email/password, clicks through gcloud's own OAuth consent
    screen, then just watches `proc` -- the local `gcloud auth login`
    listener catching the browser's redirect to localhost is the only
    real signal that this succeeded, so the loop exits the moment that
    process does rather than sleeping out a fixed timeout regardless."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    import dwd_helper  # noqa: PLC0415 - reuse the real-browser launcher

    with sync_playwright() as p:
        browser = dwd_helper._installed_browser_launch(p, headful=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        typed_email = typed_pw = sent_code = False
        deadline = time.time() + timeout
        while time.time() < deadline and proc.poll() is None:
            for pg in list(browser.contexts[0].pages):
                try:
                    if not typed_email and _fill_visible(pg, _EMAIL_SEL, email):
                        log("  entered the admin email")
                        pg.wait_for_timeout(3000)
                        typed_email = True
                        continue
                    if typed_email and not typed_pw and _fill_visible(pg, _PW_SEL, password):
                        log("  entered the password")
                        pg.wait_for_timeout(4000)
                        typed_pw = True
                        continue
                    if typed_pw:
                        for label in _CONSENT_LABELS:
                            btn = pg.get_by_role("button", name=label)
                            if btn.count() > 0 and btn.first.is_visible():
                                btn.first.click()
                                pg.wait_for_timeout(1500)
                                break
                        # Consent done: gcloud is blocked reading a code from
                        # stdin. Hand it over -- clicking Allow is only half
                        # the flow, and without this it waits until EOF and
                        # reports "gcloud crashed (EOFError)", which looks
                        # like a gcloud bug rather than a missing paste.
                        if not sent_code:
                            code = _extract_auth_code(pg)
                            if code:
                                try:
                                    proc.stdin.write(code + "\n")
                                    proc.stdin.flush()
                                    log("  handed the verification code to gcloud")
                                    sent_code = True
                                except Exception:      # noqa: BLE001
                                    pass
                except Exception:      # noqa: BLE001 - keep polling
                    continue
            time.sleep(1)

        if proc.poll() is None:
            log("  stalled -- likely 2FA/captcha. Saving a screenshot for "
                "diagnosis; connect over VNC (see connect_vps.sh) to finish "
                "signing in by hand, then re-run.")
            try:
                for i, pg in enumerate(browser.contexts[0].pages):
                    pg.screenshot(path=f"/tmp/gcloud-auth-timeout-{i}.png")
            except Exception:      # noqa: BLE001 - diagnostics only
                pass
        else:
            # gcloud caught the OAuth redirect -- sign-in genuinely
            # succeeded. Same authenticated session, one more thing to
            # clear before handing back to the caller: see
            # _accept_cloud_console_tos()'s own docstring for why.
            try:
                outcome = _accept_cloud_console_tos(page, timeout=30)
                log(f"  Cloud Console ToS check: {outcome}")
            except Exception as exc:      # noqa: BLE001 - best-effort
                log(f"  Cloud Console ToS check raised ({exc}) -- continuing anyway")
        browser.close()


# Wording changes without notice (same caveat dwd_helper.py's own
# selectors carry) -- matched by CONTENT near a checkbox rather than a
# fixed id/aria-label, and every plausible submit-button label tried in
# turn, so a future console redesign degrades to "did nothing" rather
# than a crash.
_TOS_CHECKBOX_HINTS = ("terms of service", "i have read and agree")
_TOS_CONTINUE_LABELS = ("Agree and continue", "AGREE AND CONTINUE", "Agree and Continue",
                        "I agree", "Accept", "Continue")

# Confirmed live, the hard way, against a real never-used-GCP-before
# account: neither the plain console homepage
# (console.cloud.google.com/welcome/new, loads completely normally --
# full nav, project picker, nothing to accept) nor the generic
# console.developers.google.com/terms/universal page (reported "The
# requested Terms of Service have already been accepted" -- a DIFFERENT,
# already-satisfied agreement) is what gates `gcloud projects create`'s
# "Callers must accept Terms of Service" (type: TOS, subject: cloud).
# The real gate only renders as a "Welcome" modal -- country, a "Terms of
# Service" checkbox for the actual Google Cloud Platform ToS, an
# "Agree and continue" button disabled until it's checked -- on the
# **Create Project** page specifically. The other two stay as harmless,
# fast fallbacks in case Google ever also gates one of them.
_TOS_CANDIDATE_URLS = (
    "https://console.cloud.google.com/projectcreate",
    "https://console.developers.google.com/terms/universal",
    "https://console.cloud.google.com/",
)


def _accept_cloud_console_tos(page, timeout: int = 30) -> str:
    """Best-effort: visits one or more known Google Terms-of-Service
    consent pages, on the session the browser is already signed into, and
    clicks through whichever one actually gates this identity if it has
    never used GCP before.

    Why this exists: `gcloud projects create` on a brand-new Google
    account fails with "Callers must accept Terms of Service" -- a real
    Google-side gate with no gcloud/API equivalent, only a web consent
    screen, confirmed live against a fresh trial account. It never shows
    again once accepted, so this costs a few seconds on a genuinely
    first-time identity and is a same-page no-op every time after --
    worth doing unconditionally rather than only after seeing the error
    once, since the browser session needed to clear it is already open
    right here and won't be by the time provision_gcp.py's plain `gcloud`
    subprocess call hits that error on its own.

    Returns "accepted" / "not_needed" / "could_not_find_prompt" -- logged
    by the caller, never raised. A failure here must fall through to
    provision_gcp.py's own (now more specific, see
    _explain_project_create_failure) error rather than aborting sign-in,
    since sign-in itself already succeeded.
    """
    saw_prompt_without_a_button = False
    for url in _TOS_CANDIDATE_URLS:
        outcome = _try_accept_tos_at(page, url, timeout)
        if outcome == "accepted":
            return outcome
        if outcome == "could_not_find_prompt":
            saw_prompt_without_a_button = True
    # Distinguishing these matters for diagnosis: "not_needed" means every
    # candidate genuinely had nothing to accept; "could_not_find_prompt"
    # means at least one had a real ToS checkbox this couldn't find a
    # matching submit button for -- collapsing the two into one outcome
    # would hide a console redesign behind a log line that reads as fine.
    return "could_not_find_prompt" if saw_prompt_without_a_button else "not_needed"


def _try_accept_tos_at(page, url: str, timeout: int) -> str:
    tag_base = re.sub(r"[^a-z0-9]+", "-", url.split("//", 1)[-1]).strip("-")[:40]

    def _save_diagnostics(tag: str) -> None:
        # Best-effort: this function's whole job is to be resilient to a
        # console redesign, so the FIRST time it guesses wrong there needs
        # to be something to look at other than a log line saying
        # "not_needed" -- which is indistinguishable from actually true.
        try:
            page.screenshot(path=f"/tmp/gcloud-tos-{tag_base}-{tag}.png")
            with open(f"/tmp/gcloud-tos-{tag_base}-{tag}.txt", "w", encoding="utf-8") as fh:
                fh.write(f"URL: {page.url}\n\n{page.inner_text('body')[:3000]}")
        except Exception:      # noqa: BLE001 - diagnostics only
            pass

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2000)  # a client-rendered consent dialog needs a beat to appear
    except Exception:      # noqa: BLE001
        _save_diagnostics("goto-failed")
        return "could_not_find_prompt"

    # Always kept, not just on failure: an "accepted" outcome that turns
    # out to have clicked the wrong thing (confirmed live -- see the
    # comment on _TOS_CANDIDATE_URLS) is just as worth being able to see
    # after the fact as an outright miss.
    _save_diagnostics("before-click")

    def _click_continue() -> bool:
        # Confirmed live: the real button ("Agree and continue") starts
        # disabled until the ToS checkbox above it is checked -- without
        # is_enabled() here, the button-only fallback path below would try
        # clicking it on the very first loop pass, before any checkbox has
        # been checked, and Playwright raises (rather than no-ops) on a
        # click against a disabled element.
        for label in _TOS_CONTINUE_LABELS:
            btn = page.get_by_role("button", name=label)
            if btn.count() > 0 and btn.first.is_visible() and btn.first.is_enabled():
                try:
                    btn.first.click()
                except Exception:      # noqa: BLE001 - try the next label
                    continue
                page.wait_for_timeout(2000)
                _save_diagnostics("after-click")
                return True
        return False

    start = time.time()
    deadline = start + timeout
    checked = False
    while time.time() < deadline:
        boxes = page.locator('input[type="checkbox"]')
        n = boxes.count()
        for i in range(min(n, 8)):
            box = boxes.nth(i)
            try:
                if not box.is_visible():
                    continue
                container = box.locator(
                    "xpath=ancestor::*[self::label or self::div][1]")
                text = container.inner_text(timeout=500).lower()
            except Exception:      # noqa: BLE001 - try the next checkbox
                continue
            if any(hint in text for hint in _TOS_CHECKBOX_HINTS):
                try:
                    if not box.is_checked():
                        box.check(timeout=2000)
                    checked = True
                except Exception:      # noqa: BLE001
                    pass
        if checked:
            break
        # Some of Google's own consent pages are a single button with no
        # checkbox at all -- a visible, known-labelled button is its own
        # signal this is a consent screen worth clicking through.
        if _click_continue():
            return "accepted"
        # Give the page a few seconds to finish loading before concluding
        # there is genuinely nothing to accept here (the ordinary case:
        # already accepted, or this candidate URL isn't the right gate).
        if n == 0 and time.time() - start > 5:
            _save_diagnostics("not-needed")
            return "not_needed"
        time.sleep(1)

    if not checked:
        _save_diagnostics("timed-out-unchecked")
        return "not_needed"

    if _click_continue():
        return "accepted"
    _save_diagnostics("checked-but-no-button")
    return "could_not_find_prompt"


# Confirmed live (ensure_apis.py's own NEEDS_CONSOLE_CONFIG comment):
# `gcloud services enable chat.googleapis.com` reports success and
# serviceusage reports ENABLED, but every chat.spaces().create() call still
# 404s "Google Chat app not found" until an app (name + status) is
# configured at this exact Console page. There is no API and no gcloud
# command for it -- and unlike the ToS check above, this is NOT a one-time
# per-identity gate: provision_gcp.py mints a brand-new GCP project
# (`wsmig-{src,tgt}-NNNNN`) on every tenant setup, so without this, every
# single seed/migration run needing chat_engine.py or seed_chat() would
# 404 forever, on every new tenant, until someone clicked through this page
# by hand -- exactly the "no step that needs a human to know which console
# to open" tax this whole file exists to remove.
_CHAT_NAME_SEL = ('input[aria-label*="app name" i]',
                  'input[aria-label*="Name" i]')
_CHAT_STATUS_LABELS = ("LIVE", "Live", "ON", "On")
_CHAT_SAVE_LABELS = ("SAVE", "Save")


def configure_chat_app(email: str, password: str, project: str,
                       timeout: int = 90) -> tuple[bool, str]:
    """Opens its own browser, signs in if needed, and delegates the actual
    form-filling to _fill_chat_app_form() (kept separate so that logic is
    directly testable with a fake page, the same split _accept_cloud_console_tos
    already uses -- this half is just browser lifecycle, exercised live).

    Needs its own fresh sign-in: the session gcloud_browser_auth.login()
    opened to authenticate `gcloud auth login` is already closed and its
    OAuth grant revoked by the time this runs (provision_gcp.py needs the
    project to exist first, and that happens after login()/cleanup()).

    Returns (ok, detail). Never raises -- a Chat app that isn't configured
    should not fail a setup run that Chat is only one of many services in;
    the caller logs detail and moves on, same as every other best-effort
    step in this module.
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    import dwd_helper  # noqa: PLC0415 - reuse the real-browser launcher

    url = (f"https://console.cloud.google.com/apis/api/"
          f"chat.googleapis.com/hangouts-chat?project={project}")

    with sync_playwright() as p:
        browser = dwd_helper._installed_browser_launch(p, headful=True)
        page = browser.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=max(timeout * 1000, 30000))
        except Exception as exc:      # noqa: BLE001
            browser.close()
            return False, f"could not open the Chat config page: {exc}"

        # Sign in if this identity has no existing Console session -- same
        # dance as _drive_browser, targeting whichever page the redirect
        # lands the browser on rather than a fixed URL.
        typed_email = typed_pw = False
        deadline = time.time() + timeout
        while time.time() < deadline and "accounts.google.com" in page.url:
            if not typed_email and _fill_visible(page, _EMAIL_SEL, email):
                log("  entered the admin email")
                page.wait_for_timeout(2500)
                typed_email = True
                continue
            if typed_email and not typed_pw and _fill_visible(page, _PW_SEL, password):
                log("  entered the password")
                page.wait_for_timeout(4000)
                typed_pw = True
                continue
            page.wait_for_timeout(500)

        if "accounts.google.com" in page.url:
            _save_chat_diagnostics(page, project, "stalled-signin")
            browser.close()
            return False, "sign-in did not complete (likely 2FA/captcha)"

        page.wait_for_timeout(3000)  # client-rendered config form needs a beat
        result = _fill_chat_app_form(page, project, timeout)
        browser.close()
        return result


def _save_chat_diagnostics(page, project: str, tag: str) -> None:
    try:
        page.screenshot(path=f"/tmp/chat-app-config-{project}-{tag}.png")
        with open(f"/tmp/chat-app-config-{project}-{tag}.txt",
                 "w", encoding="utf-8") as fh:
            fh.write(f"URL: {page.url}\n\n{page.inner_text('body')[:3000]}")
    except Exception:      # noqa: BLE001 - diagnostics only
        pass


def _fill_chat_app_form(page, project: str, timeout: int) -> tuple[bool, str]:
    """Best-effort, same shape as _accept_cloud_console_tos: types a
    default app name, sets status to LIVE if that control is found, and
    saves -- deliberately does NOT touch "Connection settings" (Pub/Sub
    topic, App URL, Apps Script). This project's own use of Chat
    (seed_chat(), chat_engine.py) only ever calls the API as itself, never
    receives interactive events, so a live status app with no connection
    configured is enough to stop the 404 -- and guessing at
    infrastructure-specific connection fields that do not exist yet risks
    leaving the form in an error state that blocks Save entirely.
    """
    _save_chat_diagnostics(page, project, "before-fill")

    name_box = None
    for sel in _CHAT_NAME_SEL:
        loc = page.locator(sel)
        if loc.count() > 0 and loc.first.is_visible():
            name_box = loc.first
            break

    if name_box is None:
        _save_chat_diagnostics(page, project, "no-name-field")
        return False, "could not find the app name field -- console may have changed"

    try:
        existing_name = (name_box.input_value() or "").strip()
        if not existing_name:
            name_box.click()
            name_box.type(f"{project} sandbox", delay=30)
    except Exception as exc:      # noqa: BLE001
        _save_chat_diagnostics(page, project, "name-fill-failed")
        return False, f"could not set the app name: {exc}"

    for label in _CHAT_STATUS_LABELS:
        control = page.get_by_text(label, exact=True)
        if control.count() > 0 and control.first.is_visible():
            try:
                control.first.click()
            except Exception:      # noqa: BLE001 - status may already be set
                pass
            break

    saved = False
    for label in _CHAT_SAVE_LABELS:
        btn = page.get_by_role("button", name=label)
        if btn.count() > 0 and btn.first.is_visible() and btn.first.is_enabled():
            try:
                btn.first.click()
                page.wait_for_timeout(2500)
                saved = True
            except Exception:      # noqa: BLE001 - try the next label
                continue
            break

    _save_chat_diagnostics(page, project, "after-save" if saved else "no-save-button")
    if not saved:
        return False, "filled the form but could not find/click Save"
    return True, f"configured a Chat app for {project}"
