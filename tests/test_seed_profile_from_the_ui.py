"""A profile seed (P4.2) starts from the web UI's seed endpoint, so it runs as a registered `seed` job the
source guard knows about (operator, 23 Nov 2026: test 15% of the profile's mail)."""
import pytest

import webui


@pytest.fixture
def body():
    from config import Settings
    return {"confirm_domain": Settings().source_domain, "create_users": True}


def test_a_profile_seed_passes_its_numbers(body):
    argv, _env, err = webui.seed_argv(dict(body, profile="org45", profile_users=5, seed_number=1, mail_share=0.15,
                                           upload_mb_per_sec=0.9, max_hours=12))
    assert not err, err
    i = argv.index("--profile")
    assert argv[i + 1].endswith("data-generator/profiles/org45.json")
    for flag, value in (("--users", "5"), ("--seed", "1"), ("--mail-share", "0.15"), ("--upload-mb-per-sec", "0.9")):
        assert argv[argv.index(flag) + 1] == value
    assert "--create-users" in argv


@pytest.mark.parametrize("bad", [{"profile": "../etc"}, {"profile": "nope"}, {"profile": "org45", "profile_users": 0},
                                 {"profile": "org45", "profile_users": 5, "mail_share": 2}])
def test_a_bad_profile_request_is_refused(body, bad):
    assert webui.seed_argv(dict(body, **bad))[2]
