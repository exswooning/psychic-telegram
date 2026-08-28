"""Let an operator push the seed harder, without letting them OOM it.

seed_sandbox sizes its pool from resources.recommend(), which budgets
memory per worker -- on the live box: "memory-bound: 1.7 GB budget usable /
101 MB per seed worker = 16". That default is right, and deliberately
conservative, so an override is worth having for a huge run.

It is capped because the failure mode is nasty and late: too many workers
does not fail fast, it gets the seed OOM-killed hours in, after it has
already written a partial corpus. Seeding slowly beats seeding twice.

The ceiling is derived from the machine, never hardcoded -- a fixed number
goes stale the next time the box changes, next to a comment explaining a
budget it no longer matches.
"""
import pytest

import webui


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    import config
    real = config.Settings

    def fake(account_id=None, **kw):
        st = real.__new__(real)
        st.source_domain, st.target_domain = "src.example", "tgt.example"
        st.source_admin, st.source_sa_key = "a@src.example", "/k/s.json"
        st.target_admin, st.target_sa_key = "a@tgt.example", "/k/t.json"
        st.db_path, st.account_id = "/tmp/m.db", account_id
        return st

    monkeypatch.setattr(config, "Settings", fake)
    monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)


def _argv(monkeypatch, workers, recommended=16):
    import resources
    monkeypatch.setattr(resources, "recommend",
                        lambda: {"seed_workers": recommended,
                                 "seed_reason": "memory-bound: 16"})
    return webui.seed_argv(
        {"confirm_domain": "src.example", "scale": "huge", "workers": workers}, 7)


class TestTheDefaultIsUntouched:
    def test_blank_lets_the_machine_decide(self, monkeypatch):
        argv, _, err = _argv(monkeypatch, "")
        assert not err
        assert "--workers" not in argv, "an empty box must not pin the pool"

    def test_none_does_too(self, monkeypatch):
        argv, _, err = _argv(monkeypatch, None)
        assert not err and "--workers" not in argv

    def test_zero_means_auto_not_zero_workers(self, monkeypatch):
        # seed_sandbox reads 0 as "size it yourself"; passing it through
        # would be harmless but pinning it is not what 0 means.
        argv, _, err = _argv(monkeypatch, 0)
        assert not err and "--workers" not in argv


class TestAnOverrideIsPassed:
    def test_a_sane_number_reaches_the_seeder(self, monkeypatch):
        argv, _, err = _argv(monkeypatch, 24)
        assert not err
        assert argv[argv.index("--workers") + 1] == "24"

    def test_a_string_is_accepted(self, monkeypatch):
        # It arrives from a text field.
        argv, _, err = _argv(monkeypatch, "24")
        assert not err and argv[argv.index("--workers") + 1] == "24"

    def test_exactly_twice_the_recommendation_is_allowed(self, monkeypatch):
        argv, _, err = _argv(monkeypatch, 32, recommended=16)
        assert not err, err


class TestTheCeilingHolds:
    def test_past_twice_is_refused(self, monkeypatch):
        _, _, err = _argv(monkeypatch, 33, recommended=16)
        assert "more than this machine can hold" in err

    def test_the_refusal_explains_the_cost(self, monkeypatch):
        # "Rejected" without a reason invites someone to just try again.
        _, _, err = _argv(monkeypatch, 200, recommended=16)
        assert "kills the seed" in err
        assert "16" in err and "32" in err

    def test_the_ceiling_follows_the_machine(self, monkeypatch):
        """A bigger box must allow more, with no code change."""
        assert not _argv(monkeypatch, 60, recommended=40)[2]
        assert _argv(monkeypatch, 60, recommended=8)[2]

    def test_nonsense_is_refused_clearly(self, monkeypatch):
        _, _, err = _argv(monkeypatch, "lots")
        assert "whole number" in err

    def test_negative_is_refused(self, monkeypatch):
        _, _, err = _argv(monkeypatch, -4)
        assert "at least 1" in err

    def test_a_broken_probe_does_not_block_the_seed(self, monkeypatch):
        """If resources cannot answer, the operator's number stands rather
        than the seed being refused for a reason nobody can act on."""
        import resources

        def boom():
            raise RuntimeError("no /proc")

        monkeypatch.setattr(resources, "recommend", boom)
        argv, _, err = webui.seed_argv(
            {"confirm_domain": "src.example", "scale": "huge", "workers": 99}, 7)
        assert not err and argv[argv.index("--workers") + 1] == "99"


class TestTheFormOffersIt:
    def test_the_field_exists(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "migration-webui/src/pages/SeedWizard.tsx"),
                   encoding="utf-8").read()
        assert "seed-form-workers" in src
        assert "blank = auto" in src

    def test_it_is_sent(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "migration-webui/src/api/client.ts"),
                   encoding="utf-8").read()
        assert "workers: workers || undefined" in src
