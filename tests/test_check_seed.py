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

import check_seed


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
