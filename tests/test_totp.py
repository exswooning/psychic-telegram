"""One-time passwords, so the browser can answer its own 2-Step prompt.

The setup wizard drives a real browser through Google's sign-in, and a
2-Step prompt stops it dead -- install_node's own note says "watch out for
a 2-Step prompt on your phone: the sign-in cannot answer it". Every
unattended setup ended by waiting for a human holding a phone.

An authenticator secret is the one second factor a program can present.
Implemented on the standard library because requirements.txt is
deliberately stdlib-plus-google-client -- a worker node installs exactly
that, and a one-time password is thirty lines rather than a dependency.
"""
from __future__ import annotations

import base64
import inspect
import re

import pytest

import totp

# RFC 6238 Appendix B. The shared secret is the ASCII string
# "12345678901234567890"; these are the published expected values.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTORS = [
    (59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
    (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130"),
]


class TestItAgreesWithTheSpecification:
    @pytest.mark.parametrize("when,expected", RFC_VECTORS)
    def test_rfc_6238_vectors(self, when, expected):
        """Checked against the RFC's own published values rather than
        against itself. A hand-rolled HMAC that is confidently wrong looks
        exactly like one that is right until a real sign-in rejects it."""
        assert totp.code_at(RFC_SECRET, when=when) == expected

    def test_the_code_changes_with_the_window(self):
        a = totp.code_at(RFC_SECRET, when=1111111109)
        b = totp.code_at(RFC_SECRET, when=1111111109 + totp.PERIOD)
        assert a != b

    def test_it_is_stable_within_a_window(self):
        # Anchored to a window BOUNDARY. 1000 and 1029 look like the same
        # 30 seconds and are not: 1000//30 is 33 and 1029//30 is 34, so the
        # first version of this asserted that two different windows produce
        # the same code, and was right to fail.
        base = 1000 - (1000 % totp.PERIOD)
        assert (totp.code_at(RFC_SECRET, when=base)
                == totp.code_at(RFC_SECRET, when=base + totp.PERIOD - 1))

    def test_it_is_always_six_digits(self):
        """Including when the value has leading zeros -- 005924 is in the
        vectors above precisely because that case is easy to get wrong."""
        for when, _ in RFC_VECTORS:
            code = totp.code_at(RFC_SECRET, when=when)
            assert len(code) == 6 and code.isdigit()


class TestItAcceptsWhatPeopleActuallyPaste:
    def test_googles_spaced_lowercase_form(self):
        """The 2-Step page prints the seed in lowercase groups of four.
        Rejecting it would send somebody to reformat a secret by hand, which
        is how a character gets dropped."""
        assert totp.normalise("abcd efgh ijkl mnop") == "ABCDEFGHIJKLMNOP"

    def test_an_otpauth_uri(self):
        assert totp.normalise(
            "otpauth://totp/X:a@b.com?secret=JBSWY3DPEHPK3PXP&issuer=X"
        ) == "JBSWY3DPEHPK3PXP"

    def test_it_pads_for_base32(self):
        """Google omits the padding; base32 needs a multiple of eight."""
        out = totp.normalise("JBSWY3DPEHPK3PX")
        assert len(out) % 8 == 0

    def test_both_forms_give_the_same_code(self):
        plain = totp.code_at("JBSWY3DPEHPK3PXP", when=59)
        spaced = totp.code_at("jbsw y3dp ehpk 3pxp", when=59)
        assert plain == spaced


class TestTheSecretsFile:
    def test_it_lives_outside_the_database(self, tmp_path):
        """migration.db is copied to worker nodes and taken in backups. A
        node gets a code by ASKING the coordinator, never by holding a seed."""
        assert totp.SECRETS_FILE.startswith("/etc/bitport")
        # Docstring stripped: it EXPLAINS why the database is the wrong
        # place, so an "is it absent" check against the raw text finds the
        # explanation and fails -- a test bug that pushes someone to delete
        # the reasoning.
        src = inspect.getsource(totp.load_secrets)
        src = re.sub(r'""".*?"""', "", src, flags=re.S)
        assert "migration.db" not in src and "cpdb" not in src

    def test_saving_leaves_it_owner_only(self, tmp_path):
        p = tmp_path / "totp.env"
        totp.save_secret("a@b.test", "JBSWY3DPEHPK3PXP", path=str(p))
        assert oct(p.stat().st_mode)[-3:] == "600"

    def test_a_bad_secret_is_rejected_before_it_is_stored(self, tmp_path):
        """Otherwise it is discovered at a sign-in, minutes into a run that
        is now blocked on the thing it was meant to unblock."""
        p = tmp_path / "totp.env"
        with pytest.raises(Exception):
            totp.save_secret("a@b.test", "not base32 at all !!", path=str(p))
        assert not p.exists()

    def test_a_second_account_does_not_replace_the_first(self, tmp_path):
        p = tmp_path / "totp.env"
        totp.save_secret("a@b.test", "JBSWY3DPEHPK3PXP", path=str(p))
        totp.save_secret("c@d.test", RFC_SECRET, path=str(p))
        got = totp.load_secrets(str(p))
        assert set(got) == {"a@b.test", "c@d.test"}

    def test_replacing_one_keeps_it_single(self, tmp_path):
        p = tmp_path / "totp.env"
        totp.save_secret("a@b.test", "JBSWY3DPEHPK3PXP", path=str(p))
        totp.save_secret("a@b.test", RFC_SECRET, path=str(p))
        assert len(totp.load_secrets(str(p))) == 1

    def test_an_unknown_account_returns_nothing_rather_than_a_wrong_code(self, tmp_path):
        assert totp.code_for("nobody@x.test", path=str(tmp_path / "none")) is None

    def test_the_file_says_what_it_costs(self, tmp_path):
        """Read by whoever finds it later. A seed beside the password is one
        factor, not two."""
        p = tmp_path / "totp.env"
        totp.save_secret("a@b.test", "JBSWY3DPEHPK3PXP", path=str(p))
        assert "ONE factor, not two" in p.read_text()


class TestTheEndpointsGuardIt:
    def test_reading_a_code_is_superadmin_only(self):
        """It is the second factor for an account that can administer a
        Google tenant. Handing it to any signed-in caller would make the
        session cookie sufficient for both factors."""
        import api_server
        src = inspect.getsource(api_server.mfa_code)
        assert "require_superadmin(op)" in src

    def test_storing_a_seed_is_superadmin_only(self):
        import api_server
        src = inspect.getsource(api_server.mfa_store_secret)
        assert "require_superadmin(op)" in src

    def test_the_audit_row_does_not_contain_the_secret(self):
        """The point of the record is that somebody added a second factor to
        the machine, not what it was."""
        import api_server
        src = inspect.getsource(api_server.mfa_store_secret)
        i = src.index("begin_action")
        assert "req.secret" not in src[i:]

    def test_the_time_left_is_returned_with_the_code(self):
        """A code with two seconds on it is rejected by the time it is
        typed."""
        import api_server
        src = inspect.getsource(api_server.mfa_code)
        assert "secondsRemaining" in src


class TestNoNewDependency:
    def test_it_is_standard_library_only(self):
        src = inspect.getsource(totp)
        for lib in ("pyotp", "oathtool", "cryptography", "passlib"):
            assert lib not in src, lib
        assert re.search(r"^import hmac$", src, re.M)
        assert re.search(r"^import hashlib$", src, re.M)


class TestEnrolment:
    """A QR scannable by Google Authenticator, and the key behind it."""

    def test_re_enrolling_returns_the_same_seed(self, tmp_path):
        """Rotating it silently would leave the phone holding the old one,
        and the machine would then wipe itself on schedule because the codes
        stopped matching -- the worst possible way for an enrolment bug to
        surface."""
        p = str(tmp_path / "t.env")
        first = totp.enrol("deadman@bitport", path=p)
        assert totp.enrol("deadman@bitport", path=p)["secret"] == first["secret"]

    def test_the_uri_is_what_an_authenticator_app_expects(self, tmp_path):
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        assert e["uri"].startswith("otpauth://totp/")
        assert f"secret={e['secret']}" in e["uri"]
        assert "issuer=Bitport" in e["uri"]

    def test_it_does_not_spell_out_the_defaults(self, tmp_path):
        """SHA1/6/30 are what every app assumes, and including them only
        makes the QR denser and harder for a camera to read off a screen."""
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        for noise in ("algorithm=", "digits=", "period="):
            assert noise not in e["uri"], noise

    def test_the_seed_it_hands_out_is_the_one_that_verifies(self, tmp_path):
        p = str(tmp_path / "t.env")
        e = totp.enrol("deadman@bitport", path=p)
        assert totp.code_for("deadman@bitport", p)[0] == totp.code_at(e["secret"])

    def test_the_setup_key_is_the_secret_in_readable_groups(self, tmp_path):
        """It gets typed by hand into a phone."""
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        assert e["setupKey"].replace(" ", "") == e["secret"]

    def test_the_seed_is_160_bits(self, tmp_path):
        """The RFC 4226 recommendation."""
        import base64
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        assert len(base64.b32decode(totp.normalise(e["secret"]))) == 20

    def test_the_qr_is_a_square_grid(self, tmp_path):
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        m = e["matrix"]
        assert m and all(len(row) == len(m) for row in m)

    def test_a_missing_encoder_costs_the_qr_and_not_the_enrolment(self, monkeypatch):
        """The setup key is typed into the same app and works without it."""
        import builtins
        real = builtins.__import__

        def no_qrcode(name, *a, **k):
            if name == "qrcode":
                raise ImportError("gone")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_qrcode)
        assert totp.qr_matrix("otpauth://totp/x?secret=Y") == []

    def test_the_secret_is_encoded_locally(self):
        """A QR service would be handed the second factor for a switch that
        destroys the machine."""
        import inspect
        src = inspect.getsource(totp.qr_matrix) + inspect.getsource(totp.enrol)
        assert "http" not in src.replace("otpauth", "")

    def test_the_quiet_zone_is_the_spec_minimum(self, tmp_path):
        """Four clear modules is how a scanner finds the symbol at all. At
        two, a screen QR looks perfectly fine to a person and a phone simply
        refuses to read it."""
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        m = e["matrix"]
        assert all(not any(row) for row in m[:4]), "top quiet zone"
        assert all(not any(row) for row in m[-4:]), "bottom quiet zone"
        assert all(not any(r[:4]) and not any(r[-4:]) for r in m), "side quiet zones"

    def test_the_matrix_already_carries_its_border(self, tmp_path):
        """So a caller that helpfully adds its own padding is not needed,
        and one that does not is still correct."""
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        assert len(e["matrix"]) >= 37 + 8

    def test_the_finder_patterns_are_where_a_camera_looks(self, tmp_path):
        e = totp.enrol("deadman@bitport", path=str(tmp_path / "t.env"))
        m = e["matrix"]; n = len(m)
        eye = [[1,1,1,1,1,1,1],[1,0,0,0,0,0,1],[1,0,1,1,1,0,1],[1,0,1,1,1,0,1],
               [1,0,1,1,1,0,1],[1,0,0,0,0,0,1],[1,1,1,1,1,1,1]]
        for ox, oy in ((4, 4), (n - 11, 4), (4, n - 11)):
            assert all(m[oy + y][ox + x] == bool(v)
                       for y, row in enumerate(eye)
                       for x, v in enumerate(row)), (ox, oy)


class TestQrForAnExistingSeed:
    """The Authenticator tab shows a QR to sync a phone. Unlike enrol(), it
    must never MINT a seed as a side effect of being looked at."""

    def test_it_returns_the_qr_for_a_stored_account(self, tmp_path):
        p = str(tmp_path / "t.env")
        totp.save_secret("admin@src.test", "ABCD2345ABCD2345ABCD2345ABCD2345", p)
        got = totp.qr_for("admin@src.test", path=p)
        assert got and got["uri"].startswith("otpauth://totp/")
        assert got["matrix"]

    def test_it_never_creates_one(self, tmp_path):
        """A "show QR" click is a read. Minting a second factor here would
        hand back one the operator then assumes was already there."""
        p = str(tmp_path / "t.env")
        assert totp.qr_for("nobody@x.test", path=p) is None
        assert "nobody@x.test" not in totp.load_secrets(p)

    def test_the_seed_it_shows_is_the_one_that_verifies(self, tmp_path):
        p = str(tmp_path / "t.env")
        e = totp.enrol("deadman@bitport", path=p)
        got = totp.qr_for("deadman@bitport", path=p)
        assert got["secret"] == e["secret"]
        assert totp.code_at(got["secret"]) == totp.code_for("deadman@bitport", p)[0]

    def test_enrol_and_qr_for_agree_on_the_uri(self, tmp_path):
        """They share one payload builder, so a fix to one cannot drift the
        other into producing an unscannable code."""
        p = str(tmp_path / "t.env")
        e = totp.enrol("deadman@bitport", path=p)
        assert totp.qr_for("deadman@bitport", path=p)["uri"] == e["uri"]
