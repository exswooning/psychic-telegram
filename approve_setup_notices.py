"""Approve a running setup's Google review notices for ONE named run, on the operator's command.

    python approve_setup_notices.py --account-id 5 --side source --domain a.sarafgloabalexim.com

Setup stops at Google's own first-use screens (the Cloud SDK's access review, the Cloud Platform terms)
and waits for a person; unanswered, it gives up after ten minutes. This watches that run's progress file
and answers each notice with `setup_notices.write_decision` -- the same per-notice, request-and-hash-bound
approval the UI's button writes, never a saved blanket acceptance. It approves only the categories named,
only for an admin on --domain, never for a government domain, and logs every decision. It stops when the
run ends, or after --wait seconds with nothing to answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import domain_guard
import setup_notices

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CATEGORIES = ("cloud_sdk_access", "cloud_console_terms")


def progress_path(account_id: int, side: str) -> str:
    return os.path.join(HERE, "logs", str(account_id), f"full-setup-{side}.json.progress")


def step(path: str, domain: str, categories, answered: set, log) -> str | None:
    """Answer the notice on screen if it is ours to answer. Returns what it did, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            handoff = (json.load(fh) or {}).get("handoff") or {}
    except (OSError, ValueError):
        return None
    if handoff.get("kind") != "review":
        return None
    rid, nhash, admin = handoff.get("requestId"), handoff.get("noticeHash"), handoff.get("adminEmail") or ""
    if not rid or rid in answered:
        return None
    if admin.rsplit("@", 1)[-1].lower() != domain.lower():
        log(f"not approving: the admin {admin} is not on {domain}")
        answered.add(rid)
        return "refused"
    if handoff.get("category") not in categories:
        log(f"not approving {handoff.get('category')!r}: not in {sorted(categories)}")
        answered.add(rid)
        return "refused"
    try:
        setup_notices.write_decision(path, rid, nhash, "approve")
    except (ValueError, OSError) as exc:
        log(f"could not approve {handoff.get('category')}: {exc}")
        return None
    answered.add(rid)
    log(f"approved {handoff.get('category')} for {admin}")
    return "approved"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--side", choices=("source", "target"), required=True)
    ap.add_argument("--domain", required=True, help="the tenant the notices must belong to")
    ap.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    ap.add_argument("--progress", help="the progress file to watch (default: that side's full setup; "
                                       "a seed key's is logs/<id>/seed-key.json.progress)")
    ap.add_argument("--wait", type=int, default=1500, help="stop after this many seconds with nothing to answer")
    args = ap.parse_args(argv)
    if domain_guard.is_government(args.domain):
        print(f"refusing: {args.domain} is a government domain; its Google steps are a person's", file=sys.stderr)
        return 2
    cats, answered, path = set(args.categories.split(",")), set(), args.progress or progress_path(args.account_id, args.side)
    log = lambda m: print(f"{time.strftime('%H:%M:%S', time.gmtime())} {m}", flush=True)  # noqa: E731
    idle_since = time.time()
    while time.time() - idle_since < args.wait:
        if step(path, args.domain, cats, answered, log) == "approved":
            idle_since = time.time()
        try:
            if (json.load(open(path)).get("pct") or 0) >= 100:
                log("setup finished")
                return 0
        except (OSError, ValueError):
            pass
        time.sleep(2)
    log(f"nothing to answer for {args.wait} s; stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
