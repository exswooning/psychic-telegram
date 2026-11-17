"""fableplan2 P1.6: the seeder makes the same content for the same seed, in every process.

It seeded its random numbers from hash(user), which Python salts per process, so a rerun made different
mail and no two runs could be compared (measured 16 Nov 2026: 8543 and 9792 for one address)."""
import base64
import os
import re
import subprocess
import sys

import pytest

import corpus
import seed_sandbox as seed
from fakes import FakeGmail

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


UPLOADS = []


class _Media:
    """Stands in for the upload object (the real one needs a live request to read back)."""
    def __init__(self, data, mimetype):
        self.data, self.mimetype = data, mimetype
        UPLOADS.append((len(data), mimetype))

    def read_all(self):
        return self.data


@pytest.fixture(autouse=True)
def _fake_media(monkeypatch):
    UPLOADS.clear()
    monkeypatch.setattr(seed, "_media", _Media)


def _in_a_new_process(code: str, hashseed: str, **env) -> str:
    return subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, check=True,
                          env={**os.environ, "PYTHONHASHSEED": hashseed, "PYTHONPATH": f"{HERE}{os.pathsep}{ROOT}", **env}
                          ).stdout.strip()


def test_the_seed_is_the_same_in_every_process():
    code = "import corpus;print(corpus.stable_seed('alice@x.test'), corpus.stable_seed('alice@x.test', 7))"
    assert len({_in_a_new_process(code, h) for h in ("1", "2", "3", "random")}) == 1


def test_seed_content_key_and_salt_each_change_the_stream(monkeypatch):
    monkeypatch.delenv("SEED_CAMPAIGN", raising=False)
    monkeypatch.delenv("SEED_CONTENT_KEY", raising=False)
    base = corpus.stable_seed("a@x.test")
    assert corpus.stable_seed("b@x.test") != base and corpus.stable_seed("a@x.test", 7) != base
    monkeypatch.setenv("SEED_CAMPAIGN", "1")
    assert corpus.stable_seed("a@x.test") != base
    monkeypatch.setenv("SEED_CONTENT_KEY", "twin")
    assert corpus.stable_seed("a@x.test") == corpus.stable_seed("b@x.test")


def test_no_code_path_seeds_from_hash():
    for name in ("corpus.py", "seed_sandbox.py"):
        for n, line in enumerate(open(os.path.join(HERE, name), encoding="utf-8"), 1):
            code = line.split("#", 1)[0]
            assert not re.search(r"\bhash\(", code), f"{name}:{n} {line.strip()}"


def _mail(user, settings, count=40):
    gmail = FakeGmail(user, "source")
    peers = ["p1@tenanta.com", "p2@tenanta.com", "p3@tenanta.com"]
    seed.seed_gmail(gmail, settings, user, peers, "ext@example.com", count=count)
    out = []
    for msg in gmail.messages.values():
        raw = base64.urlsafe_b64decode(msg["raw"].encode()).decode("utf-8", "replace")
        subj = re.search(r"^Subject: (.*)$", raw, re.M)
        out.append((subj.group(1) if subj else "", len(raw), tuple(sorted(msg.get("labelIds") or []))))
    return sorted(out)


def test_twins_hold_the_same_mail_and_other_users_do_not(settings, monkeypatch):
    monkeypatch.setenv("SEED_CAMPAIGN", "5")
    monkeypatch.setenv("SEED_CONTENT_KEY", "twin")
    a, b = _mail("t01@tenanta.com", settings), _mail("t02@tenanta.com", settings)
    assert a and [x[0] for x in a] == [x[0] for x in b]
    monkeypatch.delenv("SEED_CONTENT_KEY")
    assert [x[0] for x in _mail("c01@tenanta.com", settings)] != [x[0] for x in _mail("c02@tenanta.com", settings)]


def test_the_same_seed_repeats_and_another_seed_differs(settings, monkeypatch):
    monkeypatch.delenv("SEED_CONTENT_KEY", raising=False)
    monkeypatch.setenv("SEED_CAMPAIGN", "5")
    first = _mail("c01@tenanta.com", settings)
    assert _mail("c01@tenanta.com", settings) == first
    monkeypatch.setenv("SEED_CAMPAIGN", "6")
    assert _mail("c01@tenanta.com", settings) != first


def test_mail_goes_up_as_its_own_bytes_not_as_base64_in_json(settings, monkeypatch):
    """P1.7: a third fewer bytes on a line whose upload is the limit. Every message is a message/rfc822
    upload; the labels alone travel in the JSON body."""
    monkeypatch.delenv("SEED_CONTENT_KEY", raising=False)
    gmail = FakeGmail("c01@tenanta.com", "source")
    seed.seed_gmail(gmail, settings, "c01@tenanta.com", ["p1@tenanta.com"], "ext@example.com", count=20)
    assert 0 < len(UPLOADS) <= len(gmail.messages)          # the plain-ASCII ones; non-ASCII ones keep the JSON route
    assert {mime for _, mime in UPLOADS} == {"message/rfc822"}


def test_a_message_with_non_ascii_bytes_still_goes_up_as_base64_json(settings, monkeypatch):
    """The raw upload cannot carry them (googleapiclient's multipart: 'ascii' codec can't encode)."""
    monkeypatch.delenv("SEED_CONTENT_KEY", raising=False)
    gmail = FakeGmail("c01@tenanta.com", "source")
    seed.seed_gmail(gmail, settings, "c01@tenanta.com", ["p1@tenanta.com"], "ext@example.com", count=40)
    assert len(gmail.messages) >= 40
    n_media = len(UPLOADS)
    assert 0 < n_media <= len(gmail.messages)
    import base64 as b64
    non_ascii = [m for m in gmail.messages.values() if not b64.urlsafe_b64decode(m["raw"].encode()).isascii()]
    assert len(UPLOADS) + len(non_ascii) == len(gmail.messages)      # each message took exactly one route
