"""What the corpus could not express, and now can.

Every gap here was measured against the real tool, not imagined:

  groups            0 references in the seeder. So no group-typed Drive ACL
                    could exist, so the whole group-sharing path -- what a
                    real tenant's permissions are mostly made of -- had no
                    test data, and groups_engine.py could not run end to
                    end. The scope bug that made it unrunnable survived
                    exactly because nothing ever tried.
  cross-references  A Doc linking to a Sheet rots like a link in mail, and
                    nothing rewrites it. With no seeded example that was an
                    argument rather than a finding.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seed_sandbox as seed  # noqa: E402
from corpus import CorpusBuilder  # noqa: E402


class _Dir:
    """`member_fails` refuses the first N member inserts per group, which is
    what a just-created group does: members().insert 404s until it
    propagates."""
    def __init__(self, fail=False, member_fails=0):
        self.groups_made, self.members_made, self.fail = [], [], fail
        self.member_fails, self.seen = member_fails, {}
    def groups(self): return self
    def members(self): return self
    def insert(self, body=None, groupKey=None):
        if self.fail:
            raise RuntimeError("insufficient permission")
        if groupKey:
            n = self.seen.get(groupKey, 0)
            self.seen[groupKey] = n + 1
            if n < self.member_fails:
                raise RuntimeError("404 Resource Not Found: groupKey")
        (self.members_made if groupKey else self.groups_made).append(
            (groupKey, body))
        return type("R", (), {"execute": lambda s=None: {}})()


class TestGroupsAreSeeded:
    def test_it_creates_a_spread_of_group_shapes(self, tmp_path):
        d = _Dir()
        m = seed.seed_groups(d, seed.Settings(), ["a@x.test", "b@x.test",
                                                  "c@x.test"], "ext@e.test")
        names = [b["email"] for _, b in d.groups_made]
        assert m["groups"] == 4
        assert any("all-staff" in n for n in names)
        assert any("leads" in n for n in names)

    def test_an_external_member_is_included(self, tmp_path):
        """A group with an outside member is what makes the migration's
        'unmapped member is skipped, not invented' branch reachable."""
        d = _Dir()
        seed.seed_groups(d, seed.Settings(), ["a@x.test"], "ext@e.test")
        assert any(b["email"] == "ext@e.test" for _, b in d.members_made)

    def test_roles_are_not_all_the_same(self):
        """A migration that flattens OWNER to MEMBER is a permission change
        nobody asked for, and needs mixed roles to be visible."""
        d = _Dir()
        seed.seed_groups(d, seed.Settings(), ["a@x.test", "b@x.test"], "e@e.test")
        roles = {b.get("role") for _, b in d.members_made}
        assert "OWNER" in roles and "MEMBER" in roles

    def test_one_group_contains_another(self):
        """groups_engine remaps a GROUP member by localpart rather than
        through the identity map, which holds people. That branch had no
        data at all."""
        d = _Dir()
        m = seed.seed_groups(d, seed.Settings(), ["a@x.test", "b@x.test"], "e@e.test")
        assert m["nested"] == 1
        assert any((gk and "all-staff" in gk and "leads" in b["email"])
                   for gk, b in d.members_made)

    def test_a_refused_scope_is_reported_not_fatal(self):
        """Group creation is a tenant-level write the other seeding scopes
        do not cover; a seed must still produce everything else."""
        m = seed.seed_groups(_Dir(fail=True), seed.Settings(),
                             ["a@x.test"], "e@e.test")
        assert m["groups"] == 0 and m["note"]


class _Drive:
    def __init__(self): self.grants = []
    def permissions(self): return self
    def create(self, **kw):
        self.grants.append(kw.get("body"))
        return type("R", (), {"execute": lambda s=None: {"id": "p1"}})()

    def __call__(self, *a, **k):
        return None


def _builder(groups=None):
    b = object.__new__(CorpusBuilder)
    b.groups = list(groups or [])
    b.drive = _Drive()
    b._retry = staticmethod(lambda fn: fn)
    b.m = {"grants": {"group": 0, "user": 0, "domain": 0, "anyone": 0,
                      "external": 0}, "rejected": {}}
    import threading
    b._mlock = threading.Lock()
    return b


class TestGroupTypedAcls:
    def test_a_group_grant_names_the_group_not_a_person(self):
        b = _builder(["eng@x.test"])
        b.share_group("F1", "eng@x.test", "writer")
        assert b.drive.grants[0]["type"] == "group"
        assert b.drive.grants[0]["emailAddress"] == "eng@x.test"

    def test_no_groups_means_no_group_grants_attempted(self):
        """Granting to an address that does not exist is rejected by Drive
        and reads as a sharing bug rather than a missing prerequisite."""
        import inspect
        from corpus import CorpusBuilder as CB
        src = inspect.getsource(CB._plan_leaf)
        assert "and self.groups" in src


class TestCrossReferences:
    def test_the_corpus_builds_documents_that_link_to_other_files(self):
        import inspect
        from corpus import CorpusBuilder as CB
        src = inspect.getsource(CB)
        assert "_build_cross_references" in src
        body = inspect.getsource(CB._build_cross_references)
        assert "spreadsheets/d/" in body, "the link must name a real file id"
        assert "drive/folders/" in body, "and a folder, a second URL shape"

    def test_it_runs_on_every_user_not_only_edge_case_ones(self):
        """Edge cases are seeded for the first user only by default; link
        rot is not an edge case."""
        import inspect
        from corpus import CorpusBuilder as CB
        build = inspect.getsource(CB.build)
        assert build.index("self._build_cross_references") < build.index(
            "if edge_cases:")


class TestMemberFailuresAreNotSwallowed:
    """Live, `leads` and `partners` came out with zero members each and the
    seed reported success: a just-created group 404s on members().insert
    until it propagates, and the first version of this caught every
    exception and moved on. Four more failures inside `engineering` were
    invisible for the same reason."""

    def test_propagation_delay_is_retried_not_lost(self, monkeypatch):
        monkeypatch.setattr(seed.time, "sleep", lambda *_: None)
        d = _Dir(member_fails=2)
        m = seed.seed_groups(d, seed.Settings(), ["a@x.test"], "e@e.test")
        assert m["members"] > 0, "gave up on a group that was merely new"
        assert m["member_failures"] == 0

    def test_a_persistent_failure_is_counted_and_named(self, monkeypatch):
        monkeypatch.setattr(seed.time, "sleep", lambda *_: None)
        d = _Dir(member_fails=99)
        m = seed.seed_groups(d, seed.Settings(), ["a@x.test"], "e@e.test")
        assert m["member_failures"] > 0, "a silent zero-member group again"
        assert m["member_notes"], "no reason recorded for the operator"

    def test_an_already_present_member_is_not_a_failure(self, monkeypatch):
        class _Dup(_Dir):
            def insert(self, body=None, groupKey=None):
                if groupKey:
                    raise RuntimeError("409 duplicate: Member already exists")
                return super().insert(body=body, groupKey=groupKey)
        monkeypatch.setattr(seed.time, "sleep", lambda *_: None)
        m = seed.seed_groups(_Dup(), seed.Settings(), ["a@x.test"], "e@e.test")
        assert m["member_failures"] == 0
