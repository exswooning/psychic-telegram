"""
tests/test_seed_can_actually_make_groups.py
===========================================
seed_scopes_payload has advertised "Groups, members and group-typed Drive
ACLs" as a capability of this seeder, with its scope, since before the
endpoint could pass the flag. So the page said groups were possible and no
seed started from it could create one.

Live consequence: a freshly seeded 200-user tenant had zero groups -- and
therefore no group-typed Drive ACLs either, because the corpus skips group
shares when the list is empty rather than granting to an address that does
not exist. groups_engine.py stayed untestable end to end.
"""

from __future__ import annotations

import pytest

import webui


@pytest.fixture
def body():
    from config import Settings

    st = Settings()
    return {"confirm_domain": st.source_domain, "scale": "small"}


def _argv(body, **extra):
    argv, _env, err = webui.seed_argv(dict(body, **extra))
    assert not err, err
    return argv


class TestTheFlagIsReachable:
    def test_asking_for_groups_passes_the_flag(self, body):
        assert "--groups" in _argv(body, groups=True)

    def test_not_asking_does_not(self, body):
        """Off by default: it is a tenant-level write the other seeding
        scopes do not cover."""
        assert "--groups" not in _argv(body)

    def test_a_falsey_value_does_not_smuggle_it_in(self, body):
        for falsey in (False, None, "", 0):
            assert "--groups" not in _argv(body, groups=falsey), falsey


class TestItMatchesWhatIsAdvertised:
    def test_the_advertised_flag_is_the_one_sent(self):
        """The capability table names the flag it needs. If either side is
        renamed, the page goes back to advertising something unreachable --
        which is the whole bug."""
        caps = webui.seed_scopes_payload()["capabilities"]
        flags = {c["flag"] for c in caps}
        assert "--groups" in flags
        st_body = {"confirm_domain": webui.seed_scopes_payload()["domain"],
                   "scale": "small"}
        argv, _env, err = webui.seed_argv(dict(st_body, groups=True))
        assert not err, err
        assert "--groups" in argv

    def test_the_seeder_actually_accepts_it(self):
        """Nothing above proves seed_sandbox.py takes this argument -- and a
        flag it rejects is argparse exiting 2 before a single user is
        seeded. Read by path rather than imported: importing the seeder
        pulls in the whole Google client stack for one question."""
        import ast
        import os

        here = os.path.dirname(os.path.abspath(webui.__file__))
        path = os.path.join(here, "data-generator", "seed_sandbox.py")
        assert os.path.isfile(path), path
        tree = ast.parse(open(path, encoding="utf-8").read())
        flags = {a.value for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "add_argument"
                 for a in n.args if isinstance(a, ast.Constant)}
        assert "--groups" in flags, sorted(f for f in flags if isinstance(f, str))


class TestTheRunIsNotRefusedWithoutTheScope:
    def test_the_flag_is_not_gated_on_a_grant(self, body):
        """seed_sandbox prints why it skipped groups and carries on with the
        rest of the seed. An endpoint refusing the whole run over one
        optional feature would be a worse answer, and the page already shows
        whether the grant is live next to the checkbox."""
        argv = _argv(body, groups=True)
        assert "--groups" in argv
