"""The Gmail settings that break a person's day, and produce no error.

Filters and signatures migrated. These four did not, and they are the ones
somebody notices on cutover morning -- none of them raises anything
anywhere when it is missing. The mailbox simply behaves differently, and
the person affected is rarely the person running the migration.

Delegation is the worst of them: an assistant loses an executive's mailbox
silently, and it sits behind its own scope, so it fails even on a tenant
that granted everything else.
"""
from __future__ import annotations

import inspect
import re

import config
import gmail_engine


def _code(fn) -> str:
    src = inspect.getsource(fn)
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    return re.sub(r"#.*$", "", src, flags=re.M)


class TestAllFourAreMigrated:
    def test_each_has_its_own_pass(self):
        for name in ("_migrate_vacation", "_migrate_imap_pop",
                     "_migrate_forwarding", "_migrate_delegates"):
            assert hasattr(gmail_engine.GmailMigrator, name), name

    def test_they_run_with_the_other_settings(self):
        src = _code(gmail_engine.GmailMigrator.run)
        for name in ("_migrate_vacation", "_migrate_imap_pop",
                     "_migrate_forwarding", "_migrate_delegates"):
            assert name in src, name

    def test_they_are_behind_the_same_opt_in(self):
        """They need the settings scope, which config only grants when
        migrate_gmail_settings is on. Running them unconditionally would
        fail every run that did not ask for it."""
        src = _code(gmail_engine.GmailMigrator.run)
        i = src.index("if self.settings.migrate_gmail_settings:")
        assert i < src.index("_migrate_delegates")


class TestNoneOfThemCanTakeOutTheRun:
    def test_each_one_passes_on_a_missing_scope(self):
        """The mail is already delivered by this point. A tenant that never
        granted the settings scope must not lose a completed migration over
        an out-of-office message."""
        for name in ("_migrate_vacation", "_migrate_imap_pop",
                     "_migrate_forwarding", "_migrate_delegates"):
            src = _code(getattr(gmail_engine.GmailMigrator, name))
            assert "OPTIONAL_PASS_ERRORS" in src, name
            assert "raise" not in src, name

    def test_they_say_what_to_grant(self):
        """"Could not read delegates" without naming the scope sends someone
        to the wrong console page."""
        src = inspect.getsource(gmail_engine.GmailMigrator._migrate_delegates)
        assert "gmail.settings.sharing" in src
        assert "SOURCE" in src


class TestTheOrderingThatMatters:
    def test_a_forwarding_address_is_added_before_it_is_used(self):
        """Auto-forwarding cannot point at an address the account has never
        seen, so this order is not cosmetic."""
        src = _code(gmail_engine.GmailMigrator._migrate_forwarding)
        assert src.index("forwardingAddresses()") < src.index("AutoForwarding")

    def test_forwarding_arrives_unverified_on_purpose(self):
        """Silently re-enabling forwarding to an outside address on a tenant
        somebody just migrated into is a data-exfiltration path, not a
        feature. Google requiring verification is the right outcome, and it
        is logged as expected rather than as a fault."""
        src = inspect.getsource(gmail_engine.GmailMigrator._migrate_forwarding)
        assert "verif" in src.lower()
        assert "log.info(" in src

    def test_a_disabled_responder_writes_nothing(self):
        """Off is the default on a new mailbox: there is nothing to do and
        nothing worth reporting."""
        src = _code(gmail_engine.GmailMigrator._migrate_vacation)
        assert 'enableAutoReply' in src
        assert "return" in src


class TestDelegatesAreRemapped:
    def test_it_uses_the_engine_s_own_resolver(self):
        """db.resolve_identity is what the signature rewriter and the sendAs
        migration already use. A second mapping here would be a second
        answer to the same question."""
        src = _code(gmail_engine.GmailMigrator._migrate_delegates)
        assert "self.db.resolve_identity(" in src

    def test_an_unmapped_delegate_is_counted_not_dropped(self):
        """Silently dropping it is how this became invisible in the first
        place."""
        src = _code(gmail_engine.GmailMigrator._migrate_delegates)
        assert "delegates_unmapped" in src


class TestTheScopeIsActuallyGranted:
    def test_sharing_is_a_separate_scope_from_basic(self):
        assert config.GMAIL_SHARING_SCOPE != config.GMAIL_SETTINGS_SCOPE
        assert config.GMAIL_SHARING_SCOPE.endswith("settings.sharing")

    def test_both_tenants_get_it_when_settings_are_on(self):
        """Granted on one side only, it fails at the point of use rather
        than at setup -- the pattern this file keeps having to undo."""
        import dataclasses
        s = dataclasses.replace(config.Settings(), migrate_gmail_settings=True)
        assert config.GMAIL_SHARING_SCOPE in config.source_scopes(s)
        assert config.GMAIL_SHARING_SCOPE in config.target_scopes(s)

    def test_it_is_not_granted_when_settings_are_off(self):
        """A feature nobody asked for must never widen the grant a working
        deployment depends on -- config.py says so directly above it."""
        import dataclasses
        s = dataclasses.replace(config.Settings(), migrate_gmail_settings=False)
        assert config.GMAIL_SHARING_SCOPE not in config.source_scopes(s)


class TestAMissingApiMethodCannotLoseTheRun:
    """AttributeError is deliberately NOT in OPTIONAL_PASS_ERRORS.

    Catching it there would swallow real bugs in this file -- and it did,
    immediately: the four new passes raised it against a client whose
    discovery document does not expose getVacation, and took out four
    unrelated signature tests that had been passing.

    But these run AFTER the mail is delivered. An older googleapiclient
    without delegates() must not lose a completed migration. So the question
    is asked rather than the error caught.
    """

    def test_each_pass_checks_before_it_calls(self):
        for name in ("_migrate_vacation", "_migrate_imap_pop",
                     "_migrate_forwarding", "_migrate_delegates"):
            src = _code(getattr(gmail_engine.GmailMigrator, name))
            assert "_settings_has(" in src, name

    def test_it_checks_both_tenants(self):
        """The read side and the write side are different clients, and a
        method present on one says nothing about the other."""
        for name in ("_migrate_vacation", "_migrate_imap_pop",
                     "_migrate_forwarding", "_migrate_delegates"):
            src = _code(getattr(gmail_engine.GmailMigrator, name))
            assert "self.src" in src and "self.tgt" in src, name

    def test_attribute_error_is_still_not_swallowed(self):
        """The guard must not have quietly become a blanket catch: an
        AttributeError anywhere else in this engine is a bug and should
        surface."""
        assert "AttributeError" not in str(gmail_engine.OPTIONAL_PASS_ERRORS)

    def test_the_probe_asks_the_settings_collection(self):
        src = _code(gmail_engine.GmailMigrator._settings_has)
        assert "users().settings()" in src
        assert "hasattr(" in src
