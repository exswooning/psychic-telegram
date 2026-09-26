"""Read and close the incidents the run watcher recorded.

    .venv/bin/python incidents.py list [--all]     open and acknowledged (or every one)
    .venv/bin/python incidents.py show 12          the brief for #12, ready to act on
    .venv/bin/python incidents.py ack 12           somebody is looking at it
    .venv/bin/python incidents.py resolve 12 -m "fixed in abc123"
    .venv/bin/python incidents.py feed [-n 40]     the append-only log of what the watcher saw

`show` prints exactly what the Incidents panel's "Copy brief" copies. Nothing
here fixes or deploys anything -- a deploy can kill a running seed or migration.
"""
from __future__ import annotations

import argparse
import sys

import run_watch as W


def _line(i: dict) -> str:
    return (f"#{i['id']:<4} {i['status']:<12} {i['severity']:<5} {i['kind']:<12} "
            f"acct {i['account_id'] if i['account_id'] is not None else '-':<4} "
            f"x{i['occurrences']:<3} {i['last_seen_at'][:19]}  {i['title']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true", help="include resolved")
    for name in ("show", "ack", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        if name == "resolve":
            p.add_argument("-m", "--note", default="")
    fd = sub.add_parser("feed")
    fd.add_argument("-n", type=int, default=40)
    a = ap.parse_args(argv)

    if a.cmd == "list":
        rows = [i for i in W.list_incidents() if a.all or i["status"] != "resolved"]
        print("\n".join(_line(i) for i in rows) or "No open incidents.")
        return 0
    if a.cmd == "feed":
        try:
            with open(W.feed_path(), encoding="utf-8") as fh:
                print("".join(fh.readlines()[-a.n:]), end="")
        except OSError:
            print("The feed is empty: the watcher has not recorded anything yet.")
        return 0
    inc = W.get_incident(a.id)
    if not inc:
        print(f"No incident #{a.id}.", file=sys.stderr)
        return 1
    if a.cmd == "show":
        text = W.read_brief(a.id)
        print(text if text is not None else f"{_line(inc)}\n\n{inc['summary']}\n\n(no brief was written)")
        return 0
    W.set_status(a.id, "acknowledged" if a.cmd == "ack" else "resolved", getattr(a, "note", "") or "")
    print(f"#{a.id} is now {'acknowledged' if a.cmd == 'ack' else 'resolved'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
