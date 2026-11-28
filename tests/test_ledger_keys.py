"""Plan item 5: one parser for every grant key form, and nothing splits a ledger key at its last colon."""
import pathlib
import re

from ledger_keys import parse_grant

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_every_key_form():
    assert parse_grant("1abc:Bob@T.x") == ("1abc", "person", "bob@t.x")
    assert parse_grant("1abc:anyone") == ("1abc", "anyone", "")
    assert parse_grant("1SFxCgnG2pf2orfP0GR8dvVhDNUFCTV9X:domain:t.x") == ("1SFxCgnG2pf2orfP0GR8dvVhDNUFCTV9X", "domain", "t.x")
    assert parse_grant("1abc:(no-email)").kind == "no-email"
    assert parse_grant("1abc:(list-failed)").kind == "list-failed"


def test_no_code_splits_a_ledger_key_at_its_last_colon():
    """9 Nov 2026: a check did, read '<fid>:domain' as a file id and reported a file that never existed."""
    bad = [f"{p.name}:{n}" for p in ROOT.glob("*.py") for n, line in
           enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
           if re.search(r"item_id.*\.rpartition\(\":\"\)|audit_key.*\.rpartition\(\":\"\)", line)]
    assert bad == [], f"use ledger_keys.parse_grant: {bad}"
