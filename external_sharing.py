"""Check new-user access to the same external original and its target shortcut."""
from __future__ import annotations


def inspect(verifier, result, items):
    from external_access import inspect_one
    from verification_reads import error_detail
    selected = {sid: (tid, name) for kind in ("file", "folder")
                for sid, tid, name in verifier._pairs(kind, sample=True)}
    evidence = {"strategy": "original_with_private_fallback", "inScope": len(items),
                "attempted": 0, "compared": 0, "matched": 0,
                "unverified": 0, "ownerActions": 0, "items": []}
    sharing = result.setdefault("sharing", {"filesCompared": 0, "matched": 0,
                                            "notReproduced": 0})
    recognized = set()
    for item in items:
        sid = item["id"]
        if sid not in selected:
            continue
        tid, name = selected[sid]
        evidence["attempted"] += 1
        row = {"source": sid, "target": tid, "name": name, "status": "NOT_VERIFIED"}
        try:
            row.update(inspect_one(verifier.auth, verifier.db, verifier.settings,
                verifier.src_user, verifier.tgt_user, sid, tid, verifier._x))
            if row["shortcutState"] == "MATCHED":
                recognized.add(row["shortcutId"])
            if row.get("targetRole"):
                evidence["compared"] += 1
            if row["originalAccessVerified"] and row["fallbackState"] == "PRIVATE_COPY_RETAINED":
                row["status"] = "MATCHED"
                evidence["matched"] += 1
            elif row["accessState"] == "OWNER_ACTION_REQUIRED":
                from copy_resolution import record
                row["copyResolution"] = record(verifier, result, sid, tid)
                evidence["ownerActions"] += 1
                result["notCopied"].append({"type": "external_access", "id": sid,
                    "target": tid, "copyResolution": row["copyResolution"],
                    "status": "OWNER_ACTION_REQUIRED", "targetUser": verifier.tgt_user,
                    "owners": row["owners"], "requiredRole": row["sourceRole"],
                    "requiredAdditionalRoles": row["sourceAdditionalRoles"],
                    "requiredAccessExpires": row["sourceAccessExpires"],
                    "error": row["action"]})
            elif row["accessState"] == "MATCHED":
                row["status"] = "DIFFERENCES"
                diffs = []
                if row["shortcutState"] != "MATCHED":
                    diffs.append("external original shortcut missing or placement differs")
                if row["fallbackState"] != "PRIVATE_COPY_RETAINED":
                    diffs.append("external fallback has unreviewed sharing")
                result["differences"].append({"source": sid, "target": tid, "item": name,
                    "scope": "external-original-access", "diffs": diffs})
        except Exception as exc:
            row.update(status="INCOMPLETE", reason=error_detail(exc))
            from copy_resolution import record
            row["copyResolution"] = record(verifier, result, sid, tid)
            result["errors"].append(f"sharing of {sid}: original access inspection failed: {row['reason']}")
        if row["status"] != "MATCHED":
            evidence["unverified"] += 1
        evidence["items"].append(row)
    # Exclude only independently validated reference IDs, never name/checksum matches.
    for category in ("extras", "duplicates"):
        result[category] = [r for r in result.get(category, []) if r.get("target") not in recognized]
    evidence["notChecked"] = evidence["inScope"] - evidence["attempted"]
    result["externalSharing"] = evidence
    sharing["external"] = evidence
    return evidence
