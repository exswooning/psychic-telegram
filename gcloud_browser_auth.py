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
import tempfile
import time


def log(msg: str) -> None:
    print(f"[gcloud-auth] {msg}", flush=True)


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

    proc = subprocess.Popen(
        ["gcloud", "auth", "login", "--quiet"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
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

        typed_email = typed_pw = False
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
        browser.close()
