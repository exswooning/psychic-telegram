"""Account-scoped Microsoft workspace API. Imported only by the control plane."""
from __future__ import annotations

import os
import sys
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel, Field

from .catalog import WORKLOADS
from .graph import credential_status, guid, validate_config
from .operations import sandbox_gate
from .planner import build_plan, validate_mapping
from .store import Store


class Action(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class Configure(Action):
    source: dict
    target: dict
    mode: Literal["preserve_source", "native_move"] = "preserve_source"
    sandbox: bool = False


class Mapping(Action):
    sourceId: str
    targetId: str


class Pilot(Action):
    sourceId: str
    planHash: str
    confirm: str


def router(*, operator, require_login, require_admin, require_active_subscription, launch, off_loop):
    r = APIRouter(prefix="/api/v2/microsoft", tags=["Microsoft 365"])

    def store_for(op, *, write=False):
        require_login(op)
        if write:
            require_admin(op)
            require_active_subscription(op)
        return Store(op.account_id)

    def audit(store, op, action, reason, detail=None):
        if len(reason.strip()) < 3:
            raise HTTPException(400, "provide a reason for this action")
        store.audit(action, reason.strip(), op.name, detail)

    def failure(exc):
        return HTTPException(400, str(exc))

    @r.get("/workspace")
    async def workspace(op=Depends(operator)):
        store = store_for(op)

        def read():
            value = store.snapshot()
            value["plan"] = build_plan(store)
            value["credentials"] = {side: credential_status(store.account_id, side, value["config"][side]) for side in ("source", "target")}
            value["sandboxWritesEnabled"] = os.getenv("BITPORT_MICROSOFT_SANDBOX_WRITES") == "1"
            value["accountId"] = store.account_id
            return value
        return await off_loop(read)

    @r.get("/catalog")
    async def catalog(op=Depends(operator)):
        require_login(op)
        return WORKLOADS

    @r.get("/report")
    async def report(op=Depends(operator)):
        store = store_for(op)
        def chunks():
            snapshot = {"accountId": store.account_id, **store.snapshot(), "plan": build_plan(store)}
            yield json.dumps(snapshot, ensure_ascii=False)[:-1] + ',"allCoverageScopes":['
            inventory_id = snapshot["plan"]["inventoryId"]
            if inventory_id:
                with store.connect() as c:
                    first = True
                    for row in c.execute("SELECT * FROM coverage WHERE run_id=? ORDER BY side,workload,scope", (inventory_id,)):
                        yield ("" if first else ",") + json.dumps(dict(row), ensure_ascii=False)
                        first = False
            yield '],"receiptEvents":['
            with store.connect() as c:
                first = True
                for row in c.execute("SELECT * FROM receipt_events ORDER BY id"):
                    event = {**dict(row), "detail": json.loads(row["detail"])}
                    yield ("" if first else ",") + json.dumps(event, ensure_ascii=False)
                    first = False
            yield "]}"
        return StreamingResponse(chunks(), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="bitport-microsoft-coverage.json"'})

    @r.post("/connections")
    async def connections(body: Configure, op=Depends(operator)):
        store = store_for(op, write=True)
        try:
            value = validate_config(body.model_dump(exclude={"reason"}))
            audit(store, op, "connections.configure", body.reason)
            await off_loop(store.save_config, value)
        except ValueError as exc:
            raise failure(exc) from None
        return {"ok": True, "config": value}

    @r.post("/mappings")
    async def mappings(body: Mapping, op=Depends(operator)):
        store = store_for(op, write=True)
        try:
            source, target = guid(body.sourceId), guid(body.targetId)
            validate_mapping(store, source, target)
            audit(store, op, "identity.confirm", body.reason, {"sourceId": source, "targetId": target})
            store.map_user(source, target)
        except ValueError as exc:
            raise failure(exc) from None
        return {"ok": True}

    def start(store, op, kind, reason, detail=None):
        try:
            audit(store, op, kind + ".start", reason, detail)
            rid = store.new_run(kind, detail)
        except ValueError as exc:
            raise failure(exc) from None
        argv = [sys.executable, "microsoft365_cli.py", "run", "--account-id", str(store.account_id), "--run-id", rid]
        try:
            ok, message = launch(argv, store.account_id, "microsoft365")
        except Exception:
            store.update_run(rid, "ERROR", {"error": "The local worker could not be started"})
            raise
        if not ok:
            store.update_run(rid, "BLOCKED", {"error": message})
            raise HTTPException(409, message)
        return {"ok": True, "runId": rid, "detail": message}

    @r.post("/discover")
    async def discover(body: Action, op=Depends(operator)):
        store = store_for(op, write=True)
        try:
            config = validate_config(store.config())
            for side in ("source", "target"):
                if not credential_status(store.account_id, side, config[side])["ready"]:
                    raise ValueError(f"install a matching {side} application certificate first")
        except ValueError as exc:
            raise failure(exc) from None
        return await off_loop(start, store, op, "discover", body.reason)

    @r.post("/runs/{run_id}/stop")
    async def stop(run_id: str, body: Action, op=Depends(operator)):
        store = store_for(op, write=True)
        try:
            store.run(run_id)
        except ValueError:
            raise HTTPException(404, "unknown Microsoft run") from None
        audit(store, op, "run.stop", body.reason, {"runId": run_id})
        store.stop(run_id)
        return {"ok": True, "detail": "Stop requested; the worker stops at a safe checkpoint"}

    @r.post("/runs/{run_id}/recover")
    async def recover(run_id: str, body: Action, op=Depends(operator)):
        store = store_for(op, write=True)
        audit(store, op, "run.recover-metadata", body.reason, {"runId": run_id})
        try:
            store.recover_interrupted(run_id)
        except ValueError as exc:
            raise failure(exc) from None
        return {"ok": True, "detail": "Marked interrupted; no process was started or stopped"}

    async def pilot(kind, body, op):
        store = store_for(op, write=True)
        try:
            source = guid(body.sourceId)
            sandbox_gate(store, body.planHash, source)
            expected = "COPY PROFILE TO SANDBOX" if kind == "profile-pilot" else "COPY FILES TO SANDBOX"
            if body.confirm != expected:
                raise ValueError("the operation confirmation did not match")
        except ValueError as exc:
            raise failure(exc) from None
        return await off_loop(start, store, op, kind, body.reason, {"sourceId": source, "planHash": body.planHash})

    @r.post("/profile-pilot")
    async def profile_pilot(body: Pilot, op=Depends(operator)):
        return await pilot("profile-pilot", body, op)

    @r.post("/drive-pilot")
    async def drive_pilot(body: Pilot, op=Depends(operator)):
        return await pilot("drive-pilot", body, op)

    return r
