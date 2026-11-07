"""Trusted isolated harness: candidate Gmail quota code, sandbox status relay only."""

import json
import os
from pathlib import Path
import sys

import verify_gmail_quota as quota
import verify_sample


def sandbox_read(side, slot):
    print(json.dumps({"side": side, "slot": slot}), flush=True)
    reply = json.loads(sys.stdin.readline())
    if reply != {"status": 200}:
        raise RuntimeError("Sandbox metadata read was not successful")


class Batch:
    def __init__(self, side, callback):
        self.side, self.callback, self.requests = side, callback, []

    def add(self, request, request_id):
        self.requests.append((request, request_id))

    def execute(self):
        for slot, request_id in self.requests:
            sandbox_read(self.side, slot)
            self.callback(request_id, {"id": "sandbox-slot-" + str(slot)}, None)


class Gmail:
    def __init__(self, side):
        self.side = side

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **kwargs):
        assert kwargs["userId"] == "me" and kwargs["format"] == "metadata"
        return int(kwargs["id"])

    def new_batch_http_request(self, callback):
        return Batch(self.side, callback)


def main():
    v = verify_sample.Verifier(None, None, None, "sandbox-source", "sandbox-target")
    budgets = {}

    def budget(side):
        if side not in budgets:
            budgets[side] = quota.MailboxBudget(
                Path(os.environ["TMPDIR"]) / "sandbox-quota.db", side
            )
        return budgets[side]

    v._gmail_budget = budget
    for side in ("source", "target"):
        values, errors = v._raw_many(Gmail(side), ["0", "1"], side, fmt="metadata")
        if errors or set(values) != {0, 1}:
            raise RuntimeError("Candidate did not read both sandbox slots")
    print(json.dumps({"done": True}), flush=True)


if __name__ == "__main__":
    main()
