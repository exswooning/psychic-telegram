"""campaign_nightly.py -- the nightly campaign run and its one line for the morning (fableplan2 Phase 6).

    python campaign_nightly.py --account-id ID [--name nightly] [campaign.py's other options]

Runs campaign.py with those options, then appends ONE line to monitor/INBOX.md: what this night's run
found, the ids that failed, and how many scenarios still have no passing result. The campaign itself
keeps its rules: it never wipes a tenant, skips the scenarios that need an operator's permission
(`--allow` is never passed here) and runs only what has not passed yet unless `--redo` is given.
Started by systemd/bitport-campaign-nightly@.timer. Exits 0 whenever the campaign ran: a FAIL is a result
for the morning, not a crash of the unit.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import time

import campaign
import campaign_scenarios as S

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(HERE, "monitor", "INBOX.md")


def _lines(name: str) -> int:
    try:
        with open(os.path.join(campaign.results_dir(name), "results.jsonl"), encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except FileNotFoundError:
        return 0


def summary_line(name: str, account_id: int, before: int, rc: int, now: float | None = None) -> str:
    """One line: the results this run appended, and what is still not passing overall."""
    try:
        with open(os.path.join(campaign.results_dir(name), "results.jsonl"), encoding="utf-8") as fh:
            new = [json.loads(l) for l in fh.read().splitlines()[before:] if l.strip()]
    except FileNotFoundError:
        new = []
    counts = collections.Counter(r["status"] for r in new)
    failed = sorted(r["id"] for r in new if r["status"] == "FAIL")
    left = sum(1 for s in S.all_scenarios() if campaign.read_results(name).get(s.id, {}).get("status") not in campaign.PASSING)
    ran = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing to run"
    return (f"- {time.strftime('%Y-%m-%d', time.gmtime(now))} NIGHTLY CAMPAIGN {name} (account {account_id}): {ran}"
            + (f"; FAILED {', '.join(failed)}" if failed else "")
            + (f"; the campaign exited {rc}" if rc else "")
            + f"; {left} scenario(s) not yet passing\n")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    account_id = int(argv[argv.index("--account-id") + 1])
    name = argv[argv.index("--name") + 1] if "--name" in argv else "nightly"
    if "--name" not in argv:
        argv += ["--name", name]
    before = _lines(name)
    try:
        rc = campaign.main(argv)
    except SystemExit as exc:          # argparse refusing an option
        rc = int(exc.code or 0)
    except Exception:                  # noqa: BLE001 - the morning must still hear that the night failed
        import traceback
        traceback.print_exc()
        rc = 1
    os.makedirs(os.path.dirname(INBOX), exist_ok=True)
    with open(INBOX, "a", encoding="utf-8") as fh:
        fh.write(summary_line(name, account_id, before, rc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
