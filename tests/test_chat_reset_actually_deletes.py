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

    def test_reset_target_does_not_force_it_on(self):
        # It did, briefly, and that would fail every target call on any
        # tenant without the grant.
        src = open(os.path.join(ROOT, "reset_target.py"), encoding="utf-8").read()
        assert "settings.chat_allow_delete = True" not in src


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
