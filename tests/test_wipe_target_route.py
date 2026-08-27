"""The target's provisioned accounts need a way out that isn't SSH.

reset_target empties the seeded data; the ~200 users provisioning created
stay. A rehearsal on top of them is not a rehearsal: provisioning skips
users that already exist, the copy lands on the previous one, and the
fidelity check compares a tenant against itself.

The gate is reset_target's, reused rather than restated -- one place that
knows which domain must be typed.
"""
import webui


def _cfg(monkeypatch, source="src.example", target="tgt.example"):
    import config
    real = config.Settings

    def fake(account_id=None, **kw):
        st = real.__new__(real)
        st.source_domain, st.target_domain = source, target
        st.target_admin, st.target_sa_key = "admin@" + target, "/k/t.json"
        st.db_path, st.account_id = "/tmp/m.db", account_id
        return st

    monkeypatch.setattr(config, "Settings", fake)


class TestTheDomainMustBeTyped:
    def test_no_domain_typed_is_refused(self, monkeypatch):
        _cfg(monkeypatch)
        assert "type the target domain" in webui.wipe_target_argv({}, 7)[2]

    def test_the_source_domain_is_refused(self, monkeypatch):
        # The one typo that would matter, and it names why.
        _cfg(monkeypatch)
        err = webui.wipe_target_argv({"confirm_domain": "src.example"}, 7)[2]
        assert "does not match the target domain" in err
        assert "SOURCE domain" in err

    def test_a_protected_domain_is_refused(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.setenv("PROTECTED_DOMAINS", "tgt.example")
        assert "PROTECTED_DOMAINS" in webui.wipe_target_argv(
            {"confirm_domain": "tgt.example"}, 7)[2]


class TestTheCommand:
    def test_it_runs_wipe_target_and_actually_applies(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        argv, env, err = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert not err
        assert "wipe_target.py" in argv[1]
        assert "--apply" in argv, "a button that only reports is a trap"
        assert argv[argv.index("--confirm-domain") + 1] == "tgt.example"
        assert argv[argv.index("--account-id") + 1] == "7"

    def test_it_never_names_the_source_domain(self, monkeypatch):
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        argv, _, _ = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert "src.example" not in " ".join(argv)

    def test_the_sandbox_flag_survives(self, monkeypatch):
        # wipe_target.py calls reset_target.assert_sandbox, which needs it.
        _cfg(monkeypatch)
        monkeypatch.delenv("PROTECTED_DOMAINS", raising=False)
        _, env, _ = webui.wipe_target_argv({"confirm_domain": "tgt.example"}, 7)
        assert env["SANDBOX_MODE"] == "true"


class TestTheRouteIsWired:
    def test_the_endpoint_exists_and_admits_a_job(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        block = src.split('if self.path == "/api/wipe_target":')[1][:900]
        assert "wipe_target_argv" in block
        assert "job_admission.try_admit" in block, "two at once corrupt the run"
        assert "_subscription_ok" in block
