"""
data-generator/test_big_attachment.py
=====================================
Above 25 MB Gmail does not attach a file. It uploads it to Drive, replaces
the attachment with a /file/d/<id>/view?usp=drive_web link, and grants the
recipients access. That is what a real tenant's large attachments ARE, and
the corpus contained nothing like it: its biggest attachment was 2 MB and
every linked file was one the peer already had rights to.

So the question a migration has to answer about them -- does the recipient
still reach the file after the link is rewritten -- could not be asked.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import corpus
import seed_sandbox as S


class TestItIsOffUnlessAsked:
    def test_no_setting_means_no_big_file(self, monkeypatch):
        """Tens of MB of upload per user is not something an ordinary seed
        should pay for."""
        monkeypatch.delenv("SEED_BIG_FILE_MB", raising=False)
        assert corpus._big_file_mb() == 0

    def test_a_nonsense_setting_disables_it_rather_than_crashing(self, monkeypatch):
        monkeypatch.setenv("SEED_BIG_FILE_MB", "thirty")
        assert corpus._big_file_mb() == 0

    def test_a_negative_setting_disables_it(self, monkeypatch):
        monkeypatch.setenv("SEED_BIG_FILE_MB", "-5")
        assert corpus._big_file_mb() == 0

    def test_asking_for_it_turns_it_on(self, monkeypatch):
        monkeypatch.setenv("SEED_BIG_FILE_MB", "30")
        assert corpus._big_file_mb() == 30


class TestTheFileIsActuallyOversized:
    def test_thirty_megabytes_clears_gmails_ceiling(self):
        assert len(corpus._big_blob(30)) > 25 * 1024 * 1024

    def test_the_size_is_what_was_asked_for(self):
        assert len(corpus._big_blob(4)) == 4 * 1024 * 1024

    def test_the_blob_is_shared_not_regenerated(self):
        """At 30 MB across 18 concurrent users, a per-user blob would be
        half a gigabyte of identical bytes on a box whose worker count is
        already bounded by memory."""
        assert corpus._big_blob(7) is corpus._big_blob(7)


class TestTheMailLooksLikeGmailsOwn:
    def test_it_uses_the_converted_attachment_shape(self):
        body = S._big_attachment_body("aBcD1234567890_-xyzABCdef")
        assert "drive.google.com/file/d/aBcD1234567890_-xyzABCdef/view" in body
        assert "usp=drive_web" in body

    def test_the_migrator_rewrites_that_exact_link(self):
        """The point of the whole case. A shape the seeder emits and the
        rewriter misses would leave migrated mail pointing at a source
        tenant that is about to be deleted -- and it would look fine."""
        import link_rewrite

        fid = "aBcD1234567890_-xyzABCdef"
        body = S._big_attachment_body(fid).encode()
        out, hits = link_rewrite.rewrite_bytes(body, lambda s: "TGT_" + s)
        assert hits == 1, "the rewriter did not match Gmail's own link shape"
        assert b"TGT_" + fid.encode() in out
        assert fid.encode() not in out.replace(b"TGT_" + fid.encode(), b"")


class TestTheFlagReachesTheSeeder:
    def _argv(self, **body):
        import webui
        from config import Settings

        st = Settings()
        return webui.seed_argv(dict({"confirm_domain": st.source_domain,
                                     "scale": "small"}, **body))

    def test_it_is_passed_when_asked_for(self):
        argv, _env, err = self._argv(big_file_mb=30)
        assert not err
        assert "--big-file-mb" in argv and "30" in argv

    def test_it_is_absent_by_default(self):
        argv, _env, err = self._argv()
        assert not err and "--big-file-mb" not in argv

    def test_a_nonsense_value_is_refused_not_ignored(self):
        _argv, _env, err = self._argv(big_file_mb="huge")
        assert "must be a number" in err

    def test_zero_means_off(self):
        argv, _env, err = self._argv(big_file_mb=0)
        assert not err and "--big-file-mb" not in argv

    def test_the_seeder_accepts_the_flag_it_is_given(self):
        """A flag argparse rejects is exit 2 before a single user is seeded."""
        import ast

        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "seed_sandbox.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        flags = {a.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "add_argument"
                 for a in n.args if isinstance(a, ast.Constant)}
        assert "--big-file-mb" in flags
