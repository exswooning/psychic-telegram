"""
tests/test_check_seed.py
========================
The read-only preflight for seeding: can the seeder actually write?

It exists because the seed scopes and the migration scopes differ, and the
Admin Console's delegation editor *replaces* the scope line rather than
appending. Pasting the migration line leaves seeding failing with
unauthorized_client, which names neither cause. This answers the question
before a run rather than thirty minutes into one.
"""

from __future__ import annotations

import inspect

import pytest

import check_seed


class _FakeDirectorySvc:
    def __init__(self, present: set[str]):
        self.present = present

    def users(self):
        return self

    def get(self, userKey, fields=None):
        self._key = userKey
        return self

    def execute(self):
        if self._key not in self.present:
            raise RuntimeError(f"404: {self._key} not found")
        return {"primaryEmail": self._key}


class TestKeyResolution:
    """
    _build() used to authenticate every check with settings.source_sa_key --
    the production source key, read-only by design (config.SOURCE_SCOPES's
    own docstring). A scope check run against it was guaranteed to fail
    with unauthorized_client no matter what was granted to the real
    SEED_SA_KEY, because it was never testing that key at all.
    """

    def test_build_resolves_the_seed_key_not_the_source_key(self, monkeypatch):
        seen = {}

        class _FakeCreds:
            def with_subject(self, subject):
                return self

        def fake_from_file(path, scopes):
            seen["path"] = path
            return _FakeCreds()

        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            staticmethod(fake_from_file))
        monkeypatch.setattr(check_seed, "_resolve_key_path",
                            lambda settings: "/path/to/seed-sa.json")

        check_seed._build("drive", "v3", ["scope"], "admin@example.com")
        assert seen["path"] == "/path/to/seed-sa.json"

    def test_build_source_calls_the_shared_resolver_not_settings_directly(self):
        """A hardcoded `settings.source_sa_key` in _build()'s actual
        credential call is exactly the regression this guards against --
        assert the code calls the same resolver seed_sandbox.py itself
        uses, not the raw field. Checked past the docstring, which
        necessarily names the old field in its explanation."""
        src = inspect.getsource(check_seed._build)
        body = src[src.index('"""', src.index('"""') + 3) + 3:]
        assert "_resolve_key_path(settings)" in body
        assert "settings.source_sa_key" not in body


class TestAccountChecking:
    """
    check_accounts() used to check a hardcoded alice/bob/carol/dave/erin,
    regardless of who the seeder would actually target. It now checks
    exactly discover_tenant_entries()'s output -- the same function
    seed_sandbox.py's own default path uses -- so this preflight and a
    plain seeding run never disagree about "the accounts".
    """

    @pytest.fixture(autouse=True)
    def _settings(self, settings):
        settings.source_admin = "admin@src.example.com"
        self.settings = settings
        return settings

    def test_checks_every_discovered_account_not_a_fixed_five(self, monkeypatch):
        entries = [{"email": f"user{i}@src.example.com"} for i in range(8)]
        monkeypatch.setattr(check_seed, "discover_tenant_entries",
                            lambda s: (entries, ""))
        present = {e["email"] for e in entries}
        monkeypatch.setattr(check_seed, "_build",
                            lambda *a, **k: _FakeDirectorySvc(present))

        assert check_seed.check_accounts(self.settings) is True

    def test_a_missing_discovered_account_fails_the_check(self, monkeypatch, capsys):
        entries = [{"email": "alice@src.example.com"},
                  {"email": "ghost@src.example.com"}]
        monkeypatch.setattr(check_seed, "discover_tenant_entries",
                            lambda s: (entries, ""))
        monkeypatch.setattr(check_seed, "_build",
                            lambda *a, **k: _FakeDirectorySvc({"alice@src.example.com"}))

        assert check_seed.check_accounts(self.settings) is False
        out = capsys.readouterr().out
        assert "MISS ghost@src.example.com" in out

    def test_the_fallback_warning_is_surfaced_to_the_operator(self, monkeypatch, capsys):
        """discover_tenant_entries() falls back to the 5-user default with a
        warning when live discovery cannot run -- that warning must reach
        the person running this check, not be silently absorbed."""
        entries = [{"email": "alice@src.example.com"}]
        monkeypatch.setattr(
            check_seed, "discover_tenant_entries",
            lambda s: (entries, "SOURCE_ADMIN is not set, so the real "
                                "tenant headcount could not be read"))
        monkeypatch.setattr(check_seed, "_build",
                            lambda *a, **k: _FakeDirectorySvc({"alice@src.example.com"}))

        check_seed.check_accounts(self.settings)
        out = capsys.readouterr().out
        assert "tenant headcount could not be read" in out

    def test_no_source_admin_is_reported_without_calling_discovery(self, monkeypatch):
        self.settings.source_admin = ""
        called = []
        monkeypatch.setattr(check_seed, "discover_tenant_entries",
                            lambda s: called.append(1) or ([], ""))

        assert check_seed.check_accounts(self.settings) is False
        assert not called


class TestScopeReporting:
    def test_the_reported_scopes_are_derived_not_hand_written(self):
        """The count and the names drifted apart once SEED_SCOPES grew the
        Chat scopes: it printed "(7)" beside a list of five."""
        src = inspect.getsource(check_seed.check_scopes)
        assert "SEED_SCOPES" in src
        assert "gmail.insert/labels/modify" not in src, (
            "hand-written scope names drift from the list they describe")

    def test_one_token_mint_validates_every_requested_scope(self):
        """Delegation is all-or-nothing: a token request naming any
        unauthorised scope fails outright, so a single successful call proves
        the whole set. That is why this is cheap enough to run casually."""
        src = inspect.getsource(check_seed.check_scopes)
        assert "_build(\"drive\", \"v3\", SEED_SCOPES" in src

    def test_directory_write_is_checked_separately(self):
        """--create-users needs it; seeding into existing accounts does not.
        Conflating them would block a run that would have worked."""
        src = inspect.getsource(check_seed.check_scopes)
        assert "DIRECTORY_WRITE_SCOPE" in src
        assert "you can ignore this" in src

    def test_a_missing_admin_is_reported_rather_than_crashing(self):
        src = inspect.getsource(check_seed.check_scopes)
        assert "SOURCE_ADMIN is not set" in src


class TestItIsReadOnly:
    def test_nothing_here_writes_to_the_tenant(self):
        """A preflight that modified the tenant would be worse than none."""
        src = inspect.getsource(check_seed)
        # sys.path.insert is not a Drive call; look only at API-shaped lines.
        api_lines = [ln for ln in src.splitlines()
                     if "sys.path" not in ln and "import" not in ln]
        joined = "\n".join(api_lines)
        for writer in (".create(", ".delete(", ".update(", ".trash(",
                       ".patch(", ".messages().insert(", ".labels().create("):
            assert writer not in joined, f"check_seed performs a write: {writer}"

    def test_it_exits_non_zero_when_a_check_fails(self):
        """The web UI shows the return code; a failed preflight that exits 0
        looks like a pass."""
        src = inspect.getsource(check_seed.main)
        assert "return 1" in src


class TestErrorMessages:
    def test_long_api_errors_are_truncated(self):
        """Google's auth errors run to several hundred characters and bury the
        one useful word."""
        long = Exception("x" * 500)
        assert len(check_seed._short(long)) <= 220

    def test_an_empty_error_still_names_its_type(self):
        assert check_seed._short(TimeoutError()) == "TimeoutError"

    def test_newlines_are_flattened(self):
        assert "\n" not in check_seed._short(Exception("a\nb\nc"))
