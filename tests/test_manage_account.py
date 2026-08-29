"""A password reset that silently changed nothing would be worse than none."""
import contextlib

import pytest

import manage_account


def test_short_passwords_are_refused():
    with pytest.raises(ValueError):
        manage_account.set_password("someone@example.com", "short")


def test_unknown_email_raises_rather_than_reporting_success(monkeypatch):
    class _Conn:
        def execute(self, *a):
            class _R:
                def fetchone(self):
                    return None
            return _R()

    @contextlib.contextmanager
    def _rw():
        yield _Conn()

    monkeypatch.setattr(manage_account.cpdb, "rw", _rw)
    with pytest.raises(LookupError):
        manage_account.set_password("nobody@example.com", "long-enough-pw")


def test_env_file_parsing_matches_the_ui_env_shape(tmp_path):
    f = tmp_path / "ui.env"
    f.write_text("# comment\nBITPORT_EMAIL=a@b\nBITPORT_PASSWORD=s3cret=with=eq\n\n")
    got = manage_account.read_env_file(str(f))
    assert got["BITPORT_EMAIL"] == "a@b"
    assert got["BITPORT_PASSWORD"] == "s3cret=with=eq"   # only split once


def test_password_never_accepted_on_argv():
    """argv is readable by any process on the box via ps."""
    src = open("manage_account.py", encoding="utf-8").read()
    assert '"--password"' not in src
    assert "--password-file" in src
