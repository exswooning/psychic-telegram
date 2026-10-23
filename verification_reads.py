"""Read fallbacks that retain Google error reasons and never downgrade fidelity."""

from __future__ import annotations

import base64
import json
import re


def status_of(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is not None:
        return int(status)
    match = re.search(r"(?:HTTP|HttpError)\s+(\d{3})", str(exc))
    return int(match[1]) if match else None


def error_detail(exc):
    """Keep the API message, rather than truncating its URL before the reason."""
    try:
        raw = getattr(exc, "content", b"")
        error = json.loads(raw).get("error", {})
        reasons = ",".join(x.get("reason", "") for x in error.get("errors", []))
        return f"HTTP {status_of(exc)} {error.get('status', '')} {reasons}: {error.get('message', '')}"[
            :1000
        ]
    except (ValueError, TypeError, AttributeError):
        return str(exc)[:1000]


def gmail_raw(svc, mid, execute=lambda fn: fn()):
    """Retry item-level 400s directly, outside a multipart batch, at most twice.

    These reads have succeeded directly for all three affected health items.
    Metadata or snippets are never substituted for a complete raw message.
    """
    for attempt in range(3):
        try:
            message = execute(
                lambda: (
                    svc.users()
                    .messages()
                    .get(userId="me", id=mid, format="raw")
                    .execute()
                )
            )
            raw = message.get("raw")
            if not isinstance(raw, (str, bytes)) or not raw:
                raise RuntimeError(f"Gmail {mid} returned no complete raw message")
            encoded = raw.encode() if isinstance(raw, str) else raw
            decoded = base64.b64decode(
                encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            return decoded, message
        except Exception as exc:
            if status_of(exc) != 400 or attempt == 2:
                raise RuntimeError(
                    f"Gmail raw read {mid}: {error_detail(exc)}"
                ) from exc


def sheet_values(svc, file_id, pace=lambda op: None):
    """Read GRID cells normally and DATA_SOURCE tabs using grid data, not A1 ranges.

    Connected Sheets DATA_SOURCE tabs reject values.batchGet("'Data'") with
    INVALID_ARGUMENT. Their calculated output is not a writable formula. Inspect
    their declared column formulas separately; never silently skip an entire tab.
    """
    pace("read")
    meta = (
        svc.spreadsheets()
        .get(spreadsheetId=file_id, fields="sheets.properties")
        .execute()
    )
    result = []
    for sheet in meta.get("sheets", []):
        prop = sheet["properties"]
        sid, title = prop.get("sheetId"), prop["title"]
        kind = prop.get("sheetType", "GRID")
        if kind == "GRID":
            pace("read")
            got = (
                svc.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=file_id,
                    ranges=["'" + title.replace("'", "''") + "'"],
                    valueRenderOption="FORMULA",
                )
                .execute()
            )
            for vr in got.get("valueRanges", []):
                result.append({**vr, "sheetId": sid, "sheetType": kind})
        elif kind == "DATA_SOURCE":
            columns = prop.get("dataSourceSheetProperties", {}).get("columns", [])
            result.append(
                {
                    "sheetId": sid,
                    "sheetType": kind,
                    "values": [[c["formula"]] for c in columns if c.get("formula")],
                }
            )
            # Data-source output is exposed through GridData with an explicit
            # sheet ID; it is not a regular A1 values range.
            pace("read")
            got = (
                svc.spreadsheets()
                .getByDataFilter(
                    spreadsheetId=file_id,
                    body={
                        "dataFilters": [{"gridRange": {"sheetId": sid}}],
                        "includeGridData": True,
                    },
                    fields="sheets(data(startRow,startColumn,rowData(values(userEnteredValue,hyperlink))))",
                )
                .execute()
            )
            for s in got.get("sheets", []):
                for block in s.get("data", []):
                    values = []
                    for row in block.get("rowData", []):
                        values.append(
                            [
                                str(
                                    c.get("userEnteredValue", {}).get("formulaValue")
                                    or c.get("userEnteredValue", {}).get("stringValue")
                                    or c.get("hyperlink")
                                    or ""
                                )
                                for c in row.get("values", [])
                            ]
                        )
                    result.append({"sheetId": sid, "sheetType": kind, "values": values})
        else:
            raise RuntimeError(
                f"Unsupported sheet type {kind!r} in {title!r}; link coverage is incomplete"
            )
    return result
