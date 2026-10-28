"""Read-only evidence for a retained copy; never resolves original access or auth."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from verification_reads import error_detail, status_of


def remember_content(v, sid, tid, source, target, evidence, differences):
    """Keep only this run's comparison, bound to both IDs and the target version."""
    state, scope = "NOT_VERIFIED", "Content equality has not been established."
    if differences:
        state, scope = "DIFFERENCES", "The content comparison found differences."
    elif not evidence.get("error"):
        if evidence.get("nativeFeaturesChecked") and evidence.get("nativeFeaturesMatched"):
            state, scope = "MATCHED_SUPPORTED_NATIVE", evidence.get("how", "Supported native features matched.")
        elif evidence.get("opened"):
            state = "MATCHED_EXPORT" if source.get("mimeType", "").startswith("application/vnd.google-apps.") else "MATCHED_BYTES"
            scope = evidence.get("how", "Compared content matched.")
        elif (source.get("md5Checksum") and source.get("md5Checksum") == target.get("md5Checksum")
              and source.get("size") is not None and source.get("size") == target.get("size")
              and source.get("mimeType") == target.get("mimeType")):
            state, scope = "MATCHED_CHECKSUM", "Size and MD5 matched; the file was not downloaded for this comparison."
    cache = getattr(v, "_retained_copy_content", None)
    if cache is None:
        cache = v._retained_copy_content = {}
    cache[(sid, tid)] = {"contentState": state, "contentScope": scope,
                         "targetVersion": target.get("version")}


def record(v, result, sid, tid, *, source_unreadable=False):
    """Confirm the mapped target is a live owned copy, even if the original is unreadable."""
    rows = result.setdefault("copyResolutions", [])
    index = getattr(v, "_retained_resolution_index", None)
    if index is None or index[0] is not result:
        index = v._retained_resolution_index = (result, {(r["source"], r["target"]): r for r in rows})
    if (sid, tid) in index[1]:
        return index[1][(sid, tid)]
    row = {"source": sid, "target": tid, "state": "COPY_NOT_VERIFIED",
           "contentState": "NOT_VERIFIED", "contentScope": "Content equality has not been established.",
           "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "sharingResolved": False, "authorizationResolved": False,
           "label": "Copy not verified — original access unresolved"}
    try:
        if sid == tid or v.db.get_target_id(v.src_user, sid, "file") != tid:
            raise RuntimeError("No distinct migrated content-copy mapping for this item")
        target = v.auth.target_drive(v.tgt_user)
        meta = v._x(lambda: target.files().get(fileId=tid, supportsAllDrives=True,
            fields="id,name,mimeType,trashed,ownedByMe,version").execute())
        if meta.get("id") != tid:
            raise RuntimeError("The target returned a different item identity")
        if meta.get("trashed"):
            row.update(state="COPY_MISSING", label="Copy in target trash — original access unresolved")
        elif meta.get("ownedByMe") is not True or meta.get("mimeType") in (
                "application/vnd.google-apps.shortcut", "application/vnd.google-apps.folder"):
            row["note"] = "A target-owned content copy was not confirmed; a shortcut is not a content copy."
        else:
            row.update(state="COPY_EXISTS", name=meta.get("name"),
                       targetUrl="https://drive.google.com/file/d/" + quote(tid, safe="") + "/view",
                       label="Copy exists — source check unavailable" if source_unreadable else
                             "Copy exists — original access unresolved",
                       note="The retained copy is readable on the target. Original ownership, collaborators and live access remain separate findings.")
            proof = getattr(v, "_retained_copy_content", {}).get((sid, tid))
            if proof and proof.get("targetVersion") and proof["targetVersion"] == meta.get("version"):
                row.update(contentState=proof["contentState"], contentScope=proof["contentScope"])
    except Exception as exc:
        if status_of(exc) == 404:
            row["label"] = "Copy unavailable — original access unresolved"
        row["note"] = error_detail(exc)
    rows.append(row)
    index[1][(sid, tid)] = row
    return row
