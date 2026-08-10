"""
tests/test_ai_diagnostics.py
============================
The diagnostic summariser.

Two properties matter more than the prose it produces.

**It must not lose the operator's env.sh.** The key write is a
read-modify-write of the file that holds both tenants' service-account
paths and domains; truncating it takes the whole deployment down.

**It must not send anything the operator has not seen.** gather_context()
is the entire payload and the UI shows it verbatim before transmitting, so
a change that starts folding in something new -- credentials, message
bodies -- has to be a visible change here.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_diagnostics as ai  # noqa: E402
from db import MigrationDB   # noqa: E402


class TestKeyStorage:
    def test_writing_a_key_preserves_every_other_entry(self, tmp_path,
                                                       monkeypatch):
        """env.sh carries SOURCE_SA_KEY, the domains, TRANSFER_MODE. A write
        that truncated it would take the deployment down, and the symptom
        would be an unrelated auth error hours later."""
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        p = tmp_path / "env.sh"
        p.write_text("export SOURCE_DOMAIN=c.example.com\n"
                     "export TRANSFER_MODE=server_side\n")
        ai.write_key(str(p), "gsk_secret")
        body = p.read_text()
        assert "export SOURCE_DOMAIN=c.example.com" in body
        assert "export TRANSFER_MODE=server_side" in body
        assert "export GROQ_API_KEY=gsk_secret" in body

    def test_rewriting_replaces_rather_than_appends(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        p = tmp_path / "env.sh"
        ai.write_key(str(p), "gsk_one")
        ai.write_key(str(p), "gsk_two")
        assert p.read_text().count("GROQ_API_KEY") == 1
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert ai.read_key(str(p)) == "gsk_two"

    def test_env_var_wins_over_the_file(self, tmp_path, monkeypatch):
        """A key exported into the process must not be silently overridden by
        a stale one on disk."""
        p = tmp_path / "env.sh"
        p.write_text("export GROQ_API_KEY=from_file\n")
        monkeypatch.setenv("GROQ_API_KEY", "from_env")
        assert ai.read_key(str(p)) == "from_env"

    def test_a_missing_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert ai.read_key(str(tmp_path / "nope.sh")) == ""


class TestContext:
    def _db(self, tmp_path) -> str:
        path = str(tmp_path / "m.db")
        d = MigrationDB(path)
        from db import bulk_seed_identities
        bulk_seed_identities(d, [("alice@src", "alice@dst")])
        d.record_mapping("alice@src", "f1", "t1", "file")
        with d.write() as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO audit_log (source_user,item_id,item_type,status,"
                    "error_message) VALUES ('alice@src',?,'file','FAILED',"
                    "'HTTP 403 (storageQuotaExceeded): out of space')", (f"x{i}",))
        d.close()
        return path

    def test_failures_are_grouped_by_cause_not_listed(self, tmp_path):
        """636 identical storageQuotaExceeded lines tell an operator one
        thing. Pasting all 636 into a prompt buys nothing but tokens and a
        worse answer."""
        ctx = ai.gather_context(self._db(tmp_path))
        assert "3x file" in ctx
        assert ctx.count("storageQuotaExceeded") == 1

    def test_context_reports_what_exists_and_who_is_where(self, tmp_path):
        ctx = ai.gather_context(self._db(tmp_path))
        assert "## Migrated so far" in ctx
        assert "file: 1" in ctx
        assert "alice@src" in ctx

    def test_an_unreadable_ledger_is_stated_not_swallowed(self, tmp_path):
        """A summary built on a database that could not be opened must say
        so -- 'nothing failed' and 'I could not look' are different claims."""
        ctx = ai.gather_context(str(tmp_path / "missing.db"))
        assert "unreadable" in ctx

    def test_log_tail_is_included_and_denoised(self, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("FutureWarning: python 3.10 is old\n"
                       "  warnings.warn(message, FutureWarning)\n"
                       "real line: copied 5 files\n")
        ctx = ai.gather_context(self._db(tmp_path), str(log))
        assert "real line: copied 5 files" in ctx
        assert "FutureWarning" not in ctx

    def test_since_scopes_the_failure_window(self, tmp_path):
        path = self._db(tmp_path)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE audit_log SET timestamp='2020-01-01T00:00:00Z'")
        conn.commit(); conn.close()
        assert "3x file" in ai.gather_context(path)
        scoped = ai.gather_context(path, since_iso="2098-01-01T00:00:00Z")
        assert "- none" in scoped


class TestAnalyzeFailsSoft:
    def test_no_key_is_an_error_not_an_exception(self):
        """This panel diagnoses a running migration. Crashing the page it is
        meant to explain is worse than saying nothing."""
        md, err = ai.analyze("ctx", "")
        assert md == ""
        assert "no Groq API key" in err

    def test_network_failure_is_returned_not_raised(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("dns is down")
        monkeypatch.setattr(ai.urllib.request, "urlopen", boom)
        md, err = ai.analyze("ctx", "gsk_x")
        assert md == ""
        assert "could not reach Groq" in err

    def test_the_prompt_forbids_invention(self):
        """The whole value is catching a signal a human missed. A model that
        supplies plausible failures instead destroys that."""
        assert "Do not invent" in ai.SYSTEM_PROMPT
        assert "must appear in the input" in ai.SYSTEM_PROMPT
