"""Evidence-based coverage, immutable identity mapping and explicit route blockers."""
from __future__ import annotations

from .catalog import CATALOG_VERSION, PROFILE_FIELDS, WORKLOADS
from .store import fingerprint


def profile_diff(source, target):
    return [{"field": field, "source": source.get(field), "target": target.get(field)}
            for field in PROFILE_FIELDS if source.get(field) != target.get(field)]


def identity_preview(store, inventory):
    if not inventory:
        return []
    rid = inventory["id"]
    target = store.objects(rid, "target", "directory", "")
    by_id = {p["id"]: p for p in target}
    mappings = {m["source_id"]: m["target_id"] for m in store.mappings()}
    candidates = {}
    for person in target:
        local = person.get("userPrincipalName", "").split("@")[0].lower()
        if local:
            candidates.setdefault(local, []).append(person)
    output = []
    for source in store.objects(rid, "source", "directory", ""):
        selected = by_id.get(mappings.get(source["id"]))
        local = source.get("userPrincipalName", "").split("@")[0].lower()
        suggestions = candidates.get(local, []) if local else []
        output.append({"sourceId": source["id"], "sourceName": source.get("displayName"),
                       "sourceEmail": source.get("userPrincipalName"), "userType": source.get("userType"),
                       "targetId": selected["id"] if selected else None,
                       "targetEmail": selected.get("userPrincipalName") if selected else None,
                       "confirmed": bool(selected),
                       "suggestedTargetId": suggestions[0]["id"] if len(suggestions) == 1 else None,
                       "differences": profile_diff(source, selected) if selected else [],
                       "profileState": "NOT_COMPARED" if not selected else "DIFFERS" if profile_diff(source, selected) else "MATCHED_SNAPSHOT"})
    return output


def validate_mapping(store, source_id, target_id):
    inventory = store.latest_inventory()
    if not inventory or inventory["state"] in ("QUEUED", "RUNNING", "STOPPING"):
        raise ValueError("finish discovery before confirming identities")
    if inventory["config_hash"] != fingerprint(store.config()):
        raise ValueError("connections changed; rediscover both tenants first")
    for side, oid in (("source", source_id), ("target", target_id)):
        if not any(p["id"] == oid for p in store.objects(inventory["id"], side, "directory", "")):
            raise ValueError(f"the {side} user was not found in the latest inventory")


def build_plan(store):
    config = store.config()
    inventory = store.latest_inventory()
    rows = store.coverage_rows(inventory["id"], 200, findings_only=True) if inventory else []
    summaries, totals = store.coverage_summary(inventory["id"]) if inventory else ([], {})
    current = bool(inventory and inventory["config_hash"] == fingerprint(config))
    identities = identity_preview(store, inventory) if current else []
    workloads = []
    for spec in WORKLOADS:
        sides = {}
        for side in ("source", "target"):
            summary = next((r for r in summaries if r["side"] == side and r["workload"] == spec["id"]), {})
            sides[side] = {"state": "STALE" if not current and inventory else
                           "NOT_IMPLEMENTED" if not spec["discoveryImplemented"] else
                           "NOT_CHECKED" if not summary else "PARTIAL" if summary["incomplete"] else "INVENTORIED",
                           "records": summary.get("records", 0),
                           "scopes": summary.get("scopes", 0), "incompleteScopes": summary.get("incomplete", 0)}
        blockers = []
        if not current:
            blockers.append("Fresh discovery for these connections is required")
        if config.get("mode") == "preserve_source" and spec["sourceChanges"]:
            blockers.append("Native move changes the source and is excluded by preserve-source mode")
        if config.get("source", {}).get("cloud") != config.get("target", {}).get("cloud"):
            blockers.append("Cross-cloud compatibility has not been qualified")
        if not config.get("target", {}).get("region"):
            blockers.append("Required destination geography has not been specified")
        if any(s["state"] != "INVENTORIED" for s in sides.values()):
            blockers.append("Discovery does not cover every required scope")
        blockers.append("Transfer adapter is not qualified for production")
        if spec["id"] == "sharepoint":
            blockers.append("Confirm EA/license eligibility and native target-site absence, or qualify the copy route")
        if spec["id"] == "chats":
            blockers.append("Confirm orchestrator licensing, MailUser identity preparation and meeting dependencies")
        workloads.append({**spec, "inventory": sides, "blockers": blockers, "verification": "NOT_RUN", "ready": False})
    gaps = [r for r in rows if r["state"] not in ("COMPLETE",)]
    return {"catalogVersion": CATALOG_VERSION, "inventoryId": inventory["id"] if inventory else None,
            "inventoryCurrent": current, "workloads": workloads, "identities": identities,
            "targetUsers": [{"id": p["id"], "email": p.get("userPrincipalName"), "name": p.get("displayName")}
                            for p in store.objects(inventory["id"], "target", "directory", "")] if current else [],
            "findings": gaps, "findingTotals": totals, "findingCount": sum(totals.values()), "findingsTruncated": sum(totals.values()) > len(gaps),
            "productionReady": False, "locationVerified": False,
            "summary": "Discovery and profile sandbox operations are implemented. Full workload migrations are not qualified.",
            "planHash": fingerprint({"config": config, "inventory": inventory["id"] if inventory else None,
                                     "mappings": store.mappings(), "catalog": CATALOG_VERSION}),
            "cutover": {"ready": False, "mode": "final_catchup_then_stop", "blockers": [
                "Per-workload geography evidence is required", "Licensing, holds and target collisions need preflight",
                "Transfer routes and final catch-up must be qualified", "No DNS or domain changes are automated in this build"]}}
