"""Adding a machine meant carrying the control plane's credential by hand.

The node token is 43 characters, long-lived, and the SAME secret for every
node -- so copying it from a page on one computer onto another leaves the
credential for the whole control plane in a clipboard, usually a chat
message, and often a screenshot. Reported as "can you make this any easier".

A join code is read off the screen, typed once, and dead in fifteen minutes.
Same model as Tailscale auth keys and kubeadm tokens. The properties that
make it safe to hand out that casually are the ones tested here.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

import accounts_auth as aa
import control_plane_db as cpdb
import join_codes as jc
from db import MigrationDB


@pytest.fixture
def db(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("MIGRATION_DB", path)
    MigrationDB(path)
    cpdb.apply_migrations()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def account(db):
    return aa.create_account("ops@x.test", "hunter2hunter2", "Ops")


class TestTheCodeIsUsableByAHuman:
    def test_it_is_short_and_grouped(self, account):
        code, _ = jc.create(account)
        assert len(code) == 9          # XXXX-XXXX
        assert code[4] == "-"

    def test_the_alphabet_excludes_the_lookalikes(self, account):
        """I/1 and O/0 are the two pairs people mistype off a screen, and U
        is dropped so a generated code cannot spell something unfortunate."""
        for ch in "ILOU":
            assert ch not in jc.ALPHABET

    def test_it_accepts_what_someone_will_actually_type(self, account):
        # A fresh code per variant: they are single-use, so reusing one
        # would test the replay guard rather than the parsing.
        for shape in (str.lower,
                      lambda c: c.replace("-", ""),
                      lambda c: f"  {c}  ",
                      lambda c: c.lower().replace("-", " ")):
            code, _ = jc.create(account)
            assert jc.redeem(shape(code)) == account

    def test_lookalikes_are_folded_rather_than_rejected(self):
        """Typing O for 0 is the mistake the alphabet is designed around;
        punishing it with "no such code" would waste the design."""
        assert jc.normalise("O1IL") == jc.normalise("0111")


class TestItIsNotStoredInTheClear:
    def test_the_database_never_holds_the_code(self, account):
        code, _ = jc.create(account)
        with cpdb.ro() as conn:
            rows = conn.execute("SELECT * FROM join_codes").fetchall()
        blob = " ".join(str(dict(r)) for r in rows)
        assert jc.normalise(code) not in blob.upper()

    def test_but_the_hash_resolves_it(self, account):
        code, _ = jc.create(account)
        assert jc.redeem(code) == account


class TestItCannotBeSpentTwice:
    def test_a_second_use_is_refused(self, account):
        """The script it serves contains the real node token. A replayable
        code is the long-lived token again, with extra steps."""
        code, _ = jc.create(account)
        assert jc.redeem(code) == account
        with pytest.raises(jc.JoinCodeError, match="already been used"):
            jc.redeem(code)

    def test_used_is_distinguished_from_unknown(self, account):
        """A code that worked a minute ago and does not now is a different
        problem from a typo, and the operator has to know which."""
        code, _ = jc.create(account)
        jc.redeem(code)
        with pytest.raises(jc.JoinCodeError, match="already been used"):
            jc.redeem(code)
        with pytest.raises(jc.JoinCodeError, match="no such join code"):
            jc.redeem("ZZZZ-ZZZZ")

    def test_it_is_marked_used_before_anything_is_returned(self):
        """Two machines racing one code must not both be handed the token,
        so the check and the mark are one write transaction."""
        import inspect
        src = inspect.getsource(jc.redeem)
        assert "with cpdb.rw() as conn:" in src
        assert src.index("UPDATE join_codes SET used_at") < src.index("return int(")


class TestItExpires:
    def test_an_expired_code_is_refused(self, account):
        code, _ = jc.create(account, lifetime_s=-1)
        with pytest.raises(jc.JoinCodeError, match="expired"):
            jc.redeem(code)

    def test_the_default_window_is_short(self):
        assert jc.LIFETIME_S <= 30 * 60

    def test_expiry_is_checked_against_now_not_creation(self, account):
        code, expires = jc.create(account, lifetime_s=2)
        assert expires > time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        assert jc.redeem(code) == account


class TestGuessingIsBounded:
    def test_repeated_failures_lock_the_address_out(self, account):
        """Redemption is unauthenticated by necessity -- the joining machine
        has no credential yet. An endpoint that hands out a live token must
        not answer unlimited wrong guesses."""
        for _ in range(jc.MAX_FAILURES):
            with pytest.raises(jc.JoinCodeError):
                jc.redeem("ZZZZ-ZZZZ", addr="10.0.0.9")
        good, _ = jc.create(account)
        with pytest.raises(jc.JoinCodeError, match="too many bad codes"):
            jc.redeem(good, addr="10.0.0.9")

    def test_one_address_cannot_lock_out_another(self, account):
        for _ in range(jc.MAX_FAILURES):
            with pytest.raises(jc.JoinCodeError):
                jc.redeem("ZZZZ-ZZZZ", addr="10.0.0.9")
        good, _ = jc.create(account)
        assert jc.redeem(good, addr="10.0.0.10") == account

    def test_codes_have_real_entropy(self):
        """The throttle is belt and braces; the code itself is the defence."""
        import math
        bits = jc.GROUPS * jc.GROUP_LEN * math.log2(len(jc.ALPHABET))
        assert bits >= 40

    def test_two_codes_are_never_the_same(self, account):
        seen = {jc.create(account)[0] for _ in range(50)}
        assert len(seen) == 50


class TestTheEndpoints:
    def test_minting_is_superadmin_only(self):
        """Same rule as /nodes/join: the token this leads to is one shared
        secret for the whole control plane, not a per-account one."""
        import inspect

        import api_server
        src = inspect.getsource(api_server.create_join_code)
        assert "require_superadmin(op)" in src
        assert "_require_account_access(account_id, op)" in src

    def test_redeeming_takes_no_credential(self):
        """It cannot: collecting one is the point. Everything that makes
        that safe lives in the code itself."""
        import inspect

        import api_server
        sig = inspect.signature(api_server.redeem_join_code)
        assert "op" not in sig.parameters
        src = inspect.getsource(api_server.redeem_join_code)
        assert "join_codes.redeem(code, addr=addr)" in src

    def test_the_served_script_is_never_cached(self):
        """It contains a live token; a proxy or browser keeping it would
        outlive the single use that bounds it."""
        import inspect

        import api_server
        src = inspect.getsource(api_server.redeem_join_code)
        assert '"Cache-Control": "no-store"' in src

    def test_the_coordinator_url_comes_from_the_request(self):
        """By definition an address the joining machine can reach -- it just
        reached it. BITPORT_PUBLIC_ORIGIN is empty on every LAN and tailnet
        install, which is exactly where this is needed."""
        import inspect

        import api_server
        src = inspect.getsource(api_server.redeem_join_code)
        assert "x-forwarded-proto" in src
        assert "x-forwarded-host" in src

    def test_it_refuses_when_nodes_are_not_enabled(self):
        import inspect

        import api_server
        src = inspect.getsource(api_server.redeem_join_code)
        assert "BITPORT_NODE_TOKEN" in src
        assert "503" in src
