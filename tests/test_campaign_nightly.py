"""The nightly run leaves ONE line for the morning, and its units start nothing on their own."""
import configparser
import os

import campaign
import campaign_nightly as N

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fake_campaign(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(campaign, "results_dir", lambda name: str(tmp_path / name))
    monkeypatch.setattr(N, "INBOX", str(tmp_path / "monitor" / "INBOX.md"))

    def fake_main(argv):
        for r in rows:
            campaign.append_result("nightly", r)
        return 0
    monkeypatch.setattr(campaign, "main", fake_main)


def test_one_line_with_what_failed_and_what_is_left(monkeypatch, tmp_path):
    _fake_campaign(monkeypatch, tmp_path, [{"id": "a", "status": "PASS"}, {"id": "b", "status": "FAIL"},
                                           {"id": "c", "status": "UNVERIFIED"}])
    assert N.main(["--account-id", "5"]) == 0          # a FAIL is a result, not a crash of the unit
    text = open(N.INBOX).read()
    assert text.count("\n") == 1 and "NIGHTLY CAMPAIGN nightly (account 5)" in text
    assert "1 FAIL" in text and "1 PASS" in text and "FAILED b" in text and "not yet passing" in text


def test_a_second_night_reports_only_its_own_results(monkeypatch, tmp_path):
    _fake_campaign(monkeypatch, tmp_path, [{"id": "a", "status": "PASS"}])
    N.main(["--account-id", "5"])
    _fake_campaign(monkeypatch, tmp_path, [])
    N.main(["--account-id", "5"])
    lines = open(N.INBOX).read().splitlines()
    assert len(lines) == 2 and "nothing to run" in lines[1] and "PASS" not in lines[1]


def test_a_crashed_campaign_still_reports(monkeypatch, tmp_path):
    _fake_campaign(monkeypatch, tmp_path, [])
    monkeypatch.setattr(campaign, "main", lambda argv: 1 / 0)
    assert N.main(["--account-id", "5"]) == 0
    assert "the campaign exited 1" in open(N.INBOX).read()


def test_the_units_are_a_timer_service_pair_that_runs_no_privileged_scenario():
    d = os.path.join(HERE, "systemd")
    timer = configparser.ConfigParser(strict=False, interpolation=None)
    timer.optionxform = str
    timer.read(os.path.join(d, "bitport-campaign-nightly@.timer"))
    assert timer["Timer"]["Persistent"] == "true" and "OnCalendar" in timer["Timer"]
    svc = open(os.path.join(d, "bitport-campaign-nightly@.service")).read()
    execs = [line for line in svc.splitlines() if line.startswith("ExecStart=")]
    assert len(execs) == 1 and "campaign_nightly.py --account-id %i" in execs[0]
    assert "--allow" not in execs[0] and "--redo" not in execs[0]
