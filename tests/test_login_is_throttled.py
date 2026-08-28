"""The login had no limit on attempts. None at all.

No counter, no lockout, no delay -- against accounts that hold customers'
Workspace super-admin service-account keys. PBKDF2 at 200,000 iterations
helps against offline cracking and hurts here: unlimited attempts on a
2-core box is a CPU-exhaustion vector as well as a guessing one.

The second bug was subtler. authenticate() promised that "no such email"
and "wrong password" both return None so the form cannot enumerate
accounts -- and then returned early for a missing email, before any KDF
ran. A stopwatch told them apart: microseconds versus 200,000 iterations.
"""
import time

import pytest

import accounts_auth as aa


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """A real control-plane database, per test."""
    import control_plane_db as cpdb
    db = str(tmp_path / "cp.db")
    monkeypatch.setattr(cpdb, "_db_path", lambda: db)
    cpdb.apply_migrations(db)
    monkeypatch.setattr(aa, "MAX_LOGIN_ATTEMPTS", 3)
    monkeypatch.setattr(aa, "LOGIN_LOCKOUT_SECONDS", 60)
    yield db


def _account(email="a@example.com", pw="correct-horse"):
    return aa.create_account(email, pw, "Test")


class TestTheAccountLocks:
    def test_the_right_password_works(self):
        aid = _account()
        assert aa.authenticate("a@example.com", "correct-horse") == aid

    def test_wrong_passwords_eventually_lock_it(self):
        _account()
        for _ in range(3):
            assert aa.authenticate("a@example.com", "wrong") is None
        assert aa.login_locked_for("a@example.com") > 0

    def test_a_locked_account_refuses_the_RIGHT_password(self):
        """Otherwise the lock costs an attacker nothing -- they keep
        guessing and the one correct guess still lands."""
        _account()
        for _ in range(3):
            aa.authenticate("a@example.com", "wrong")
        assert aa.authenticate("a@example.com", "correct-horse") is None

    def test_a_success_before_the_limit_clears_the_count(self):
        # Someone who mistypes twice this morning must not be closer to a
        # lockout all day.
        _account()
        aa.authenticate("a@example.com", "wrong")
        aa.authenticate("a@example.com", "wrong")
        assert aa.authenticate("a@example.com", "correct-horse")
        for _ in range(2):
            assert aa.authenticate("a@example.com", "wrong") is None
        assert aa.login_locked_for("a@example.com") == 0

    def test_the_lock_expires(self, monkeypatch):
        _account()
        monkeypatch.setattr(aa, "LOGIN_LOCKOUT_SECONDS", -1)
        for _ in range(3):
            aa.authenticate("a@example.com", "wrong")
        assert aa.login_locked_for("a@example.com") == 0

    def test_locking_one_account_does_not_lock_another(self):
        _account("a@example.com")
        _account("b@example.com")
        for _ in range(3):
            aa.authenticate("a@example.com", "wrong")
        assert aa.authenticate("b@example.com", "correct-horse")


class TestItStillDoesNotLeakWhoExists:
    def test_both_failures_return_the_same_none(self):
        _account()
        assert aa.authenticate("a@example.com", "wrong") is None
        assert aa.authenticate("nobody@example.com", "wrong") is None

    def test_an_unknown_email_costs_the_same_work(self):
        """The promise the docstring already made, now actually true. An
        early return made it measurable with a stopwatch."""
        _account()

        def took(email):
            t = time.perf_counter()
            aa.authenticate(email, "wrong")
            return time.perf_counter() - t

        known = min(took("a@example.com") for _ in range(3))
        # Fresh unknown addresses so no lockout accumulates on one of them.
        unknown = min(took(f"nobody{i}@example.com") for i in range(3))
        ratio = max(known, unknown) / max(min(known, unknown), 1e-9)
        assert ratio < 4, (
            f"unknown-email path is {ratio:.1f}x different -- that is an "
            f"account-enumeration oracle (known={known:.4f}s "
            f"unknown={unknown:.4f}s)")

    def test_a_dummy_hash_exists_to_verify_against(self):
        assert aa._DUMMY_HASH and "$" in aa._DUMMY_HASH or aa._DUMMY_HASH


class TestTheWindowResets:
    def test_failures_older_than_the_window_do_not_accumulate(self, monkeypatch):
        # Two wrong guesses a week apart should not add up to a lockout.
        _account()
        monkeypatch.setattr(aa, "LOGIN_LOCKOUT_SECONDS", -1)
        for _ in range(5):
            aa.authenticate("a@example.com", "wrong")
        assert aa.login_locked_for("a@example.com") == 0

    def test_a_corrupt_timestamp_does_not_lock_anyone_out_forever(self):
        _account()
        import control_plane_db as cpdb
        aa.authenticate("a@example.com", "wrong")
        with cpdb.rw() as conn:
            conn.execute("UPDATE login_attempts SET locked_until='not-a-date'")
        assert aa.login_locked_for("a@example.com") == 0


class TestTheEndpointDoesNotAnnounceTheLock:
    """"This account is locked" confirms the account exists just as loudly
    as "wrong password" would.

    So the API says the same thing for all three cases and logs the real
    reason server-side: the operator can see it, the internet cannot. A
    locked-out legitimate user gets no explanation, which is the accepted
    cost of not having an enumeration oracle.
    """

    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return open(os.path.join(root, "api_server.py"), encoding="utf-8").read()

    def test_one_message_covers_every_failure(self):
        block = self._src().split("async def auth_login")[1][:1400]
        assert block.count('raise HTTPException(401') == 1
        assert '"wrong email or password"' in block

    def test_the_lock_is_logged_not_returned(self):
        block = self._src().split("async def auth_login")[1][:1400]
        assert "login_locked_for" in block
        assert "log.warning" in block

    def test_logging_cannot_break_the_login(self):
        block = self._src().split("async def auth_login")[1][:1400]
        assert "never fail a login on logging" in block
