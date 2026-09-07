"""Groups are a tenant's permission model, and nothing created one.

Every Drive ACL naming a group, every SSO assignment targeting one, and
every distribution list depends on the group existing on the far side. sso.py
already resolves an assignment's target group by email and skips the
assignment when it is missing -- so the gap was visible from inside the
codebase before anything migrated a group.
"""
import groups_engine


class _DB:
    def __init__(self, identities=None):
        self.identities = identities or {}
        self.mappings, self.audits = [], []
    def resolve_identity(self, e): return self.identities.get((e or "").lower())
    def record_mapping(self, u, s, t, ty): self.mappings.append((s, t, ty))
    def log_audit(self, u, i, ty, status, msg=""): self.audits.append((i, ty, status, msg))
    def statuses(self): return [a[2] for a in self.audits]


class _Svc:
    def __init__(self, groups=None, members=None):
        self._groups = groups or {}
        self._members = members or {}
        self.created, self.added = [], []
    def groups(self): return self
    def members(self): return self
    def list(self, customer=None, groupKey=None, maxResults=None, pageToken=None):
        if customer is not None:
            data = {"groups": self._groups.get("list", [])}
        else:
            data = {"members": self._members.get(groupKey, [])}
        return type("R", (), {"execute": lambda s=None: data})()
    def insert(self, body=None, groupKey=None):
        (self.added if groupKey else self.created).append((groupKey, body))
        return type("R", (), {"execute": lambda s=None: {}})()


def _mig(settings, db, src, tgt):
    m = object.__new__(groups_engine.GroupMigrator)
    m.settings = settings
    m.db = db
    m.stats = {"groups": 0, "members": 0, "skipped": 0,
               "members_skipped": 0, "failed": 0}
    m.auth = type("A", (), {"directory": lambda s=None, tenant=None, groups=False:
                            (src if tenant == "source" else tgt)})()
    return m


class TestGroupsAreRecreatedOnTheTargetDomain:
    def test_the_localpart_survives_and_the_domain_changes(self, settings):
        settings.target_domain = "target.test"
        m = _mig(settings, _DB(), _Svc(), _Svc())
        assert m.target_email("eng@source.test") == "eng@target.test"

    def test_a_group_is_created_with_its_name(self, settings):
        settings.target_domain = "target.test"
        settings.dry_run = False
        src = _Svc(groups={"list": [{"email": "eng@source.test", "name": "Engineering",
                                     "description": "the team"}]},
                   members={"eng@source.test": []})
        tgt = _Svc(groups={"list": []}, members={"eng@target.test": []})
        m = _mig(settings, _DB(), src, tgt)
        m.migrate()
        assert tgt.created, "no group was created"
        _, body = tgt.created[0]
        assert body["email"] == "eng@target.test"
        assert body["name"] == "Engineering" and body["description"] == "the team"

    def test_an_existing_group_is_not_created_twice(self, settings):
        """A resumed run must not leave two groups with one purpose."""
        settings.target_domain = "target.test"
        settings.dry_run = False
        src = _Svc(groups={"list": [{"email": "eng@source.test"}]},
                   members={"eng@source.test": []})
        tgt = _Svc(groups={"list": [{"email": "eng@target.test"}]},
                   members={"eng@target.test": []})
        m = _mig(settings, _DB(), src, tgt)
        stats = m.migrate()
        assert tgt.created == [] and stats["skipped"] == 1


class TestMembership:
    def _run(self, settings, members, identities):
        settings.target_domain = "target.test"
        settings.dry_run = False
        src = _Svc(groups={"list": [{"email": "eng@source.test"}]},
                   members={"eng@source.test": members})
        tgt = _Svc(groups={"list": []}, members={"eng@target.test": []})
        db = _DB(identities)
        m = _mig(settings, db, src, tgt)
        m.migrate()
        return m, tgt, db

    def test_members_are_remapped_through_the_identity_map(self, settings):
        m, tgt, _ = self._run(
            settings,
            [{"email": "a@source.test", "role": "MEMBER", "type": "USER"}],
            {"a@source.test": "a@target.test"})
        assert tgt.added, "no member added"
        assert tgt.added[0][1]["email"] == "a@target.test"

    def test_the_role_is_preserved(self, settings):
        """An owner demoted to member is a permission change nobody asked
        for."""
        m, tgt, _ = self._run(
            settings,
            [{"email": "a@source.test", "role": "OWNER", "type": "USER"}],
            {"a@source.test": "a@target.test"})
        assert tgt.added[0][1]["role"] == "OWNER"

    def test_an_unmapped_member_is_skipped_not_invented(self, settings):
        """A group that silently gains an unmapped address is a data leak
        with a plausible explanation."""
        m, tgt, db = self._run(
            settings,
            [{"email": "stranger@elsewhere.test", "role": "MEMBER", "type": "USER"}],
            {})
        assert tgt.added == []
        assert "SKIPPED_UNMAPPED_IDENTITY" in db.statuses()

    def test_a_nested_group_is_remapped_by_localpart(self, settings):
        """The identity map holds people, not groups."""
        m, tgt, _ = self._run(
            settings,
            [{"email": "leads@source.test", "role": "MEMBER", "type": "GROUP"}],
            {})
        assert tgt.added[0][1]["email"] == "leads@target.test"


class TestDryRun:
    def test_nothing_is_written(self, settings):
        settings.target_domain = "target.test"
        settings.dry_run = True
        src = _Svc(groups={"list": [{"email": "eng@source.test"}]},
                   members={"eng@source.test": []})
        tgt = _Svc(groups={"list": []}, members={"eng@target.test": []})
        m = _mig(settings, _DB(), src, tgt)
        stats = m.migrate()
        assert tgt.created == [] and stats["groups"] == 1


class TestTheLedgerCanBeReset:
    def test_group_types_are_resettable(self):
        """A type no reset clears means a re-run finds every group already
        mapped and creates none."""
        import reset_drive_ledger as r
        assert "group" in r.SERVICE_TYPES["groups"]
        assert "group_member" in r.SERVICE_TYPES["groups"]


class TestTheScopeReachesTheGrant:
    """A scope nobody can grant is a feature nobody can run.

    GROUP_WRITE_SCOPE was added inside auth.directory() and never reached
    source_scopes/target_scopes, so it never appeared in the line an admin
    pastes into Admin Console. Reading groups worked -- group.readonly is in
    the base source scopes -- so the inventory looked healthy while every
    write failed at token-mint with `unauthorized_client`, an error that
    names no scope and points at no file.
    """

    def test_target_scopes_ask_for_it_when_groups_are_on(self):
        from config import GROUP_WRITE_SCOPE, Settings, target_scopes
        s = Settings()
        s.migrate_groups = True
        assert GROUP_WRITE_SCOPE in target_scopes(s)

    def test_and_not_when_they_are_off(self):
        """The source credential stays read-only by construction; the target
        should not carry a write scope it is not using either."""
        from config import GROUP_WRITE_SCOPE, Settings, target_scopes
        s = Settings()
        s.migrate_groups = False
        assert GROUP_WRITE_SCOPE not in target_scopes(s)

    def test_the_dwd_line_an_admin_pastes_includes_it(self):
        """The union is the whole point: one line pasted once, covering
        features turned on later. A scope missing from it is a scope that
        gets granted only after somebody debugs a token-mint failure."""
        import webui
        from config import GROUP_WRITE_SCOPE
        assert GROUP_WRITE_SCOPE in webui.dwd_payload()["migrate_target_full"]
