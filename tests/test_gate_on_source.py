"""A run checks the source answers Drive before it writes anything (live: a lapsed source
answered every user 401 and a full run stamped 292 users BLOCKED)."""
from types import SimpleNamespace

import pytest

import main


class _Auth:
    def __init__(self, exc=None):
        self.exc, self.asked = exc, []

    def api(self, side, name, user):
        self.asked.append((side, name, user))
        outer = self

        class _Req:
            def execute(self):
                if outer.exc:
                    raise outer.exc
                return {"user": {"emailAddress": user}}
        return SimpleNamespace(about=lambda: SimpleNamespace(get=lambda **k: _Req()))


def _settings(**kw):
    return SimpleNamespace(source_admin="info@src", dry_run=False, **kw)


def test_a_tenant_wide_401_stops_the_run(capsys):
    auth = _Auth(RuntimeError('<HttpError 401 ... "Active session is invalid">'))
    with pytest.raises(SystemExit) as e:
        main._gate_on_source(auth, _settings())
    assert e.value.code == 2
    assert "SOURCE REFUSED" in capsys.readouterr().out
    assert auth.asked == [("source", "drive", "info@src")]


def test_any_other_error_lets_the_run_start(capsys):
    main._gate_on_source(_Auth(TimeoutError("read timed out")), _settings())
    assert "SOURCE REFUSED" not in capsys.readouterr().out


def test_a_healthy_source_says_so(capsys):
    main._gate_on_source(_Auth(), _settings())
    assert "SOURCE CHECK" in capsys.readouterr().out


def test_dry_run_and_no_admin_skip_the_check():
    auth = _Auth(RuntimeError("HttpError 401"))
    main._gate_on_source(auth, SimpleNamespace(source_admin="info@src", dry_run=True))
    main._gate_on_source(auth, SimpleNamespace(source_admin="", dry_run=False))
    assert auth.asked == []
