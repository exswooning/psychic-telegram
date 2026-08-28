"""An unlicensed account looks like an outage unless someone checks.

_NO_MAILBOX already recognised the Gmail symptom of a missing licence
("Mail service not enabled" + failedPrecondition). Drive runs BEFORE Gmail
in migrate_user, so for an unlicensed account that is not the error that
happens. What happens is:

    drive/v3/files/root -> 401 "Active session is invalid. Error code: 4"

which names no cause at all. seeduser382 failed exactly that way in two
separate migrations and was reported FAILED both times, while the
Licensing API answered HTTP 412 "There aren't enough available licenses"
for a tenant holding 201 accounts against 200 seats.

"Active session is invalid" on its own is NOT this: resilience.py retries
it precisely because a freshly-provisioned account says the same thing for
a minute or two. Only a version that outlived every retry is a candidate --
and even then the licence is checked, not assumed, because a revoked
delegation and a suspended account read identically.
"""
import main


DRIVE_401 = RuntimeError(
    'exhausted 6 retries on HTTP 401 (authError): <HttpError 401 when '
    'requesting https://www.googleapis.com/drive/v3/files/root?fields=id '
    'returned "The caller does not have permission". Details: "[{\'message\': '
    '\'Active session is invalid. Error code: 4\', \'reason\': \'authError\'}]">')

GMAIL_NO_MAILBOX = RuntimeError(
    'HTTP 400 (failedPrecondition): Mail service not enabled')

TRANSIENT = RuntimeError(
    'HTTP 401 (authError): Active session is invalid. Error code: 4')


class _S:
    """Stand-in Settings; only identity matters to the code under test."""


class TestWhatCountsAsBlocked:
    def test_the_drive_symptom_is_recognised(self):
        assert main.is_blocked_externally(DRIVE_401)

    def test_the_gmail_symptom_still_is(self):
        assert main.is_blocked_externally(GMAIL_NO_MAILBOX)

    def test_a_transient_401_that_never_exhausted_is_not(self):
        """The freshly-provisioned case resilience.py retries. Marking it
        blocked would park a user that was about to succeed."""
        assert not main.is_blocked_externally(TRANSIENT)

    def test_an_unrelated_error_is_not(self):
        assert not main.is_blocked_externally(RuntimeError("HTTP 500 backendError"))


class TestTheMessageStatesAFactOrSaysItCannot:
    def test_no_licence_is_named_outright(self, monkeypatch):
        monkeypatch.setattr(main, "_licence_of", lambda s, u: (None, ""))
        out = main.explain_user_failure(DRIVE_401, "a@src", "a@tgt", _S())
        assert "NO Workspace licence" in out
        assert "Billing > Licences" in out
        assert "HTTP 412" in out, "should say what happens when seats run out"

    def test_a_held_licence_rules_that_cause_out(self, monkeypatch):
        # Naming the wrong cause sends somebody to the wrong console page.
        monkeypatch.setattr(main, "_licence_of",
                            lambda s, u: ("1010020027", ""))
        out = main.explain_user_failure(DRIVE_401, "a@src", "a@tgt", _S())
        assert "DOES hold a licence" in out
        assert "suspended" in out and "delegation" in out

    def test_an_unanswerable_check_says_so(self, monkeypatch):
        monkeypatch.setattr(main, "_licence_of",
                            lambda s, u: ("", "scope not delegated"))
        out = main.explain_user_failure(DRIVE_401, "a@src", "a@tgt", _S())
        assert "Could not check" in out
        assert "scope not delegated" in out

    def test_the_original_error_is_always_kept(self, monkeypatch):
        """A diagnosis layer that paraphrases away the detail that would
        have identified the error is worse than none."""
        monkeypatch.setattr(main, "_licence_of", lambda s, u: (None, ""))
        for exc in (DRIVE_401, GMAIL_NO_MAILBOX):
            out = main.explain_user_failure(exc, "a@src", "a@tgt", _S())
            assert str(exc)[:60] in out

    def test_it_works_without_settings(self):
        # Callers that predate the parameter must not crash.
        out = main.explain_user_failure(DRIVE_401, "a@src", "a@tgt")
        assert "Could not check" in out

    def test_an_unrecognised_error_is_returned_verbatim(self):
        exc = RuntimeError("something nobody has seen before")
        assert main.explain_user_failure(exc, "a@src", "a@tgt", _S()) == str(exc)


class TestTheProbeNeverBreaksTheRun:
    def test_a_throwing_lookup_is_swallowed_into_a_reason(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("licensing API down")

        import tenant_inventory
        monkeypatch.setattr(tenant_inventory, "licenses", boom)
        sku, why = main._licence_of(_S(), "a@src")
        assert sku == "" and "licensing API down" in why

    def test_a_reported_scope_gap_becomes_the_reason(self, monkeypatch):
        import tenant_inventory
        monkeypatch.setattr(tenant_inventory, "licenses",
                            lambda s, side: ({}, "scope not delegated"))
        assert main._licence_of(_S(), "a@src") == ("", "scope not delegated")

    def test_a_known_user_returns_their_sku(self, monkeypatch):
        import tenant_inventory
        monkeypatch.setattr(tenant_inventory, "licenses",
                            lambda s, side: ({"a@src": "1010020027"}, ""))
        assert main._licence_of(_S(), "A@SRC")[0] == "1010020027"

    def test_an_absent_user_returns_none_not_empty(self, monkeypatch):
        # None means "no licence"; "" means "could not tell". Collapsing
        # them would report a definite cause from a failed lookup.
        import tenant_inventory
        monkeypatch.setattr(tenant_inventory, "licenses",
                            lambda s, side: ({"other@src": "x"}, ""))
        assert main._licence_of(_S(), "a@src")[0] is None


class TestTheCallerPassesSettings:
    def test_migrate_user_hands_the_probe_what_it_needs(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
        block = src.split("detail = explain_user_failure(")[1][:120]
        assert "settings" in block, "the probe can never run without it"
