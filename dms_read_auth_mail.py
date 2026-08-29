"""Read the Data Import authorization email over the Gmail API and print the
links it contains. Deterministic -- no Gmail UI, no onboarding modal.

The source SA already holds gmail.readonly via domain-wide delegation, so it
can impersonate the super admin and read their inbox directly.
"""
import base64
import re
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import Settings
from auth import AuthManager

ADMIN = "info@source.rohitrokaya.com.np"
st = Settings(account_id=66)
gmail = AuthManager(st).source_gmail(ADMIN)

res = gmail.users().messages().list(
    userId="me", q="data migration authorization OR connection request",
    maxResults=5).execute()
msgs = res.get("messages", [])
print("matching messages:", len(msgs))
if not msgs:
    sys.exit(0)


def walk(part, out):
    body = part.get("body", {})
    data = body.get("data")
    if data:
        out.append(base64.urlsafe_b64decode(data).decode("utf-8", "replace"))
    for p in part.get("parts", []) or []:
        walk(p, out)


full = gmail.users().messages().get(userId="me", id=msgs[0]["id"],
                                    format="full").execute()
hdrs = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
print("subject:", hdrs.get("Subject"))
print("from   :", hdrs.get("From"))
chunks = []
walk(full["payload"], chunks)
html = "\n".join(chunks)

# Anchors aren't plain href="..." here, so map each URL to the visible text
# that immediately follows it (the anchor label sits right after the href).
urls = list(dict.fromkeys(re.findall(r'https?://[^\s"\'<>)]+', html)))
print("\n=== url -> nearby text ===")
target_url = None
for u in urls:
    i = html.find(u)
    window = html[i:i + 400]
    label = re.sub(r"<[^>]+>", " ", window)
    label = re.sub(r"&nbsp;|&amp;|&#39;|&quot;|&rsquo;", " ", label)
    label = re.sub(r"\s+", " ", label).strip()
    # drop the url itself from the label text
    label = label.replace(u, "").strip()
    print(f"  {u[-24:]} -> {label[:60]!r}")
    if re.search(r"view authorization|authorization request|approve|"
                 r"grant|review request", label, re.I):
        target_url = u

# fall back to the last CTA, which in this template is the action link
if target_url is None and urls:
    target_url = urls[-1]
    print("  (fell back to the last url as the action link)")

print("\n>>> AUTHORIZATION LINK:", target_url or "(not identified)")
# persist it for the browser step
if target_url:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dms_auth_url.txt"), "w") as fh:
        fh.write(target_url)
    print(">>> saved to dms_auth_url.txt")
