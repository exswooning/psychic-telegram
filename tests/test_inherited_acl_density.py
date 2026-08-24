"""Decide from the corpus whether per-file inherited grants are affordable.

recreate_inherited_acls defaults on, which is right for an ordinary tenant:
each document then carries its own sharing, so access survives the file being
moved out of the folder it was shared through.

It stops being right when a corpus shares at FOLDER level. Every file in a
shared folder carries a copy of that folder's whole grant list, at one
permissions.create per grantee per file. Live on a 201-user tenant:
9,721,368 ACL operations across 191,672 files -- about 97% of every API call
the migration made -- for access the copied folder tree already gave. Users
took 15 to 35 hours each.

config.py always had the flag, and its comment always said to turn it off
"on very large tenants". That needs someone to know the flag exists, know
this tenant is one of those, and know it before starting. Nobody has all
three on a first run.
"""
import pytest

import drive_engine


class Settings:
    def __init__(self, recreate=True):
        self.recreate_inherited_acls = recreate


@pytest.fixture(autouse=True)
def _reset():
    drive_engine._INHERIT_STATS.clear()
    drive_engine._INHERIT_STATS.update(
        {"files": 0, "inherited": 0, "disabled": False})
    yield


def _collect():
    msgs = []
    return msgs, (lambda fmt, *a: msgs.append(fmt % a))


class TestItDisablesOnlyWhenTheCorpusIsPathological:
    def test_a_dense_corpus_switches_to_folder_derived_sharing(self):
        s = Settings()
        msgs, rec = _collect()
        for _ in range(drive_engine.INHERIT_SAMPLE_FILES + 5):
            drive_engine._note_inherited_density(202, s, rec)
        assert drive_engine._inherited_acls_affordable(s) is False
        assert len(msgs) == 1, "must say so exactly once, not per file"
        assert "FOLDER level" in msgs[0]

    def test_ordinary_per_file_sharing_is_left_alone(self):
        """A few grantees per file is what the setting exists to preserve."""
        s = Settings()
        msgs, rec = _collect()
        for _ in range(500):
            drive_engine._note_inherited_density(3, s, rec)
        assert drive_engine._inherited_acls_affordable(s) is True
        assert msgs == []

    def test_it_does_not_decide_before_it_has_a_sample(self):
        """A handful of heavily-shared files says nothing about a tenant, and
        disabling on one outlier silently changes what a small migration
        preserves."""
        s = Settings()
        msgs, rec = _collect()
        for _ in range(drive_engine.INHERIT_SAMPLE_FILES - 1):
            drive_engine._note_inherited_density(500, s, rec)
        assert drive_engine._inherited_acls_affordable(s) is True
        assert msgs == []

    def test_the_operator_can_still_turn_it_off_outright(self):
        assert drive_engine._inherited_acls_affordable(
            Settings(recreate=False)) is False

    def test_an_explicit_setting_is_not_re_enabled_by_a_sparse_corpus(self):
        s = Settings(recreate=False)
        for _ in range(200):
            drive_engine._note_inherited_density(0, s)
        assert drive_engine._inherited_acls_affordable(s) is False


class TestTheDecisionIsProcessWide:
    def test_it_holds_across_users(self):
        """It is a property of the corpus, not of one mailbox -- deciding per
        user would re-pay the sample cost for every one of 201 of them."""
        s = Settings()
        for _ in range(drive_engine.INHERIT_SAMPLE_FILES):
            drive_engine._note_inherited_density(200, s)
        assert drive_engine._inherited_acls_affordable(Settings()) is False

    def test_counting_stops_once_decided(self):
        """No point sampling a question already answered."""
        s = Settings()
        for _ in range(drive_engine.INHERIT_SAMPLE_FILES):
            drive_engine._note_inherited_density(200, s)
        before = drive_engine.inherited_acl_stats()["files"]
        for _ in range(50):
            drive_engine._note_inherited_density(200, s)
        assert drive_engine.inherited_acl_stats()["files"] == before

    def test_stats_report_what_was_measured(self):
        """A run that quietly changed what it preserves must be able to say
        so afterwards."""
        s = Settings()
        for _ in range(drive_engine.INHERIT_SAMPLE_FILES):
            drive_engine._note_inherited_density(100, s)
        st = drive_engine.inherited_acl_stats()
        assert st["disabled"] is True
        assert st["density"] == 100.0


class TestThresholdBoundary:
    def test_just_under_the_limit_keeps_per_file_grants(self):
        s = Settings()
        under = int(drive_engine.INHERIT_DENSITY_LIMIT) - 1
        for _ in range(drive_engine.INHERIT_SAMPLE_FILES + 1):
            drive_engine._note_inherited_density(under, s)
        assert drive_engine._inherited_acls_affordable(s) is True

    def test_at_the_limit_switches(self):
        s = Settings()
        at = int(drive_engine.INHERIT_DENSITY_LIMIT)
        for _ in range(drive_engine.INHERIT_SAMPLE_FILES + 1):
            drive_engine._note_inherited_density(at, s)
        assert drive_engine._inherited_acls_affordable(s) is False
