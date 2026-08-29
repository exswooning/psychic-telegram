"""Open the authorization link extracted from the email and grant it.

read_auth_mail.py pulled the 'View authorization request' URL out of the
Gmail message over the API and saved it to dms_auth_url.txt. This opens it
in the browser already signed in as the source super admin (open_console),
lands on Google's authorization page, and clicks the grant control. The
consent page is a plain Google page, not Gmail's onboarding-blocked UI, so
this part is reliable.
"""
import re
import sys

sys.path.insert(0, "/root/migration")
import dms_migrate as D

URL_FILE = "/root/migration/dms_auth_url.txt"
GRANT = ("Authorize", "Approve", "Grant", "Allow", "Accept", "Confirm",
         "Continue", "Agree")


def main():
    url = open(URL_FILE).read().strip()
    print("opening:", url[:70], "...")
    opened = D.open_console(False, 220)
    if opened is None:
        print("sign-in failed"); return 1
    p, browser, page = opened
    try:
        page.set_viewport_size({"width": 1500, "height": 1100})
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        print("landed on:", page.url)

        # the c.gle redirect drops us on an account chooser; pick the admin
        import os
        admin = os.environ.get("DWD_EMAIL", "")
        for _ in range(3):
            if "accountchooser" in page.url or "Choose an account" in \
                    page.inner_text("body"):
                acct = page.get_by_text(admin, exact=False)
                if acct.count():
                    acct.first.click()
                    page.wait_for_timeout(8000)
                    print("picked account ->", page.url)
                else:
                    break
            else:
                break

        # if a consent id is in the resolved URL, go straight to the page
        m = re.search(r"consents%23consents%2F(\w+)|consents/(\w+)", page.url)
        if m:
            cid = m.group(1) or m.group(2)
            direct = f"https://admin.google.com/ac/migrate/consents#consents/{cid}"
            if page.url.split("#")[0] != direct.split("#")[0]:
                page.goto(direct, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(8000)
                print("consent page ->", page.url)

        page.screenshot(path="/root/migration/dms_grant_before.png",
                        full_page=True)
        body = re.sub(r"\s+", " ", page.inner_text("body"))
        print("page text:", body[:400])
        print("buttons:", [ (b.inner_text() or "").strip()[:30]
                            for b in page.locator("button").all()[:25]
                            if (b.inner_text() or "").strip() ])

        clicked = None
        for lbl in GRANT:
            btn = page.get_by_role("button", name=re.compile(f"^{lbl}$", re.I))
            if btn.count() and btn.first.is_enabled():
                btn.first.click()
                clicked = lbl
                break
        if clicked is None:
            print("NO grant button found -- see dms_grant_before.png")
            return 2
        page.wait_for_timeout(6000)
        page.screenshot(path="/root/migration/dms_grant_after.png",
                        full_page=True)
        after = re.sub(r"\s+", " ", page.inner_text("body"))
        print(f"clicked {clicked!r}; after:", after[:300])
        print("OK")
        return 0
    finally:
        browser.close(); p.stop()


if __name__ == "__main__":
    raise SystemExit(main())
