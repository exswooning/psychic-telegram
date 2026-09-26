"""Seed reports: judged from a transcript, because a seed leaves nothing else.

The rules that matter are the same as for a migration: a run whose outcome is
unknown is never a pass, and "users not finished" is only a failure once the run
has ended -- mid-run it just means not yet."""
from __future__ import annotations

import pytest

import benchmarks as B
import run_report as RR
import seed_report as SR

HEAD = ["Seeding 4 users in x.com at scale 'small'", "Workers: 2 (2 users x 14 threads)"]
DONE = "  [{u}@x.com] done in 10.0s: 12 files, 3 events"


def lines(finished=4, failed_service=0, extra=()):
    out = list(HEAD)
    for i in range(4):
        out.append(f"  [u{i}@x.com] starting (Eng)")
    for i in range(finished):
        tail = " (chat failed (HTTP 404))" if i < failed_service else ""
        out.append(DONE.format(u=f"u{i}") + tail)
    return out + list(extra)


def verdict(facts, overrides=None):
    return B.evaluate(facts, overrides, B.for_kind("seed"))


class TestParsing:
    def test_counts_users_services_and_warnings(self):
        p = SR.parse(lines(3, 1, ["  ! chat for u0@x.com: HTTP 404 (NOT_FOUND): boom"]))
        assert p["head"]["totalUsers"] == 4 and p["head"]["workers"] == 2
        assert sum(1 for u in p["users"].values() if u["status"] == "done") == 3
        assert p["users"]["u0@x.com"]["failedServices"] == ["chat"]
        assert p["warnings"][0]["kind"] == "chat" and p["warnings"][0]["code"] == "HTTP 404 (NOT_FOUND)"

    def test_records_spliced_into_one_line_are_pulled_apart(self):
        p = SR.parse(["  [a@x.com] starting (Eng)  ! b@x.com FAILED: boom  [c@x.com] starting (Ops)"])
        assert set(p["users"]) == {"a@x.com", "c@x.com"} and p["warnings"][0]["kind"] == "b@x.com"

    def test_a_fill_run_reads_storage_and_the_heartbeats(self):
        p = SR.parse(["Topping up storage for 300 user(s) toward 100% of each account's own storage share ...",
                      "  [a@x.com] top-up in 9.0s: 1.0GB -> 30.0GB (12 filler file(s))",
                      "  ... still topping up: 1/300 users done after 40m00s (12 in flight) -- 328.6 GB uploaded of 383 GB planned"])
        assert p["head"]["mode"] == "fill" and p["users"]["a@x.com"]["storage"] == (1.0, 30.0)
        assert p["fill"] == [(2400, 328.6, 383.0)]

    def test_an_old_heartbeat_with_no_upload_figures_is_not_a_fill_sample(self):
        assert SR.parse(["  ... still topping up: 0/300 users done after 1m00s (12 in flight)"])["fill"] == []


class TestFacts:
    def test_a_clean_finished_run(self):
        seed, fam = SR.facts(lines(4), ended=True)
        assert seed["finished"] == 4 and seed["finishedShare"] == 1.0 and seed["neverFinished"] == 0 and fam == []

    def test_never_finished_is_only_judged_once_the_run_has_ended(self):
        mid, _ = SR.facts(lines(2), ended=False)
        end, _ = SR.facts(lines(2), ended=True)
        assert mid["neverFinished"] is None and end["neverFinished"] == 2

    def test_failed_services_are_counted_by_service_and_user(self):
        seed, _ = SR.facts(lines(4, failed_service=2), ended=True)
        assert seed["failedServiceUsers"] == 2 and seed["failedServices"] == {"chat": 2}
        assert seed["failedServiceShare"] == 0.5

    def test_fill_reached_needs_the_run_to_be_over(self):
        beat = ["  [a@x.com] top-up in 9s: 1.0GB -> 30.0GB (12 filler file(s))",
                "  ... still topping up: 1/1 users done after 5m00s (0 in flight) -- 383.0 GB uploaded of 383 GB planned"]
        assert SR.facts(beat, ended=False)[0]["fillReached"] is None
        assert SR.facts(beat, ended=True)[0]["fillReached"] == 1.0

    def test_warning_families_carry_counts_and_the_users_they_hit(self):
        _, fam = SR.facts(["  ! chat for a@x.com: HTTP 404 (NOT_FOUND): x", "  ! chat for b@x.com: HTTP 404 (NOT_FOUND): x",
                           "  ! label L: HTTP 409 (aborted): y"], ended=True)
        assert (fam[0]["count"], fam[0]["users"], fam[0]["itemType"]) == (2, 2, "chat")


def _report(settings, transcript, run=None):
    return RR.build_report(RR.collect_seed_facts(settings, run=run, transcript=transcript), account_id=3)


class TestVerdicts:
    def test_a_clean_finished_seed_passes(self, settings):
        r = _report(settings, lines(4), {"returnCode": 0, "finishedAt": "2026-09-26T01:00:00Z"})
        assert r["verdict"] == "PASS" and r["kind"] == "seed"

    def test_a_seed_whose_exit_was_never_seen_is_unverified_not_a_pass(self, settings):
        r = _report(settings, lines(4))
        assert r["verdict"] == "UNVERIFIED"

    def test_a_crashed_seed_fails_even_if_every_user_finished(self, settings):
        assert _report(settings, lines(4), {"returnCode": -9, "finishedAt": "x"})["verdict"] == "FAIL"

    def test_users_who_never_reported_fail_a_finished_run(self, settings):
        r = _report(settings, lines(2), {"returnCode": 0, "finishedAt": "2026-09-26T01:00:00Z"})
        by = {x["id"]: x["status"] for x in r["benchmarks"]["results"]}
        assert by["never_finished"] == "fail" and by["users_finished"] == "fail" and r["verdict"] == "FAIL"

    def test_a_run_still_going_is_not_failed_for_users_not_done_yet(self, settings):
        r = _report(settings, lines(2))            # no run record: still in flight, or unknown
        by = {x["id"]: x["status"] for x in r["benchmarks"]["results"]}
        assert by["never_finished"] == "unknown"

    def test_a_seed_report_never_borrows_the_migration_benchmarks(self, settings):
        r = _report(settings, lines(4))
        ids = {x["id"] for x in r["benchmarks"]["results"]}
        assert "users_finished" in ids and "count_parity" not in ids and "item_failure_rate" not in ids

    def test_next_steps_speak_about_the_seed(self, settings):
        r = _report(settings, lines(2, failed_service=1), {"returnCode": 0, "finishedAt": "x"})
        text = " ".join(r["nextSteps"])
        assert "never reported" in text and "failed service" in text and "Tally" not in text


class TestSeedPdfsAndStorage:
    def test_both_pdfs_render_and_say_it_is_a_seed(self, settings, tmp_path):
        import report_pdf
        r = _report(settings, lines(3, failed_service=1, extra=["  ! chat for u0@x.com: HTTP 404 (NOT_FOUND): boom"]),
                    {"returnCode": 0, "finishedAt": "x"})
        for aud in ("human", "claude"):
            p = str(tmp_path / f"{aud}.pdf")
            report_pdf.write_pdf(r, p, aud)
            assert open(p, "rb").read().startswith(b"%PDF-")
        raw = open(str(tmp_path / "claude.pdf"), "rb").read()
        assert b"Seed totals" in raw and b"tail -n 200" in raw and b"audit_log" not in raw

    def test_generate_saves_and_lists_a_seed_report_without_a_ledger(self, settings, tmp_path, monkeypatch):
        monkeypatch.setattr(RR, "reports_dir", lambda a: str(tmp_path / "reports" / str(a)))
        meta = RR.generate(None, settings, 3, kind="seed", transcript=lines(4),
                           run={"returnCode": 0, "finishedAt": "2026-09-26T01:00:00Z"})
        assert meta["kind"] == "seed" and meta["files"] == ["json", "human.pdf", "claude.pdf"]
        assert [r["kind"] for r in RR.list_reports(3)] == ["seed"]
