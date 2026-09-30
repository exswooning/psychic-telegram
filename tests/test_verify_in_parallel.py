"""'Verify everything' on 300 users ran one user at a time and saved nothing until
the end -- two such runs sat for half an hour with an empty page and a log that
looked frozen. Users now run side by side and each is saved as it finishes."""
from __future__ import annotations

import threading
import time
import types

import verify_sample as V


class _FakeVerifier:
    live = [0]
    peak = [0]
    lock = threading.Lock()

    def __init__(self, auth, db, settings, u, t, retry=None, limit=None):
        self.evidence = [{"service": "drive"}]

    def drive(self):
        with self.lock:
            self.live[0] += 1
            self.peak[0] = max(self.peak[0], self.live[0])
        time.sleep(0.05)
        with self.lock:
            self.live[0] -= 1
        return {"checked": 1, "identical": 1, "differences": [], "missing": [], "duplicates": [],
                "extras": [], "notCopied": [], "errors": [], "notes": []}


class _DB:
    def all_identities(self):
        return [{"entity_type": "user", "source_email": f"u{i}@s", "target_email": f"u{i}@t"}
                for i in range(6)]


def test_users_run_side_by_side_and_each_is_handed_over_when_done(monkeypatch):
    monkeypatch.setattr(V, "Verifier", _FakeVerifier)
    monkeypatch.setattr(V.Verifier, "_blank", staticmethod(lambda: {}), raising=False)
    monkeypatch.setenv("VERIFY_WORKERS", "3")
    _FakeVerifier.peak[0] = 0
    seen = []
    settings = types.SimpleNamespace(source_domain="s", target_domain="t")
    report = V.run(None, _DB(), settings, services=("drive",),
                   on_user=lambda u, per: seen.append(u))
    assert _FakeVerifier.peak[0] == 3
    assert sorted(seen) == [f"u{i}@s" for i in range(6)]
    assert list(report["users"]) == [f"u{i}@s" for i in range(6)]      # chosen order kept
    assert report["totals"]["checked"] == 6


def test_a_verification_that_outlives_a_restart_can_be_stopped(monkeypatch):
    import webui
    ps = "  777  90 /root/migration/.venv/bin/python verify_sample.py --account-id 3\n"
    monkeypatch.setattr(webui.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=ps))
    assert [(j["pid"], j["name"]) for j in webui._external_processes()] == [(777, "verify")]
