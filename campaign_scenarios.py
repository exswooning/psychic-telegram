"""The campaign's scenarios (fableplan2, docs/tasks/synthetic-10-plan.md, P2.1 to P2.11), as data.

One scenario is one thing caused on purpose and what must hold afterwards. The runner (campaign.py)
executes them; tests/test_campaign.py fails when a known error, a benchmark, a watcher or a failure has
no scenario -- that is what makes "every possible" checkable. `act`, `inject` and `expect` are the plan's
words made structured; a Sonnet session refines each one when its group first runs live (P3.4).

How a scenario ends (`expect`, from its known_issues entry): `fixed` (automatic: a row in issue_fix_log, or in
item_retries for a run failure), `accepted` (in the parity gate's accepted list) or `listed` (the gate names it for
a person). `act["body"]` may say "$user": the twin, or the fixture user when the scenario has a fixture.

How a scenario is caused (`how`):
  seeded            the cohort's data makes it happen inside an ordinary run
  must_not_happen   a fixed defect: the scenario fails if the finding comes back
  arm               only one transfer arm makes it happen
  changed_after     something is changed on a. or b. after the run
  on_target         made on b. before the run
  failpoint         a failpoint (failpoints.py, P2.3) loses or kills at one call
  failure           a failure scenario of group `failures` causes it
  protected_source  a. is un-declared a sandbox for this one scenario
  google            only Google decides when; tried live three times (`act["tries"]`), else its unit test stands
  unseedable        cannot be caused on a fresh tenant; `unit_test` names what covers it, `inject["cause"]` says why
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
        "gmail.link_scan_encrypted": "c04", "run.mail_owed_to_dms": "c04",
        "drive.shortcut_target": "c03", "drive.item_absent": "c03", "drive.group_grant": "c02",
        "drive.embedded_images": "c03",
        "run.form_rebuild": "c09", "drive.comments": "c03", "run.comment_differs": "c03",
        "drive.comments_unreadable": "c09", "drive.sheet_too_large": "c03",
        "tasks.untitled": "c07", "run.malware_flag": "c09", "drive.unmapped_source": "c02"}, "happens in the run"),
    "must_not_happen": ({
        "gmail.link_scan_fixed": "c04", "drive.sheets_scope": "c03", "drive.office_compared_as_native": "c01",
        "drive.comment_quote_escaped": "c03"}, "fixed defect: the finding must not return"),
    "arm": ({"drive.native_structure": "a twin's Drive moved by download_upload"}, "download_upload arm only"),
    "changed_after": ({
        "gmail.labels_missing": "change a label on b.", "gmail.unmapped_source": "new mail on a.",
        "drive.mtime": "touch a file on b.", "drive.content_changed": "edit a file on a.",
        "drive.missing_on_target": "delete a copy on b.", "drive.grant_missing": "remove a grant on b.",
        "contacts.unmapped_source": "add a contact on a.", "contacts.missing_on_target": "delete a contact on b.",
        "tasks.list_missing": "delete a task list on b.",
        "drive.ab_test_residue": "after an A/B arm run"}, "changed on purpose after the run"),
    "on_target": ({"drive.parent_folder": "two folders of one name and creation time on b."}, "made on b. first"),
    "failpoint": ({"drive.duplicate": "drive.copy.landed lost",
                   "run.comment_prior_attempt": "drive.comment.created lost",
                   "run.reply_lost": "drive.comment.created lost"}, "through a failpoint"),
    "failure": ({"run.disk_full": "F-disk", "run.db_rollback": "F-disk", "run.dns": "F-net-12"}, "through a failure scenario"),
    "protected_source": ({"drive.source_changed": "lift a.'s sandbox declaration, edit a file mid-run"},
                         "needs a protected source"),
    # A full verify beside a delta of the same user (c03: the 20,000 x 120 Sheet; c10: 5,000 messages), three tries.
    "google": ({"drive.read_refused": "c03", "drive.unverified_transient": "c03", "gmail.rate_limited": "c10",
                "run.rate_limited": "c03"}, "Google decides when"),
    "unseedable": ({"run.resource_key": "needs a link-shared file from before Google's 2021 change",
                    "gmail.reset_leftover": "needs a reset, then a rerun (P3.5 runs 2 and 3 read it on the gate)",
                    "run.backend_500": "Google's own 500 on an upload; nothing on our side causes it",
                    "run.coverage_incomplete": "a pass whose coverage check runs before its findings are repaired",
                    "run.google_unavailable": "Google's own 5xx through every retry; nothing on our side causes it",
                    "gmail.dms_reconcile_mismatch": "Google's DMS import decides it; split mail reconciles what it finds",
                    "drive.external_copy": "needs a file an outside account owns, shared in -- only that person can make "
                                           "one (c02's outside_folder waits on the same upload); seen live on dharan",
                    "drive.content_export": "Docs and Slides are judged as rendered and Sheets by structure, so the export "
                                            "compare is left only to Drawings, whose download_upload round trip comes out "
                                            "identical: a twin's arm reads drive.native_structure (fp2-live, 24 Nov 2026)",
                    "run.held_for_drive": "a user's Drive has to FAIL in an ordered run; a healthy cohort never does (F-scope is the failures group's)",
                    "drive.form_relinked": "Google links a Form to a responses Sheet only in the Forms UI (linkedSheetId is read-only): no API makes one",
                    "drive.forms_response_sheet": "same cause: no seeded Form has a responses Sheet, and no API links one",
                    "run.comment_missing": "two copies of one Doc sharing a comment id comes from Drive's UI copy; files.copy copies no comments",
                    "drive.copy_unrecorded": "a crafted ledger row with one exact-content target copy; covered by the isolated finding repair test",
                    "drive.external_copy_review": "a crafted fallback with extra target grants; covered by the isolated external-access repair test",
                    "drive.form_title": "a crafted Form title difference; covered by the isolated target-only Forms repair test",
                    "drive.script_project": "a crafted Apps Script difference that needs a person; covered by the Drive finding repair test",
                    "drive.form_upload_question": "a crafted file-upload question that needs a person; covered by the Drive finding repair test",
                    "drive.sheet_tabs": "a crafted Sheet tab and gid conflict that needs a person; covered by the Drive finding repair test",
                    "gmail.source_gone": "a crafted source message returning 404 after listing; covered by the run failure repair test",
                    "gmail.destination_ambiguous": "a crafted duplicate destination Message-ID; covered by the run failure repair test"},
                   "likely unseedable"),
    # not_migrated first: it needs c07's Chat still unmigrated, the rest migrate it.
    "chat": ({"chat.not_migrated": "c07", "chat.attributed": "c07", "chat.space_type": "c07", "chat.members": "c07",
              "chat.sender": "c07", "chat.text": "c07"}, "opt-in, outside the score"),
}
# What covers an unseedable or google-decided kind when it cannot be caused live.
UNIT_TESTS = {
    "run.resource_key": "tests/test_resource_keys.py",
    "drive.read_refused": "tests/test_verify_sample.py",
    "drive.unverified_transient": "tests/test_verify_sample.py",
    "gmail.rate_limited": "tests/test_verify_gmail_quota.py",
    "run.rate_limited": "tests/test_rate_limit_gets_time.py",
    "gmail.reset_leftover": "tests/test_split_mail_duplicates.py",
    "run.backend_500": "tests/test_dharan_followups.py",          # the end-of-run straggler re-copy
    "run.coverage_incomplete": "tests/test_campaign.py",          # named and classified (run 7, 21 Nov 2026)
    "run.google_unavailable": "tests/test_known_issues_run_kinds_dns.py",   # named (incident #255, 22 Nov 2026)
    "gmail.dms_reconcile_mismatch": "tests/test_split_mail_safety.py",   # named (fp2 run 15, t03; 24 Nov 2026)
    "drive.external_copy": "tests/test_known_issues_run_kinds_dns.py",   # accepted by the operator, 24 Nov 2026
    "drive.content_export": "tests/test_verify_sample.py",       # test_native_documents_are_exported_and_compared
    "run.held_for_drive": "tests/test_new_accounts_and_held_mail.py",     # held while Drive failed, runs once Drive succeeds
    "drive.form_relinked": "tests/test_form_relink.py",
    "drive.forms_response_sheet": "tests/test_form_relink.py",
    "run.comment_missing": "tests/test_resource_keys.py",                 # two files, one comment id, both comments copied
    "drive.copy_unrecorded": "tests/test_government_finding_repairs.py",
    "drive.external_copy_review": "tests/test_government_finding_repairs.py",
    "drive.form_title": "tests/test_government_finding_repairs.py",
    "drive.script_project": "tests/test_government_finding_repairs.py",
    "drive.form_upload_question": "tests/test_government_finding_repairs.py",
    "drive.sheet_tabs": "tests/test_government_finding_repairs.py",
    "gmail.source_gone": "tests/test_government_run_failure_repairs.py",
    "gmail.destination_ambiguous": "tests/test_government_run_failure_repairs.py",
}


# What the control run's ledger shows when a seeded condition was met and handled (no finding needed): the
# (item_type, status) of an audit row. A kind not listed has no evidence defined yet, so it reads UNVERIFIED.
EVIDENCE = {
    "gmail.duplicate": ("message", "SUCCESS_SOURCE_DUPLICATE"),
    "gmail.link_scan_encrypted": ("message_link", "LINK_SCAN_ENCRYPTED"),
    "run.mail_owed_to_dms": ("message", "DELIVERED_BY_DMS"),
}


# Kinds proved from the source instead (campaign_evidence.PROBES): the case is there, and the user reads IDENTICAL.
SOURCE_PROBES = ("drive.sheet_too_large", "drive.embedded_images", "run.form_rebuild", "drive.group_grant", "gmail.no_message_id", "gmail.body_link_copy", "gmail.link_owner_not_migrated", "tasks.untitled", "drive.comments", "drive.shortcut_target", "run.malware_flag", "drive.comment_quote_escaped",
                  "drive.item_absent", "drive.comments_unreadable", "run.comment_differs", "drive.unmapped_source")


def _ends(kid: str) -> str:
    """How a finding of this kind must end, from its known_issues entry and the parity gate's accepted kinds."""
    import known_issues
    import parity_gate
    entry = known_issues.BY_ID.get(kid) or known_issues.RUN_BY_ID[kid]
    return "fixed" if entry[6] else "accepted" if kid in parity_gate.ACCEPTED_KINDS else "listed"


def _migrate(**body) -> dict:
    return {"path": "/api/v2/migrate/start", "body": {"users": ["$user"], **body}}


_DRIVE = {"services": ["drive"]}
_DU = _migrate(**_DRIVE, transfer_mode="download_upload")
_VERIFY_DRIVE = {"verify_after": "drive"}      # a verify with repair once the run is over: where a fix is read from


def _beside(service: str) -> dict:
    """A delta over 90 days for the fixture user, a full verify started at the same moment, three tries."""
    return {"path": "/api/v2/migrate/delta", "body": {"users": ["$user"], "services": [service], "days": 90},
            "beside_verify": service, "tries": 3}


def _check(service: str) -> dict:
    return {"path": "/api/v2/one-to-one/run", "body": {"users": ["$user"], "services": [service], "limit": 0, "repair": False}}


def _lost(point: str) -> dict:
    return {"env": {"BITPORT_FAULT": f"{point}@1:lost"}}


# kind -> (act, inject, more expect). A cause the driver cannot make (an `inject` that is only a "cause") reads
# UNVERIFIED, "not automated yet", before any twin or run is spent: parent_folder, source_changed, ab_test_residue.
_ACT = {
    "drive.native_structure": (_DU, {}, _VERIFY_DRIVE),
    "drive.duplicate": (_migrate(**_DRIVE), _lost("drive.copy.landed"), _VERIFY_DRIVE),
    "run.comment_prior_attempt": (_migrate(**_DRIVE), _lost("drive.comment.created"), {}),
    "run.reply_lost": (_migrate(**_DRIVE), _lost("drive.comment.created"), {}),
    "drive.parent_folder": (_migrate(**_DRIVE), {"cause": "two folders of one name and creation time on b., made before the run"},
                            _VERIFY_DRIVE),
    "drive.source_changed": (_migrate(**_DRIVE), {"cause": "restore a.'s protection (domain-guard/restore), edit a source "
                                                         "file mid-run, declare the sandbox again (domain-guard/revoke)"}, {}),
    "drive.ab_test_residue": (_check("drive"), {"cause": "the Benchmarks page's A/B on the twin (ab_transfer), which leaves "
                                                         "grants to its throwaway accounts"}, {}),
    "drive.read_refused": (_beside("drive"), {}, _VERIFY_DRIVE), "drive.unverified_transient": (_beside("drive"), {}, _VERIFY_DRIVE),
    "run.rate_limited": (_beside("drive"), {}, _VERIFY_DRIVE), "gmail.rate_limited": (_beside("gmail"), {}, {"verify_after": "gmail"}),
    "chat.not_migrated": (_check("chat"), {}, {}),
    **{k: (_migrate(services=["chat"]), {}, {"verify_after": "chat"})
       for k in ("chat.attributed", "chat.space_type", "chat.members", "chat.sender", "chat.text")},
}
# Kinds a failure scenario causes: this one reads what that one left on its twin (so that one runs first).
_VIA_FAILURE = {"run.disk_full": "fail:F-disk", "run.db_rollback": "fail:F-disk", "run.dns": "fail:F-net-12"}


# Fired live on fresh twins (fp2-live, 24 Nov 2026) and settled without a finding: a lost copy reply is adopted
# (drive_engine._take_placed, t07), a lost comment reply is reconciled and CONFIRMED (migration_comments, t08).
HANDLED_UNDER_FAULT = ("drive.duplicate", "run.comment_prior_attempt")


def _errors() -> list:
    out = []
    for how, (ids, why) in _ERRORS.items():
        for kid, what in ids.items():
            group = "chat" if how == "chat" else "errors"
            expect = ({"finding_absent": kid, **({"needs_source": kid} if kid in SOURCE_PROBES else {})} if how == "must_not_happen"
                      else {"finding_classified": kid, "ends_as": _ends(kid), "or_evidence": EVIDENCE.get(kid),
                                                                 "or_source": kid if kid in SOURCE_PROBES else None}
                      if how == "seeded" else {"finding_classified": kid, "ends_as": _ends(kid)})
            end = _ends(kid)
            if how == "changed_after":
                expect = {"finding_classified": kid, "ends_as": end, "service": kid.split(".")[0],
                          **({end: kid} if end != "listed" else {})}
            act, inject, more = _ACT.get(kid, ({}, None, {}))
            if kid in _ACT or kid in _VIA_FAILURE:
                # a fixed defect must not return; every other kind is classified, then ends as its entry says
                expect = ({"finding_absent": kid, "ends_as": end} if kid == "run.db_rollback"
                          else {"finding_classified": kid, "ends_as": end, end: kid, **more})
            if kid in HANDLED_UNDER_FAULT:
                # The fault fires (checked in campaign.run_scenario) and Bitport settles it itself: no finding comes.
                expect = {"finding_absent": kid, "ends_as": end, **more}
            out.append(Scenario(
                id=f"err:{kid}", group=group, title=f"{kid} ({why})", how=how, act=act,
                fixtures=(what,) if what in {f"c{n:02d}" for n in range(1, 11)} else (),
                inject=inject if inject is not None else (
                    {"failure": _VIA_FAILURE[kid]} if kid in _VIA_FAILURE else
                    {"cause": what} if how in ("changed_after", "on_target", "failpoint", "failure",
                                               "protected_source", "unseedable") else {}),
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
# The fault that makes it read FAIL or UNVERIFIED: a failure scenario ("F-...", whose run's report is read) or, when
# no run on a. can cause it, the reason -- that benchmark is `unseedable` and BENCH_UNIT proves it. A key "seed:<id>"
# is the seed run's check of that id (run_completed is in both kinds).
BENCH_FAULT = {
    "run_completed": "F-kill",
    # ledger.succeeded counts the whole ledger, so a twin that copies nothing leaves it far above 1
    "migrated_something": "ledger.succeeded is the whole ledger's: only an empty ledger reads 0, and a. holds the cohort",
    "item_failure_rate": "F-suspend", "users_failed": "F-suspend",
    "blocked_items": "b. has 300 licences and the cohort 30, so none can be short (F-licence is unseedable too)",
    "retry_rate": "F-rate", "quota_pushbacks": "F-rate", "p95_latency": "F-rate",
    "items_per_min_per_worker": "F-rate", "count_parity": "F-tamper", "checksum_failures": "F-tamper",
    "acl_fidelity": "F-tamper", "extra_grants": "F-tamper", "timestamps_preserved": "F-tamper",
    "service_failures": "needs a seed that fails a service: a scope pulled from the seed key (15 min to propagate) and a re-seed of a twin",
    "fill_users_failed": "needs a fill that exhausts a.'s storage pool (storageQuotaExceeded), which stops every later seed there",
    "warnings_per_user": "needs a seed that draws over one warning per user (a refused scope), as service_failures",
    "fill_reached": "needs a fill that exhausts a.'s storage pool, as fill_users_failed",
    "seed:run_completed": "F-webui", "seed:users_finished": "F-webui", "seed:never_finished": "F-webui",
}
BENCH_UNIT = "tests/test_benchmark_truth_table.py"      # every check FAILs (or WARNs, if it cannot fail) on crafted facts


def bench_fault(kind: str, bid: str) -> str:
    return BENCH_FAULT.get(f"{kind}:{bid}") or BENCH_FAULT.get(bid, "")


def _benchmarks() -> list:
    import benchmarks
    out = []
    for kind, group in (("migration", benchmarks.DEFAULTS), ("seed", benchmarks.SEED_DEFAULTS)):
        for b in group:
            fault = bench_fault(kind, b.id)
            ran = fault.startswith("F-")      # else no run on a. can cause it
            # What shows the fault: FAIL; WARN for a check that cannot fail (bad is None); UNKNOWN when it is
            # required (a report with a required UNKNOWN is UNVERIFIED, never PASS).
            accept = ["FAIL"] + (["WARN"] if b.bad is None else []) + (["UNKNOWN", "UNVERIFIED"] if b.required else [])
            for outcome in ("pass", "fail"):
                fail = outcome == "fail"
                out.append(Scenario(
                    id=f"bench:{kind}:{b.id}:{outcome}", group="benchmarks",
                    title=f"{kind} benchmark {b.label} reads {outcome.upper()}",
                    how="check_only" if not fail else "failure" if ran else "unseedable",
                    inject={} if not fail else {"fault": fault, **({"failure": f"fail:{fault}"} if ran else {"cause": fault})},
                    unit_test=BENCH_UNIT if fail and not ran else "",
                    expect={"benchmark": b.id, "report_kind": kind, "outcome": "FAIL or UNVERIFIED" if fail else "PASS",
                            **({"accept": accept} if fail else {})}))
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
