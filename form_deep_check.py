"""Deep check of every migrated Google Form of a user: fields, settings, responses and the response Sheet.

Read-only on both tenants. For each Form the ledger maps:
  * items are LINED UP by content (difflib over title+kind), so one missing question reads as one
    missing question, not "every item after it differs"; each matched pair is compared field by field
    (type, required, options, quiz points/answers, branching, images/video, description);
  * the Form's own settings (quiz, email collection) and title/description;
  * responses: how many the source holds and the target holds (past responses are not carried by any
    API -- reported, not a difference);
  * the responses Sheet: the source Form's linkedSheetId, whether that Sheet has a target copy, its
    cell values compared tab by tab, and whether the target Form is linked to it (relinked by hand,
    form_relink.py; a relink adds an empty "Form Responses" tab on the Sheet).

    python form_deep_check.py --account-id 3 --user a@src [--user ...] [--json out.json]
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys

FORM = "application/vnd.google-apps.form"
IGNORE = {"itemId", "questionId", "kix", "sourceUri", "contentUri", "properties"}


def _clean(x):
    """Ids, image URLs and the like are per-copy; strip them before comparing."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items() if k not in IGNORE and not k.endswith("Id")}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def _kind(item: dict) -> str:
    for k in ("questionItem", "questionGroupItem", "pageBreakItem", "textItem", "imageItem", "videoItem"):
        if k in item:
            q = (item[k].get("question") or {}) if k == "questionItem" else {}
            sub = next((s for s in ("choiceQuestion", "textQuestion", "scaleQuestion", "dateQuestion",
                                    "timeQuestion", "fileUploadQuestion", "rowQuestion", "ratingQuestion") if s in q), "")
            return f"{k}:{sub}"
    return "unknown"


def _sig(item: dict) -> str:
    return f"{_kind(item)}|{(item.get('title') or '').strip()}"


def _field_diffs(a: dict, b: dict, prefix: str = "") -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if isinstance(x, dict) and isinstance(y, dict):
            out += _field_diffs(x, y, f"{prefix}{k}.")
        elif x != y:
            out.append(f"{prefix}{k}: {json.dumps(x, ensure_ascii=False)[:80]} -> {json.dumps(y, ensure_ascii=False)[:80]}")
    return out


def compare_items(src_items: list, tgt_items: list) -> dict:
    """Missing, extra and changed items, aligned by content."""
    a, b = [_sig(i) for i in src_items], [_sig(i) for i in tgt_items]
    missing, extra, changed = [], [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal" or op == "replace":
            for x, y in zip(src_items[i1:i2], tgt_items[j1:j2]):
                d = _field_diffs(_clean(x), _clean(y)) if _sig(x) == _sig(y) or op == "replace" else []
                if op == "replace" and _sig(x) != _sig(y):
                    d = [f"became {_sig(y)}"] + d
                if d:
                    changed.append({"item": (x.get("title") or "untitled")[:60], "kind": _kind(x), "diffs": d[:8]})
            if op == "replace":
                missing += [(i.get("title") or "untitled")[:60] for i in src_items[i1 + (j2 - j1):i2]]
                extra += [(i.get("title") or "untitled")[:60] for i in tgt_items[j1 + (i2 - i1):j2]]
        elif op == "delete":
            missing += [f"{(i.get('title') or 'untitled')[:60]} ({_kind(i)})" for i in src_items[i1:i2]]
        elif op == "insert":
            extra += [f"{(i.get('title') or 'untitled')[:60]} ({_kind(i)})" for i in tgt_items[j1:j2]]
    return {"source": len(src_items), "target": len(tgt_items), "missing": missing, "extra": extra, "changed": changed}


def _sheet_values(sheets, sid: str) -> dict:
    meta = sheets.spreadsheets().get(spreadsheetId=sid, fields="sheets(properties(title))").execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    out = {}
    if titles:
        got = sheets.spreadsheets().values().batchGet(spreadsheetId=sid, ranges=[f"'{t}'" for t in titles]).execute()
        for t, vr in zip(titles, got.get("valueRanges", [])):
            out[t] = vr.get("values", [])
    return out


def check_form(auth, db, source_user: str, target_user: str, sid: str, tid: str) -> dict:
    rep: dict = {"source": sid, "target": tid}
    sf, tf = auth.api("source", "forms", source_user), auth.api("target", "forms", target_user)
    s, t = sf.forms().get(formId=sid).execute(), tf.forms().get(formId=tid).execute()
    rep["title"] = (s.get("info") or {}).get("title")
    rep["info"] = _field_diffs({k: (s.get("info") or {}).get(k) for k in ("title", "description")},
                               {k: (t.get("info") or {}).get(k) for k in ("title", "description")})
    rep["settings"] = _field_diffs(_clean(s.get("settings") or {}), _clean(t.get("settings") or {}))
    rep["items"] = compare_items(s.get("items") or [], t.get("items") or [])
    for side, svc, fid in (("sourceResponses", sf, sid), ("targetResponses", tf, tid)):
        try:
            n, tok = 0, None
            while True:
                r = svc.forms().responses().list(formId=fid, pageToken=tok, pageSize=5000).execute()
                n += len(r.get("responses", []))
                tok = r.get("nextPageToken")
                if not tok:
                    break
            rep[side] = n
        except Exception as exc:      # noqa: BLE001 - the scope may not be granted: say so
            rep[side] = f"could not read: {str(exc)[:80]}"
    linked = s.get("linkedSheetId")
    sheet: dict = {"sourceSheet": linked, "targetFormLinkedTo": t.get("linkedSheetId")}
    if linked:
        copy = db.target_for_source_id(linked)
        sheet["sheetCopied"] = bool(copy)
        sheet["targetSheet"] = copy
        sheet["targetFormLinkedToCopy"] = bool(copy) and t.get("linkedSheetId") == copy
        if copy:
            try:
                a = _sheet_values(auth.api("source", "sheets", source_user), linked)
                b = _sheet_values(auth.api("target", "sheets", target_user), copy)
                tabs = {}
                for tab in sorted(set(a) | set(b)):
                    x, y = a.get(tab), b.get(tab)
                    if x is None:
                        tabs[tab] = "only on the target" + (" (the empty tab a relink adds)" if not any(y or []) or tab.startswith("Form Responses") else "")
                    elif y is None:
                        tabs[tab] = "missing on the target"
                    else:
                        cells = sum(1 for r in range(max(len(x), len(y))) for c in range(max(len(x[r]) if r < len(x) else 0, len(y[r]) if r < len(y) else 0))
                                    if (x[r][c] if r < len(x) and c < len(x[r]) else "") != (y[r][c] if r < len(y) and c < len(y[r]) else ""))
                        tabs[tab] = "identical" if not cells else f"{cells} cell(s) differ ({len(x)} vs {len(y)} rows)"
                sheet["tabs"] = tabs
            except Exception as exc:      # noqa: BLE001
                sheet["tabs"] = f"could not read: {str(exc)[:100]}"
    rep["responsesSheet"] = sheet
    it = rep["items"]
    rep["parity"] = not (rep["info"] or rep["settings"] or it["missing"] or it["extra"] or it["changed"])
    return rep


def check_user_forms(auth, db, user: str, target: str) -> list[dict]:
    """Every mapped Form of one user, deep-checked (what main() prints, as data)."""
    out = []
    drive = auth.source_drive(user)
    for sid, tid in db.conn.execute("SELECT source_id, target_id FROM id_mapping WHERE source_user=? "
                                    "AND type='file'", (user,)).fetchall():
        try:
            if drive.files().get(fileId=sid, fields="mimeType", supportsAllDrives=True).execute().get("mimeType") != FORM:
                continue
            r = check_form(auth, db, user, target, sid, tid)
        except Exception as exc:      # noqa: BLE001
            r = {"source": sid, "target": tid, "error": str(exc)[:200], "parity": False}
        r["user"] = user
        out.append(r)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--user", action="append", required=True)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    from auth import AuthManager
    from config import Settings
    from db import MigrationDB
    st = Settings(account_id=args.account_id)
    db, auth = MigrationDB(st.db_path), AuthManager(st)
    ledger = {r["source_email"].lower(): r["target_email"] for r in db.all_identities() if r["entity_type"] == "user"}
    report = []
    for u in args.user:
        tgt = ledger[u.lower()]
        rows = db.conn.execute("SELECT source_id, target_id FROM id_mapping WHERE source_user=? AND type='file'", (u,)).fetchall()
        drive = auth.source_drive(u)
        for sid, tid in rows:
            try:
                if drive.files().get(fileId=sid, fields="mimeType", supportsAllDrives=True).execute().get("mimeType") != FORM:
                    continue
                r = check_form(auth, db, u, tgt, sid, tid)
            except Exception as exc:      # noqa: BLE001
                r = {"source": sid, "target": tid, "error": str(exc)[:200], "parity": False}
            r["user"] = u
            report.append(r)
            it = r.get("items", {})
            sh = r.get("responsesSheet", {})
            print(f"{u.split('@')[0]} | {r.get('title', r.get('error', ''))[:45]!r}: parity={r['parity']} "
                  f"items {it.get('source')}->{it.get('target')} missing={len(it.get('missing', []))} "
                  f"extra={len(it.get('extra', []))} changed={len(it.get('changed', []))} settings={len(r.get('settings', []))} "
                  f"| responses {r.get('sourceResponses')}->{r.get('targetResponses')} "
                  f"| sheet {'none' if not sh.get('sourceSheet') else ('copied' if sh.get('sheetCopied') else 'NOT copied')}"
                  f"{'' if not sh.get('sourceSheet') else ', linked ' + str(sh.get('targetFormLinkedToCopy'))} {sh.get('tabs', '')}",
                  flush=True)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
    ok = sum(1 for r in report if r.get("parity"))
    print(f"forms: {len(report)}, at parity {ok}, with differences {len(report) - ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
