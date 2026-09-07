"""The list each user needs, and the warning that matters more than the list.

Grants cannot be migrated -- no API creates one. sso.py collects them grouped
by app, which answers the operator's question. This answers the user's, and
carries the instruction that protects a paid subscription: be able to sign in
WITHOUT Google before the move.
"""
import reconnect_pack


class _DB:
    def __init__(self, rows): self._rows = rows
    def all_identities(self, status=None):
        return [{"source_email": s, "target_email": t} for s, t in self._rows]


def _patch_grants(monkeypatch, grants):
    monkeypatch.setattr(reconnect_pack, "AuthManager", lambda s: None)
    monkeypatch.setattr(
        reconnect_pack, "SSOMigrator",
        lambda auth, db, s: type("M", (), {
            "read_grants_by_user": lambda self, users: grants})())


class TestTheSheet:
    def test_every_mapped_user_appears_even_with_no_apps(self, settings, monkeypatch):
        """A user with nothing to reconnect still has to be accounted for,
        or "who is left" cannot be answered."""
        _patch_grants(monkeypatch, {"a@src.test": [{"name": "Canva"}]})
        db = _DB([("a@src.test", "a@tgt.test"), ("b@src.test", "b@tgt.test")])
        pack = reconnect_pack.build(db, settings)
        assert set(pack) == {"a@src.test", "b@src.test"}
        assert pack["b@src.test"]["apps"] == []

    def test_the_target_address_is_carried(self, settings, monkeypatch):
        _patch_grants(monkeypatch, {})
        db = _DB([("a@src.test", "a@tgt.test")])
        assert reconnect_pack.build(db, settings)["a@src.test"]["target"] == "a@tgt.test"

    def test_it_can_be_scoped_to_named_users(self, settings, monkeypatch):
        _patch_grants(monkeypatch, {})
        db = _DB([("a@src.test", "a@t"), ("b@src.test", "b@t")])
        assert set(reconnect_pack.build(db, settings, ["a@src.test"])) == {"a@src.test"}


class TestTheNotice:
    def _text(self, apps):
        return reconnect_pack.NOTICE.format(
            name="Alice", source_email="alice@src.test",
            target_email="alice@tgt.test", target_domain="tgt.test",
            app_block=reconnect_pack._app_block(apps))

    def test_it_tells_them_to_set_a_password_before_the_move(self):
        """The one instruction worth more than the app list: a subscription
        is recoverable through a password reset, and unrecoverable if Google
        sign-in is the only key."""
        t = self._text([{"name": "Canva"}])
        assert "BEFORE THE MOVE" in t
        assert "password" in t and "reset" in t
        assert t.index("BEFORE THE MOVE") < t.index("AFTER THE MOVE"), (
            "the protective step must come before the reconnect step")

    def test_it_names_their_actual_apps(self):
        t = self._text([{"name": "Canva"}, {"name": "Figma"}])
        assert "Canva" in t and "Figma" in t and "(2)" in t

    def test_a_user_with_no_apps_is_told_so_plainly(self):
        t = self._text([])
        assert "no connected apps" in t
        assert "Your connected apps" not in t

    def test_it_explains_the_blocked_case(self):
        """A blocked app looks like a broken app unless somebody says
        otherwise, and it is not the user's to fix."""
        assert "blocked" in self._text([])

    def test_it_uses_their_new_address_for_signing_in(self):
        t = self._text([{"name": "Canva"}])
        assert "alice@tgt.test" in t
        # and the old one only where a password reset would be sent
        assert "alice@src.test" in t


class TestGroupingComesOffOneScan:
    def test_by_app_is_built_from_by_user(self):
        """Two scans of every user's tokens to answer two shapes of the same
        question would double the slowest part of the inventory."""
        import inspect, sso
        src = inspect.getsource(sso.SSOMigrator.read_oauth_grants)
        assert "read_grants_by_user" in src
        assert "tokens()" not in src, "still scanning separately"
