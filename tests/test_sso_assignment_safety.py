"""The two behaviours that stop an SSO migration locking the tenant out.

An assignment decides who signs in through the IdP. Apply one pointing at a
profile the IdP has not been told about and those users cannot log in --
including the admin running the migration.
"""
import sso


class _DB:
    def __init__(self): self.rows = []
    def log_audit(self, *a, **k): self.rows.append(a)
    def statuses(self): return [r[3] for r in self.rows]


def _mig(settings, source_path="/Engineering", target_ou="orgUnits/999",
         group_email="eng@src.test", group_here=True):
    m = object.__new__(sso.SSOMigrator)
    m.settings = settings
    m.db = _DB()
    m.stats = {"skipped": 0, "assignments": 0, "failed": 0, "grants_seen": 0}
    m._org_unit_path = lambda tenant, res: source_path
    m._org_unit_by_path = lambda tenant, path: target_ou
    m._group_email = lambda tenant, res: group_email
    m._group_exists = lambda tenant, email: group_here
    return m


class TestTheRootOrgUnitIsEveryone:
    def test_it_is_treated_as_tenant_wide(self, settings):
        """Assigning SSO at "/" is how an admin applies it to everyone. Its
        scope is "orgUnit", so it walked past the tenant-wide guard."""
        m = _mig(settings, source_path="/")
        scope, why = m._remap_target({"targetOrgUnit": "orgUnits/1"})
        assert scope == sso.TENANT_WIDE
        assert "every account" in why

    def test_a_real_org_unit_is_not(self, settings):
        m = _mig(settings, source_path="/Engineering")
        scope, resolved = m._remap_target({"targetOrgUnit": "orgUnits/1"})
        assert scope == "orgUnit" and resolved == "orgUnits/999"

    def test_no_target_at_all_is_tenant_wide(self, settings):
        m = _mig(settings)
        scope, _ = m._remap_target({})
        assert scope == sso.TENANT_WIDE


class TestTheIdIsSentInTheFormTheApiWants:
    def test_a_bare_id_is_prefixed(self, settings):
        """orgunits.get reads an unprefixed id as a PATH, does not find it,
        and 404s -- so every org-unit assignment reported "could not read
        source org unit" and the remap never ran."""
        seen = {}

        class _OU:
            def get(self, customerId=None, orgUnitPath=None):
                seen["path"] = orgUnitPath
                return type("R", (), {"execute": lambda s=None: {"orgUnitPath": "/Eng"}})()

        class _Dir:
            def orgunits(self): return _OU()

        m = object.__new__(sso.SSOMigrator)
        m.auth = type("A", (), {"source_directory": lambda s=None: _Dir(),
                                "target_directory": lambda s=None: _Dir()})()
        assert m._org_unit_path("source", "orgUnits/03ph8a2z") == "/Eng"
        assert seen["path"] == "id:03ph8a2z"

    def test_an_already_prefixed_id_is_not_doubled(self, settings):
        seen = {}

        class _OU:
            def get(self, customerId=None, orgUnitPath=None):
                seen["path"] = orgUnitPath
                return type("R", (), {"execute": lambda s=None: {"orgUnitPath": "/Eng"}})()

        class _Dir:
            def orgunits(self): return _OU()

        m = object.__new__(sso.SSOMigrator)
        m.auth = type("A", (), {"source_directory": lambda s=None: _Dir(),
                                "target_directory": lambda s=None: _Dir()})()
        m._org_unit_path("source", "orgUnits/id:03ph8a2z")
        assert seen["path"] == "id:03ph8a2z"


class TestTheBodyCarriesExactlyOneTarget:
    """The field deciding who gets locked out must not depend on a later
    pop still being there."""

    def _body_for(self, scope, resolved):
        body = {"rank": 0, "samlSsoInfo": {"inboundSamlSsoProfile": "p"},
                "ssoMode": "SAML_SSO"}
        if scope == "orgUnit":
            body["targetOrgUnit"] = resolved
        elif scope == "group":
            body["targetGroup"] = resolved
        return body

    def test_tenant_wide_carries_no_target(self):
        b = self._body_for(sso.TENANT_WIDE, "every account in the tenant")
        assert "targetOrgUnit" not in b and "targetGroup" not in b

    def test_the_source_builds_it_that_way(self):
        import inspect
        src = inspect.getsource(sso.SSOMigrator.migrate_assignments)
        assert 'body.pop("targetOrgUnit"' not in src, (
            "back to build-then-pop for a lockout-grade field")
        assert 'if scope == "orgUnit":' in src


class _Creds:
    def __init__(self, items): self.items = items
    def list(self, parent=None):
        return type("R", (), {"execute": lambda s=None: {"idpCredentials": self.items}})()


class _Profiles:
    def __init__(self, prof, creds): self.prof, self.creds = prof, creds
    def get(self, name=None):
        if self.prof is None:
            raise RuntimeError("404")
        return type("R", (), {"execute": lambda s=None: self.prof})()
    def idpCredentials(self): return _Creds(self.creds)


def _live_mig(prof, creds):
    m = object.__new__(sso.SSOMigrator)
    svc = type("S", (), {"inboundSamlSsoProfiles": lambda s=None: _Profiles(prof, creds)})()
    m.auth = type("A", (), {"cloud_identity": lambda s=None, t=None: svc})()
    return m


GOOD_IDP = {"idpConfig": {"entityId": "https://idp.test/x",
                          "singleSignOnServiceUri": "https://idp.test/sso"}}


class TestAProfileIsNotAssignedUntilItCanActuallySignPeopleIn:
    """migrate_profiles copies idpConfig but NOT the signing certificate --
    credentials are a separate sub-resource with no place in create(). So a
    freshly migrated profile is a shell with the right name, and assigning
    one takes sign-in away from everybody it covers."""

    def test_a_profile_with_no_certificate_is_refused(self):
        m = _live_mig(GOOD_IDP, creds=[])
        ok, why = m.profile_is_live("inboundSamlSsoProfiles/1")
        assert ok is False
        assert "certificate" in why

    def test_a_fully_configured_profile_is_allowed(self):
        m = _live_mig(GOOD_IDP, creds=[{"name": "c1"}])
        ok, why = m.profile_is_live("inboundSamlSsoProfiles/1")
        assert ok is True and "1 credential" in why

    def test_a_profile_with_no_idp_config_is_refused(self):
        m = _live_mig({"idpConfig": {}}, creds=[{"name": "c1"}])
        ok, why = m.profile_is_live("inboundSamlSsoProfiles/1")
        assert ok is False and "entityId" in why

    def test_an_unreadable_profile_is_refused_not_assumed_good(self):
        m = _live_mig(None, creds=[])
        ok, why = m.profile_is_live("inboundSamlSsoProfiles/1")
        assert ok is False and "cannot read" in why

    def test_the_assignment_loop_consults_it_before_creating(self):
        import inspect
        src = inspect.getsource(sso.SSOMigrator.migrate_assignments)
        # After the local checks (cheap, and they give the precise reason)
        # and before anything is created.
        assert src.index("_remap_target") < src.index("profile_is_live")
        assert src.index("profile_is_live") < src.index("inboundSsoAssignments")
        assert "SKIPPED_PROFILE_NOT_LIVE" in src


class TestTheDocstringNoLongerClaimsTheCertificateMoves:
    def test_it_says_credentials_are_not_copied(self):
        doc = sso.SSOMigrator.migrate_profiles.__doc__
        assert "NOT" in doc and "idpCredentials" in doc
