"""The campaign's scenarios (fableplan2, docs/tasks/synthetic-10-plan.md, P2.1 to P2.11), as data.

One scenario is one thing caused on purpose and what must hold afterwards. The runner (campaign.py)
executes them; tests/test_campaign.py fails when a known error, a benchmark, a watcher or a failure has
no scenario -- that is what makes "every possible" checkable. `act`, `inject` and `expect` are the plan's
words made structured; a Sonnet session refines each one when its group first runs live (P3.4).

How a scenario is caused (`how`):
  seeded            the cohort's data makes it happen inside an ordinary run
  must_not_happen   a fixed defect: the scenario fails if the finding comes back
  arm               only one transfer arm makes it happen
  changed_after     something is changed on a. or b. after the run
  on_target         made on b. before the run
  failpoint         a failpoint (failpoints.py, P2.3) loses or kills at one call
  failure           a failure scenario of group `failures` causes it
  protected_source  a. is un-declared a sandbox for this one scenario
  google            only Google decides when; tried live three times, else its unit test stands
  unseedable        cannot be caused on a fresh tenant; `unit_test` names what covers it
  chat              opt-in, outside the score
"""
from __future__ import annotations

from dataclasses import dataclass, field

GROUPS = ("errors", "failures", "benchmarks", "watchers", "chat")


@dataclass(frozen=True)
class Scenario:
    id: str
    group: str
    title: str
    how: str
    fixtures: tuple = ()
    act: dict = field(default_factory=dict)
    inject: dict = field(default_factory=dict)
    expect: dict = field(default_factory=dict)
    needs: tuple = ()            # the operator permission it rests on: "net" (P0.7b), "reboot" (P0.7c)
    unit_test: str = ""          # for `unseedable`: the test that covers it instead


# ---- errors: every id of known_issues.ISSUES and RUN_KINDS ---------------------------------------------
# how -> (ids, fixtures or the change made). Fixtures name data-generator/fixtures.py entries (P2.4).
_ERRORS = {
    "seeded": ({
        "gmail.labels_added": "c04", "gmail.duplicate": "c04", "gmail.no_message_id": "c04",
        "gmail.body_link_copy": "c04", "gmail.link_owner_not_migrated": "c04",
        "gmail.link_scan_encrypted": "c04", "run.mail_owed_to_dms": "c04", "run.held_for_drive": "c04",
        "drive.shortcut_target": "c03", "drive.item_absent": "c03", "drive.group_grant": "c02",
        "drive.embedded_images": "c03", "drive.form_relinked": "c03", "drive.forms_response_sheet": "c03",
        "run.form_rebuild": "c09", "drive.comments": "c03", "run.comment_differs": "c03",
        "run.comment_missing": "c03", "drive.comments_unreadable": "c03", "drive.sheet_too_large": "c03",
        "tasks.untitled": "c07", "run.malware_flag": "c09", "drive.unmapped_source": "c02"}, "happens in the run"),
    "must_not_happen": ({
        "gmail.link_scan_fixed": "c04", "drive.sheets_scope": "c03", "drive.office_compared_as_native": "c01",
        "drive.comment_quote_escaped": "c03"}, "fixed defect: the finding must not return"),
    "arm": ({"drive.native_structure": "c03", "drive.content_export": "c03"}, "download_upload arm only"),
    "changed_after": ({
        "gmail.labels_missing": "change a label on b.", "gmail.unmapped_source": "new mail on a.",
        "drive.mtime": "touch a file on b.", "drive.content_changed": "edit a file on a.",
        "drive.missing_on_target": "delete a copy on b.", "drive.grant_missing": "remove a grant on b.",
        "contacts.unmapped_source": "add a contact on a.", "contacts.missing_on_target": "delete a contact on b.",
        "tasks.list_missing": "delete a task list on b.",
        "drive.ab_test_residue": "after an A/B arm run"}, "changed on purpose after the run"),
    "on_target": ({"drive.parent_folder": "two folders of one name and creation time on b."}, "made on b. first"),
    "failpoint": ({"drive.duplicate": "drive.copy.landed lost",
                   "run.comment_prior_attempt": "drive.comment.created lost"}, "through a failpoint"),
    "failure": ({"run.disk_full": "F-disk", "run.db_rollback": "F-disk"}, "through a failure scenario"),
    "protected_source": ({"drive.source_changed": "lift a.'s sandbox declaration, edit a file mid-run"},
                         "needs a protected source"),
    "google": ({"drive.read_refused": "verify beside a migration of one mailbox", "drive.unverified_transient":
                "verify beside a migration", "gmail.rate_limited": "verify beside a migration of one mailbox"},
               "Google decides when"),
    "unseedable": ({"run.resource_key": "needs a link-shared file from before Google's 2021 change"},
                   "likely unseedable"),
    "chat": ({"chat.attributed": "c07", "chat.space_type": "c07", "chat.members": "c07", "chat.sender": "c07",
              "chat.text": "c07", "chat.not_migrated": "a run with Chat off"}, "opt-in, outside the score"),
}
# What covers an unseedable or google-decided kind when it cannot be caused live.
UNIT_TESTS = {
    "run.resource_key": "tests/test_resource_keys.py",
    "drive.read_refused": "tests/test_verify_sample.py",
    "drive.unverified_transient": "tests/test_verify_sample.py",
    "gmail.rate_limited": "tests/test_verify_gmail_quota.py",
}


# What the control run's ledger shows when a seeded condition was met and handled (no finding needed): the
# (item_type, status) of an audit row. A kind not listed has no evidence defined yet, so it reads UNVERIFIED.
EVIDENCE = {
    "gmail.duplicate": ("message", "SUCCESS_SOURCE_DUPLICATE"),
    "gmail.link_scan_encrypted": ("message_link", "LINK_SCAN_ENCRYPTED"),
    "run.mail_owed_to_dms": ("message", "DELIVERED_BY_DMS"),
}


# Kinds proved from the source instead (campaign_evidence.PROBES): the case is there, and the user reads IDENTICAL.
SOURCE_PROBES = ("drive.sheet_too_large", "drive.embedded_images", "run.form_rebuild", "drive.group_grant", "gmail.no_message_id", "gmail.body_link_copy", "gmail.link_owner_not_migrated", "tasks.untitled", "drive.comments", "drive.shortcut_target", "run.malware_flag", "drive.comment_quote_escaped")


def _errors() -> list:
    out = []
    for how, (ids, why) in _ERRORS.items():
        for kid, what in ids.items():
            group = "chat" if how == "chat" else "errors"
            expect = ({"finding_absent": kid, **({"needs_source": kid} if kid in SOURCE_PROBES else {})} if how == "must_not_happen"
                      else {"finding_classified": kid, "ends_as": "its known_issues entry", "or_evidence": EVIDENCE.get(kid),
                                                                 "or_source": kid if kid in SOURCE_PROBES else None}
                      if how == "seeded" else {"finding_classified": kid, "ends_as": "its known_issues entry"})
            if how == "changed_after":
                import known_issues
                auto = {i[0] for i in known_issues.ISSUES if i[6]}
                expect = {"finding_classified": kid, "ends_as": "its known_issues entry",
                          "service": kid.split(".")[0], **({"fixed": kid} if kid in auto else {})}
            out.append(Scenario(
                id=f"err:{kid}", group=group, title=f"{kid} ({why})", how=how,
                fixtures=(what,) if what in {f"c{n:02d}" for n in range(1, 11)} else (),
                inject={"cause": what} if how in ("changed_after", "on_target", "failpoint", "failure",
                                                  "protected_source", "google", "unseedable") else {},
                expect=expect, unit_test=UNIT_TESTS.get(kid, "")))
    return out


# ---- failures: one row each ----------------------------------------------------------------------------
_FAILURES = (
    ("F-kill", "The process dies at each failpoint", ""),
    ("F-lost", "A reply is lost after the write landed", ""),
    ("F-kill-random", "SIGKILL at 5 random moments", ""),
    ("F-stop", "Stop, then Start (one press, and two)", ""),
    ("F-api", "The API restarts in the Drive pass, the mail pass and a follow-on", ""),
    ("F-webui", "The web UI restarts during a seed, then a migration", ""),
    ("F-reboot", "The box reboots mid-run", "reboot"),
    ("F-net-3", "No network for 3 minutes", "net"),
    ("F-net-12", "No network for 12 minutes", "net"),
    ("F-disk", "The scratch disk fills (a 64 MB tmpfs, never the real disk)", ""),
    ("F-memory", "Memory runs short (the watchdog's threshold set low)", ""),
    ("F-rate", "Google pushes back (per_user_qps 40, mail_workers 16)", ""),
    ("F-scope", "A scope is gone from a.'s delegation between two runs", ""),
    ("F-suspend", "A target account is suspended mid-run", ""),
    ("F-source-delete", "A source file and a source message vanish mid-run", ""),
    ("F-source-edit", "A source file changes after its copy", ""),
    ("F-tamper", "b. is changed after the run", ""),
    ("F-double", "Start pressed twice", ""),
    ("F-deploy", "A deploy during a run", ""),
    ("F-dms-held", "The DMS with one user FAILED", ""),
    ("F-licence", "More users than b. has licences", ""),
)


def _failures() -> list:
    return [Scenario(id=f"fail:{fid}", group="failures", title=title, how="failure", needs=(need,) if need else (),
                     expect={"equal_to_control": True, "no_item_twice": True})
            for fid, title, need in _FAILURES]


# ---- failpoints (P2.3): F-kill at every point, F-lost at every Google write --------------------------------
# A point after a ledger write or around the run has no reply to lose: kill only.
KILL_ONLY = ("drive.mapped", "gmail.mapped", "run.pass_done", "run.exit", "followon.repair.start",
             "followon.dms.start")


def _failpoints() -> list:
    import failpoints
    out = []
    for name in failpoints.NAMES:
        for mode in ("kill",) if name in KILL_ONLY else ("kill", "lost"):
            fault = f"{name}@1" + (":lost" if mode == "lost" else "")
            out.append(Scenario(id=f"fp:{name}:{mode}", group="failures", how="failpoint",
                                title=f"{'the process dies' if mode == 'kill' else 'the reply is lost'} at {name}",
                                inject={"env": {"BITPORT_FAULT": fault}},
                                expect={"equal_to_control": True, "no_item_twice": True}))
    return out


# ---- benchmarks: each check can pass and can fail ------------------------------------------------------
# The fault that makes it read FAIL or UNVERIFIED: a failure scenario ("F-...") or a short phrase.
BENCH_FAULT = {
    "run_completed": "F-kill", "migrated_something": "migrate a user whose source is empty",
    "item_failure_rate": "F-source-delete", "users_failed": "F-suspend", "blocked_items": "F-licence",
    "retry_rate": "F-rate", "quota_pushbacks": "F-rate", "p95_latency": "F-rate",
    "items_per_min_per_worker": "F-rate", "count_parity": "F-tamper", "checksum_failures": "F-tamper",
    "acl_fidelity": "F-tamper", "extra_grants": "F-tamper", "timestamps_preserved": "F-tamper",
    "users_finished": "F-kill", "never_finished": "F-kill", "service_failures": "F-scope",
    "fill_users_failed": "a seed run into a user with no storage left", "warnings_per_user": "a seed run with a refused scope",
    "fill_reached": "a seed run stopped half way",
}


def _benchmarks() -> list:
    import benchmarks
    out = []
    for kind, group in (("migration", benchmarks.DEFAULTS), ("seed", benchmarks.SEED_DEFAULTS)):
        for b in group:
            for outcome in ("pass", "fail"):
                out.append(Scenario(
                    id=f"bench:{kind}:{b.id}:{outcome}", group="benchmarks",
                    title=f"{kind} benchmark {b.label} reads {outcome.upper()}", how="check_only" if outcome == "pass" else "failure",
                    inject={} if outcome == "pass" else {"fault": BENCH_FAULT.get(b.id, "")},
                    expect={"benchmark": b.id,
                            "outcome": "PASS" if outcome == "pass" else "FAIL or UNVERIFIED"}))
    return out


# ---- watchers: each oversight check and incident kind opens and closes ---------------------------------
# Where the condition arises. "failure:F-x" = inside that failure scenario; "ledger" = a crafted ledger copy.
WATCHER_OPENS_IN = {
    "watch_repair_loop": "ledger", "watch_slow_file": "failure:F-rate", "watch_verdict_flip": "ledger",
    "watch_unclassified": "ledger", "watch_source_changed": "failure:F-source-edit",
    "watch_box_busy": "failure:F-api", "watch_tenant_ready": "failure:F-scope", "watch_google_status": "ledger",
    "watch_isolation": "ledger", "watch_limiter_low": "failure:F-rate", "watch_fallbacks": "ledger",
    "watch_recurring": "ledger",
    "crashed": "failure:F-kill", "stalled": "failure:F-memory", "failing": "failure:F-suspend",
    "traceback": "failure:F-kill", "verdict_fail": "failure:F-tamper", "disk_full": "failure:F-disk",
    "dms_not_started": "failure:F-dms-held", "source_changed": "failure:F-source-edit",
    "teardown": "never: teardown is the operator's (its unit test stands)",
}
INCIDENT_KINDS = ("crashed", "stalled", "failing", "traceback", "verdict_fail", "disk_full",
                  "dms_not_started", "source_changed", "teardown")


def _watchers() -> list:
    import oversight
    names = [c.__name__ for c in oversight.CHECKS] + list(INCIDENT_KINDS)
    out = []
    for n in names:
        where = WATCHER_OPENS_IN.get(n, "")
        for phase in ("opens", "closes"):
            out.append(Scenario(id=f"watch:{n}:{phase}", group="watchers", title=f"{n} {phase}",
                                how="unseedable" if where.startswith("never") else "failure",
                                inject={"where": where}, expect={"incident": n, phase: True, "closes_on": "evidence, never time"},
                                unit_test="tests/test_oversight.py" if where.startswith("never") else ""))
    return out


def all_scenarios() -> list:
    return _errors() + _failures() + _failpoints() + _benchmarks() + _watchers()


def by_id() -> dict:
    return {s.id: s for s in all_scenarios()}


import campaign_failures, campaign_watchers  # noqa: E402,F401 -- replace the stub fail:* and watch:* scenarios (P2.7, P2.11)
