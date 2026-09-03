"""The Chat reset deleted nothing, for as long as it has existed.

reset_chat() calls spaces().delete(), but chat.spaces authorizes
create/list/patch and NOT delete, so every call came back 403. The except
around it was a bare `pass`, so `deleted` stayed 0 and the run printed

    [191/201] elena.tamang78@...: 0 files, 7234 messages, 492 events,
              0 chat spaces, 100 contacts, 5 task list(s) deleted

for all 201 users. "0 chat spaces" is what a genuine zero, a failed list
and a failed delete all print, so nothing distinguished a tenant that was
clean from one that had not been touched. 200 spaces survived a reset that
reported success.

Why it mattered beyond tidiness: phases.py compares Chat on messages only
(phases.py: "chat": ("messages",)) and passes whenever target >= source, so
undeleted residue inflates the target and a Chat migration that moved
little or nothing still verifies OK.

Why no test caught it: the seeder's FakeChat honours delete unconditionally,
so the suite exercised a path that 403s in production.
"""
import sys
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "data-generator"))


class TestTheScopeIsAvailableButNotForced:
    """Requesting an ungranted scope fails the WHOLE token exchange.

    Adding chat.delete to SEED_SCOPES unconditionally was tried and broke
    seeding outright -- confirmed live, through the UI's own check:

        FAIL seed write scopes -> unauthorized_client: Client is
        unauthorized to retrieve access tokens using this method

    Drive, Gmail and Calendar seeding all went down with it, for a Chat
    delete nobody had granted yet. So the scope is available behind a flag
    and off until the Admin Console grant exists -- the same shape as
    contacts/tasks, which build_people_tasks keeps on their own credential
    for exactly this reason.
    """

    def test_the_scope_is_named_somewhere(self):
        import seed_sandbox
        assert seed_sandbox.CHAT_DELETE_SCOPE == \
            "https://www.googleapis.com/auth/chat.delete"

    def test_it_is_not_in_the_default_seed_scopes(self):
        import seed_sandbox
        assert seed_sandbox.CHAT_DELETE_SCOPE not in seed_sandbox.SEED_SCOPES, (
            "an ungranted scope here fails drive/gmail/calendar seeding too")

    def test_the_flag_adds_it(self):
        import seed_sandbox
        from config import Settings
        st = Settings()
        st.chat_allow_delete = True
        assert seed_sandbox.CHAT_DELETE_SCOPE in seed_sandbox.seed_scopes(st)

    def test_the_flag_is_off_by_default(self):
        import seed_sandbox
        from config import Settings
        assert seed_sandbox.CHAT_DELETE_SCOPE not in \
            seed_sandbox.seed_scopes(Settings())

    def test_no_settings_at_all_still_works(self):
        # Callers that predate the flag pass nothing.
        import seed_sandbox
        assert seed_sandbox.seed_scopes(None) == seed_sandbox.SEED_SCOPES

    def test_the_target_side_has_the_same_switch(self):
        from config import CHAT_DELETE_SCOPE, Settings, target_scopes
        st = Settings()
        st.migrate_chat = True
        assert CHAT_DELETE_SCOPE not in target_scopes(st)
        st.chat_allow_delete = True
        assert CHAT_DELETE_SCOPE in target_scopes(st)

    def test_a_migration_never_asks_for_delete_by_default(self):
        from config import CHAT_DELETE_SCOPE, CHAT_SCOPES
        assert CHAT_DELETE_SCOPE not in CHAT_SCOPES

    def test_reset_target_never_enables_it_without_proving_the_grant(self):
        """It once set the flag unconditionally, and that fails every target
        call on a tenant without the grant -- an ungranted scope takes down
        the whole token exchange, Drive and Gmail with it.

        The rule is "never without the grant", not "never". Requiring a human
        to remember a second switch has its own failure, and it is the quiet
        one: the grant was made months ago, the flag never followed, and the
        reset silently left every Chat space standing. So enabling it is
        allowed only where a token mint has just proved the scope is real.

        Asserted structurally rather than as a string match, because the
        previous check could not tell a blind force from a verified one.
        """
        src = open(os.path.join(ROOT, "reset_target.py"), encoding="utf-8").read()
        if "chat_allow_delete = True" not in src:
            return                      # not enabling it at all is still fine
        # Everything from the probe to the assignment must sit in the `else`
        # of a try that refreshes real credentials.
        head = src.split("chat_allow_delete = True")[0]
        assert "creds.refresh(" in head, \
            "the flag is set without minting a token to prove the scope"
        assert "CHAT_DELETE_SCOPE" in head, \
            "the token minted must be for chat.delete specifically"
        tail = head.rsplit("creds.refresh(", 1)[1]
        assert "else:" in tail, \
            "the flag must be set in the else of the probe, not after it -- " \
            "an exception path that still enables it is the original bug"


class TestAFailedDeleteIsNoLongerSilent:
    """A 403 that prints as "0 spaces deleted" is indistinguishable from
    success. That silence is what let a missing scope survive."""

    class _Spaces:
        def __init__(self, store, refuse):
            self._store, self._refuse = store, refuse

        def list(self, **kw):
            store = self._store

            class _R:
                def execute(self_inner):
                    return {"spaces": [{"name": k, "displayName": v}
                                       for k, v in store.items()]}
            return _R()

        def delete(self, name):
            refuse = self._refuse

            class _R:
                def execute(self_inner):
                    if refuse:
                        raise RuntimeError(
                            '<HttpError 403 "Request had insufficient '
                            'authentication scopes.">')
                    return {}
            return _R()

    class _Chat:
        def __init__(self, store, refuse):
            self._s = TestAFailedDeleteIsNoLongerSilent._Spaces(store, refuse)

        def spaces(self):
            return self._s

    def _run(self, refuse, capsys):
        import seed_sandbox
        from config import Settings
        names = seed_sandbox._chat_names("alice")
        store = {f"spaces/{i}": n for i, n in enumerate(names)}
        chat = self._Chat(store, refuse)
        deleted = seed_sandbox.reset_chat(chat, Settings(), local="alice")
        return deleted, capsys.readouterr()

    def test_a_403_is_reported(self, capsys):
        deleted, cap = self._run(True, capsys)
        assert deleted == 0
        assert "could not be deleted" in cap.err
        assert "403" in cap.err

    def test_it_names_how_many_it_could_not_delete(self, capsys):
        _, cap = self._run(True, capsys)
        # "0 deleted" told you nothing; "N matched but could not be deleted"
        # tells you the tenant is still dirty.
        assert "space(s) matched" in cap.err

    def test_a_failed_LIST_is_reported_too(self, capsys):
        """The other half of the same silence.

        Fixing only the delete left "0 chat spaces" still covering a failed
        list. The first reset after that fix reported zero deletes AND zero
        delete-failures across 201 users, for a tenant with 200 spaces
        still standing -- which is precisely what this branch produces, and
        is indistinguishable from a tenant that had no seeded spaces.
        """
        import seed_sandbox
        from config import Settings

        class _Boom:
            def spaces(self):
                class _S:
                    def list(self_inner, **kw):
                        class _R:
                            def execute(self_i):
                                raise RuntimeError("403 insufficient scopes")
                        return _R()
                return _S()

        deleted = seed_sandbox.reset_chat(_Boom(), Settings(), local="alice")
        cap = capsys.readouterr()
        assert deleted == 0
        assert "could not list spaces" in cap.err

    def test_a_working_delete_stays_quiet_and_counts(self, capsys):
        deleted, cap = self._run(False, capsys)
        assert deleted > 0
        assert "could not be deleted" not in cap.err

    def test_nothing_to_delete_is_not_reported_as_a_failure(self, capsys):
        import seed_sandbox
        from config import Settings
        chat = self._Chat({"spaces/9": "Real Room"}, True)
        deleted = seed_sandbox.reset_chat(chat, Settings(), local="alice")
        cap = capsys.readouterr()
        assert deleted == 0
        assert "could not be deleted" not in cap.err, (
            "a space this seeder never created is not a failure")
