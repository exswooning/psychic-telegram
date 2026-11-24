"""Ask the API for a delta for one account -- run from cron (hourly for dharan.gov.np -> new.mdharan.com, operator,
24 Nov 2026). The request goes through POST /api/v2/migrate/delta like a button press, so every gate, audit row and
queue rule applies; a busy account refuses it and the next hour tries again.

    python hourly_delta.py --account-id 7 --services drive --days 1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--services", default="drive")
    ap.add_argument("--days", type=int, default=1)
    a = ap.parse_args(argv)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    import job_admission
    import job_queue
    # One at a time: while the box is busy, each hour would otherwise queue another delta behind the first.
    if any(r.get("account_id") == a.account_id and r.get("job_name") == "delta" for r in job_admission.list_active()) \
            or any(r["job_name"] == "delta" for r in job_queue.waiting(a.account_id)):
        print(stamp, "skipped: a delta for this account is already running or queued", flush=True)
        return 0
    import campaign
    d = campaign.LiveDriver(a.account_id, "hourly-delta")
    body = {"account_id": a.account_id, "services": a.services.split(","), "days": a.days,
            "reason": f"hourly delta ({a.services}, last {a.days} day(s)), scheduled by cron"}
    try:
        print(stamp, json.dumps(d.api("POST", "/api/v2/migrate/delta", body)), flush=True)
    except urllib.error.HTTPError as exc:      # busy or refused: said, and the next hour tries again
        print(stamp, "refused", exc.code, exc.read()[:300].decode("utf-8", "replace"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
