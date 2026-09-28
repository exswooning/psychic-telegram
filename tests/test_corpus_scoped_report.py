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


def test_metrics_page_failure_breakdown_is_also_corpus_scoped():
    """The Metrics page's pie chart reuses the same grouped-by-cause query (a second
    `out["failures"] = _group_failures` call site, inside the metrics endpoint) -- it must
    not lose the scoping the Migrations page's copy has, or a reseed's deleted users would
    inflate the pie with causes from a run that no longer exists."""
    i = SRC.index('out["failures"] = _group_failures')
    j = SRC.index('out["failures"] = _group_failures', i + 1)
    assert "EXISTS (SELECT 1 FROM identity_map" in SRC[j:j + 1300]


def test_skip_breakdown_is_corpus_scoped():
    b = _block('out["skipped"] = [')
    assert "FROM audit_log" in b and "EXISTS (SELECT 1 FROM identity_map" in b
