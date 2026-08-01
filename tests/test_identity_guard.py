"""
tests/test_identity_guard.py
============================
Does identity_map describe the tenants we are configured for?

A migration.db outlives the run that created it. Point the same directory at a
second tenant pair and the previous map is still sitting there, so every
command silently operates on the *old* migration's users.

Seen live: env.sh configured for c.→a., identity_map still holding one.→two.,
and `preflight` reporting ten authentication failures — all of them about
accounts in a tenant that was no longer being migrated. The output was
entirely truthful and entirely misleading.
"""

from __future__ import annotations

import pytest

from config import Settings
from main import identity_domain_mismatch


def rows(*pairs):
    return [{"source_email": a, "target_email": b} for a, b in pairs]


def settings(src="c.example.com", tgt="a.example.com"):
    s = Settings()
    s.source_domain, s.target_domain = src, tgt
    return s


class TestIdentityDomainMismatch:
    def test_matching_domains_pass_silently(self):
        r = rows(("alice@c.example.com", "alice@a.example.com"),
                 ("bob@c.example.com", "bob@a.example.com"))
        assert identity_domain_mismatch(r, settings()) == ""

    def test_the_live_case_is_caught(self):
        """Exactly what happened: a whole map from the previous tenant pair."""
        r = rows(("alice.brown@one.example.com", "alice.brown@two.example.com"))
        msg = identity_domain_mismatch(r, settings())
        assert "one.example.com" in msg and "c.example.com" in msg
        assert "two.example.com" in msg and "a.example.com" in msg

    def test_source_only_mismatch_is_reported_alone(self):
        r = rows(("alice@wrong.com", "alice@a.example.com"))
        msg = identity_domain_mismatch(r, settings())
        assert "source addresses" in msg
        assert "target addresses" not in msg

    def test_target_only_mismatch_is_reported_alone(self):
        r = rows(("alice@c.example.com", "alice@wrong.com"))
        msg = identity_domain_mismatch(r, settings())
        assert "target addresses" in msg
        assert "source addresses" not in msg

    def test_one_stray_row_among_good_ones_is_caught(self):
        """A partially reloaded map is worse than a wholly wrong one -- most
        users migrate correctly and a few go somewhere unexpected."""
        r = rows(("alice@c.example.com", "alice@a.example.com"),
                 ("ghost@old.example.com", "ghost@a.example.com"))
        assert "old.example.com" in identity_domain_mismatch(r, settings())

    def test_case_differences_are_not_a_mismatch(self):
        r = rows(("Alice@C.Example.COM", "Alice@A.Example.COM"))
        assert identity_domain_mismatch(r, settings()) == ""

    def test_unconfigured_domains_do_not_produce_noise(self):
        """Before step 2 there is nothing to compare against; claiming a
        mismatch then would be wrong."""
        r = rows(("alice@anything.com", "bob@other.com"))
        assert identity_domain_mismatch(r, settings("", "")) == ""

    def test_empty_map_is_not_a_mismatch(self):
        assert identity_domain_mismatch([], settings()) == ""

    def test_every_offending_domain_is_named(self):
        r = rows(("a@x.com", "a@a.example.com"), ("b@y.com", "b@a.example.com"))
        msg = identity_domain_mismatch(r, settings())
        assert "x.com" in msg and "y.com" in msg


class TestWizardSurfacesTheMismatch:
    def test_a_mismatched_map_does_not_count_as_loaded(self, tmp_path, monkeypatch):
        """Reporting step 4 as done would walk the operator straight into a
        migration aimed at the wrong tenant."""
        import sqlite3

        import wizard

        db = tmp_path / "migration.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE identity_map (source_email TEXT, "
                    "target_email TEXT, entity_type TEXT, status TEXT, "
                    "notes TEXT, created_at TEXT)")
        con.execute("INSERT INTO identity_map VALUES "
                    "('alice@one.example.com','alice@two.example.com',"
                    "'user','PENDING','','')")
        con.commit()
        con.close()

        monkeypatch.setenv("SOURCE_DOMAIN", "c.example.com")
        monkeypatch.setenv("TARGET_DOMAIN", "a.example.com")

        st = wizard.State.__new__(wizard.State)
        st.env, st.notes, st.gcloud = {"MIGRATION_DB": str(db)}, {}, ""
        st._preflight = None

        assert st.identities_loaded() == 0
        assert "WRONG TENANTS" in st.notes["identities"]

    def test_a_matching_map_counts_normally(self, tmp_path, monkeypatch):
        import sqlite3

        import wizard

        db = tmp_path / "migration.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE identity_map (source_email TEXT, "
                    "target_email TEXT, entity_type TEXT, status TEXT, "
                    "notes TEXT, created_at TEXT)")
        con.execute("INSERT INTO identity_map VALUES "
                    "('alice@c.example.com','alice@a.example.com',"
                    "'user','PENDING','','')")
        con.commit()
        con.close()

        monkeypatch.setenv("SOURCE_DOMAIN", "c.example.com")
        monkeypatch.setenv("TARGET_DOMAIN", "a.example.com")

        st = wizard.State.__new__(wizard.State)
        st.env, st.notes, st.gcloud = {"MIGRATION_DB": str(db)}, {}, ""
        st._preflight = None

        assert st.identities_loaded() == 1
        assert "WRONG TENANTS" not in st.notes["identities"]
