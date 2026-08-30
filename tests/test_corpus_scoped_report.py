"""Failures and skips must reflect the CURRENT corpus. A reseed leaves audit
rows for deleted users; those must not count against this run."""
import re
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "api_server.py"), encoding="utf-8").read()


def _block(anchor):
    i = SRC.index(anchor)
    return SRC[i:i + 1300]


def test_items_failed_is_corpus_scoped():
    b = _block('out["itemsFailed"]')
    assert "EXISTS (SELECT 1 FROM identity_map" in b
    assert "m.source_email = a.source_user" in b


def test_items_skipped_is_corpus_scoped_from_audit_log():
    b = _block('out["itemsSkipped"]')
    assert "FROM audit_log" in b
    assert "EXISTS (SELECT 1 FROM identity_map" in b
    assert "audit_counts" not in b        # the un-scopable aggregate is gone here


def test_failure_breakdown_is_corpus_scoped():
    b = _block('out["failures"] = _group_failures')
    assert "EXISTS (SELECT 1 FROM identity_map" in b


def test_skip_breakdown_is_corpus_scoped():
    b = _block('out["skipped"] = [')
    assert "FROM audit_log" in b and "EXISTS (SELECT 1 FROM identity_map" in b
