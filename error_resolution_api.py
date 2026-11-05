"""Authorised UI controls and narrowly scoped coding-runner transport."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import error_resolution as E


class Action(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PolicyAction(Action):
    enabled: bool
    daily_limit: int = Field(default=3, ge=1, le=20)


class RunnerAction(Action):
    account_id: int | None = None
    all_accounts: bool = False
    label: str = Field(default="Local source laptop", min_length=1, max_length=80)


class CaseAction(Action):
    action: str


class RunnerReport(BaseModel):
    lease: str = Field(min_length=32, max_length=100)
    state: str = Field(max_length=40)
    detail: str = Field(default="", max_length=2000)
    source_commit: str = ""
    candidate_commit: str = ""
    evidence: dict = Field(default_factory=dict)


def register(app, api):
    def case_for(op, cid):
        api.require_login(op)
        case = E.case_detail(cid)
        login = getattr(op, "login_id", None) or op.account_id
        if not case or not (
            op.is_superadmin
            or api.accounts_auth.may_open(login, False, case["account_id"])
        ):
            raise HTTPException(404, "No such error case")
        return case

    def runner_auth(authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ")
        runner = E.authenticate(token) if authorization.startswith("Bearer ") else None
        if not runner:
            raise HTTPException(401, "Invalid coding-runner credential")
        return runner

    @app.get("/api/v2/error-resolution")
    async def overview(account_id: int | None = None, op=Depends(api.operator)):
        api.require_login(op)
        aid = api._reports_account(op, account_id)
        if aid is None and not op.is_superadmin:
            raise HTTPException(403, "Account required")
        cases = await api._off_loop(E.list_cases, aid)
        return {
            "cases": cases,
            "policy": await api._off_loop(E.policy),
            "runners": await api._off_loop(
                E.runners, None if op.is_superadmin else aid
            ),
            "canManage": op.role == "admin",
            "canManageGlobal": op.is_superadmin,
            "collection": "Automatic for active and finishing jobs; bounded incremental scans survive restarts.",
            "resolutionRule": "Fresh complete source/target verification is required; samples and commits cannot close cases.",
        }

    @app.get("/api/v2/error-resolution/cases/{cid}")
    async def detail(cid: int, op=Depends(api.operator)):
        return await api._off_loop(case_for, op, cid)

    @app.get("/api/v2/error-resolution/cases/{cid}/bundle")
    async def handoff(cid: int, op=Depends(api.operator)):
        case_for(op, cid)
        return await api._off_loop(E.bundle, cid)

    @app.post("/api/v2/error-resolution/policy")
    async def configure(body: PolicyAction, op=Depends(api.operator)):
        def apply():
            E.set_policy(body.enabled, body.daily_limit)
            return True, "Coding queue policy updated; active migrations are unchanged."

        return await api._gated(
            op,
            "error-resolution.policy",
            body,
            "GLOBAL",
            apply,
            extra_check=api.require_superadmin,
        )

    @app.post("/api/v2/error-resolution/cases/{cid}/action")
    async def act(cid: int, body: CaseAction, op=Depends(api.operator)):
        case = case_for(op, cid)

        def apply():
            if body.action == "queue":
                E.queue_case(cid)
                return True, "Queued for coding investigation."
            if body.action == "recheck":
                with api.cpdb.ro() as db:
                    users = [
                        r[0]
                        for r in db.execute(
                            "SELECT DISTINCT source_user FROM error_affected_items WHERE case_id=? AND source_user!='' LIMIT 21",
                            (cid,),
                        )
                    ]
                if (
                    not users
                    or case["service"]
                    not in {
                        "drive",
                        "gmail",
                        "calendar",
                        "contacts",
                        "tasks",
                        "chat",
                        "directory",
                    }
                    or len(users) > 20
                ):
                    return (
                        False,
                        "Select affected users and services in Verification; this case has insufficient or broad scope.",
                    )
                if not E.globally_idle(api.HERE):
                    return (
                        False,
                        "Waiting: migration, verification, queued work or repairs have priority.",
                    )
                argv = [
                    api.PY,
                    "verify_sample.py",
                    *api._account_argv(case["account_id"]),
                    "--services",
                    case["service"],
                ]
                for user in users:
                    argv += ["--user", user]
                ok, message = api._run_admitted(argv, case["account_id"], "verify")
                if ok:
                    with api.cpdb.rw() as db:
                        E._move(
                            db,
                            cid,
                            "RECHECKING",
                            "Full scoped read-only verification admitted. " + message,
                        )
                return ok, message
            return (
                False,
                "Action must be queue or recheck; resolution requires verification evidence.",
            )

        return await api._gated(
            op, "error-resolution." + body.action, body, str(cid), apply
        )

    @app.post("/api/v2/error-resolution/runners")
    async def connect(body: RunnerAction, request: Request, op=Depends(api.operator)):
        api.require_login(op)
        if body.all_accounts:
            api.require_superadmin(op)
            aid = -1
        else:
            aid = api._resolve_account(body, op)
        made = {}

        def apply():
            rid, token = E.create_runner(aid, body.label)
            made.update(
                runnerId=rid, apiUrl=str(request.base_url).rstrip("/"), token=token
            )
            return (
                True,
                "Scoped coding-runner credential created. It grants no tenant API or deployment access.",
            )

        result = await api._gated(op, "error-resolution.connect", body, str(aid), apply)
        if not made:
            return result
        return JSONResponse(made, headers={"Cache-Control": "no-store"})

    @app.post("/api/v2/error-resolution/runners/{rid}/revoke")
    async def revoke(rid: int, body: Action, op=Depends(api.operator)):
        api.require_login(op)

        def apply():
            with api.cpdb.rw() as db:
                row = db.execute(
                    "SELECT account_id FROM error_runner_tokens WHERE id=?", (rid,)
                ).fetchone()
                if not row or (
                    not op.is_superadmin and row["account_id"] != op.account_id
                ):
                    return False, "No such coding runner."
                db.execute(
                    "UPDATE error_runner_tokens SET enabled=0,detail='Revoked' WHERE id=?",
                    (rid,),
                )
            return True, "Runner credential revoked."

        return await api._gated(op, "error-resolution.revoke", body, str(rid), apply)

    # These credentials cannot start migrations, read tenant files, obtain Google
    # tokens, alter policy, or resolve a case. They only lease/report coding work.
    @app.post("/api/v2/error-resolution/worker/claim")
    async def claim(runner=Depends(runner_auth)):
        if not await api._off_loop(E.globally_idle, api.HERE):
            return {
                "attempt": None,
                "reason": "Migration/verification/repair has priority.",
            }
        return {"attempt": await api._off_loop(E.claim, runner)}

    @app.post("/api/v2/error-resolution/worker/{attempt}/heartbeat")
    async def beat(attempt: int, body: RunnerReport, runner=Depends(runner_auth)):
        try:
            await api._off_loop(E.heartbeat, runner, attempt, body.lease, body.detail)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {
            "ok": True,
            "enabled": E.policy()["enabled"],
            "yield": not E.globally_idle(api.HERE),
        }

    @app.post("/api/v2/error-resolution/worker/{attempt}/report")
    async def report(attempt: int, body: RunnerReport, runner=Depends(runner_auth)):
        try:
            await api._off_loop(
                E.report,
                runner,
                attempt,
                body.lease,
                body.state,
                body.detail,
                body.source_commit,
                body.candidate_commit,
                body.evidence,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}


def dispatch_rechecks(api):
    """Low-priority follow-up, never automatic data repair or job cancellation."""
    if not E.globally_idle(api.HERE):
        return
    with api.cpdb.ro() as db:
        rows = db.execute(
            "SELECT r.*,c.account_id,c.service,c.state FROM error_rechecks r JOIN error_cases c ON c.id=r.case_id ORDER BY requested_at LIMIT 20"
        ).fetchall()
    for row in rows:
        if row["state"] == "RESOLVED":
            continue
        if row["started_at"]:
            from datetime import datetime, timezone

            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
            ).total_seconds()
            if age > 60 and row["state"] == "RECHECKING":
                with api.cpdb.rw() as db:
                    E._move(
                        db,
                        row["case_id"],
                        "FAILED",
                        "Recheck finished without complete matching evidence; findings remain open. Inspect the saved service result.",
                    )
            continue
        with api.cpdb.ro() as db:
            users = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT source_user FROM error_affected_items WHERE case_id=? AND source_user!='' LIMIT 21",
                    (row["case_id"],),
                )
            ]
        if (
            not users
            or len(users) > 20
            or row["service"]
            not in {
                "drive",
                "gmail",
                "calendar",
                "contacts",
                "tasks",
                "chat",
                "directory",
            }
        ):
            with api.cpdb.rw() as db:
                E._move(
                    db,
                    row["case_id"],
                    "OWNER_ACTION",
                    "Select verification scope in the UI; automatic recheck scope is unavailable or too broad.",
                )
                db.execute(
                    "DELETE FROM error_rechecks WHERE case_id=?", (row["case_id"],)
                )
            continue
        argv = [
            api.PY,
            "verify_sample.py",
            *api._account_argv(row["account_id"]),
            "--services",
            row["service"],
        ]
        for user in users:
            argv += ["--user", user]
        ok, message = api._run_admitted(argv, row["account_id"], "verify")
        if ok:
            with api.cpdb.rw() as db:
                db.execute(
                    "UPDATE error_rechecks SET started_at=?,detail=? WHERE case_id=?",
                    (E.utc(), E.redact(message), row["case_id"]),
                )
                E._move(
                    db,
                    row["case_id"],
                    "RECHECKING",
                    "Fresh full scoped read-only verification admitted; repairs remain controlled by their existing policy.",
                )
        return
