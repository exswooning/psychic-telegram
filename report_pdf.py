"""
report_pdf.py -- a run report as a PDF, for two readers.

human   leads with the verdict and what to do, in plain words.
claude  leads with what a fix needs: the failing benchmarks with exact values
        and the metric each was read from, the error families with real
        messages and the users they hit, configuration, environment, and the
        tail of the run's own log. Uncompressed, so any extractor can read it.

reportlab's built-in fonts are WinAnsi (cp1252). Anything outside that -- an
arrow, a user's non-Latin name in an error message -- would print as a black
box, so text is squeezed through cp1252 first; that is a display decision and
the JSON beside these keeps every byte.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, Preformatted, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

INK, MUTED, RULE = colors.HexColor("#1d2939"), colors.HexColor("#667085"), colors.HexColor("#d0d5dd")
STATUS_COLOR = {"pass": colors.HexColor("#1f7a4d"), "warn": colors.HexColor("#b7791f"),
                "fail": colors.HexColor("#b42318"), "unknown": colors.HexColor("#667085")}
VERDICT_COLOR = {"PASS": STATUS_COLOR["pass"], "FAIL": STATUS_COLOR["fail"],
                 "UNVERIFIED": STATUS_COLOR["warn"]}
VERDICT_LINE = {
    "PASS": "Every required benchmark was checked and none failed.",
    "FAIL": "At least one benchmark failed. Fix that before relying on this migration.",
    "UNVERIFIED": "No benchmark failed, but at least one required check could not be made. "
                  "That is not a pass.",
}
CATEGORIES = ("health", "reliability", "performance", "fidelity")


def _t(x) -> str:
    """Text safe for the built-in fonts and for Paragraph markup."""
    s = "" if x is None else str(x)
    for a, b in (("→", "->"), ("≥", ">="), ("≤", "<="), ("…", "..."),
                 ("✓", "ok"), ("✗", "x")):
        s = s.replace(a, b)
    return escape(s.encode("cp1252", "replace").decode("cp1252"))


def _n(v) -> str:
    return "not measured" if v is None else f"{v:,}" if isinstance(v, int) else _t(v)


def _dur(sec) -> str:
    if not sec:
        return "not recorded"
    h, rem = divmod(int(sec), 3600)
    return f"{h}h {rem // 60:02d}m" if h else f"{rem // 60}m {rem % 60:02d}s"


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold", fontSize=20,
                                textColor=INK, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=10, textColor=MUTED, spaceAfter=8),
        "h": ParagraphStyle("h", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12.5,
                            textColor=INK, spaceBefore=14, spaceAfter=5),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=9.5, leading=13, textColor=INK),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=8.5, leading=11, textColor=INK),
        "cellb": ParagraphStyle("cb", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8.5,
                                leading=11, textColor=INK),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontSize=8, leading=10.5, textColor=MUTED),
        "mono": ParagraphStyle("m", parent=base["Code"], fontName="Courier", fontSize=7.4, leading=9.2,
                               textColor=INK),
    }


def _table(rows, widths, head=True, zebra=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if head:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f4f7")),
                  ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK)]
    if zebra:
        style += [("ROWBACKGROUNDS", (0, 1 if head else 0), (-1, -1), [colors.white, colors.HexColor("#fafafa")])]
    t.setStyle(TableStyle(style))
    return t


def _status_cell(status, st):
    return Paragraph(f'<font color="{STATUS_COLOR[status].hexval()}"><b>{status.upper()}</b></font>', st["cell"])


def _banner(report, st, width):
    v = report["verdict"]
    counts = report["benchmarks"]["counts"]
    t = Table([[Paragraph(f'<font color="white" size="20"><b>{v}</b></font>', st["body"]),
                Paragraph(f'<font color="white">{_t(VERDICT_LINE[v])}<br/>'
                          f'{counts["pass"]} passed, {counts["warn"]} warning(s), '
                          f'{counts["fail"]} failed, {counts["unknown"]} not checked.</font>', st["body"])]],
              colWidths=[38 * mm, width - 38 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), VERDICT_COLOR[v]),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    return t


def _bench_rows(report, st, only=None):
    order = {c: i for i, c in enumerate(CATEGORIES)}
    rows = sorted(report["benchmarks"]["results"], key=lambda r: order.get(r["category"], 9))
    if only:
        rows = [r for r in rows if r["status"] in only]
    return rows


def _pair_run(facts):
    run = facts.get("run") or {}
    return run, facts.get("ledger") or {}, facts.get("users") or {}


# ---------------------------------------------------------------------------
def _human(report, st, width):
    facts = report["facts"]
    run, ledger, users = _pair_run(facts)
    tenants = report.get("tenants") or {}
    out = [Paragraph(f"{_t(report['kind'].title())} report", st["title"]),
           Paragraph(f"{_t(tenants.get('source') or '?')} -> {_t(tenants.get('target') or '?')}"
                     f" &nbsp;|&nbsp; generated {_t(report['generatedAt'])} &nbsp;|&nbsp; {_t(report['id'])}", st["sub"]),
           _banner(report, st, width)]

    head = facts.get("headline") or {}
    glance = [[Paragraph("<b>At a glance</b>", st["cell"]), ""],
              ["Users", f"{_n(users.get('done'))} done, {_n(users.get('failed'))} failed, "
                        f"{_n(users.get('pending'))} pending, of {_n(users.get('total'))}"],
              ["Items migrated", _n(ledger.get("succeeded"))],
              ["Items failed / blocked", f"{_n(ledger.get('failed'))} / {_n(ledger.get('blocked'))}"],
              ["Skipped on purpose", f"{_n(ledger.get('skipped'))} (a decision, not a failure)"],
              ["Data moved", _t(head.get("dataMigrated") or "not measured")],
              ["Duration", _dur(run.get("durationSec")) + ("" if run.get("timingSource") == "job" else
                                                           " (from the ledger's first and last row)" if run.get("durationSec") else "")],
              ["Throughput", (f"{facts['perf']['itemsPerMin']:.0f} items/min" if (facts.get("perf") or {}).get("itemsPerMin")
                              else "not measured (needs the job's own start and finish)")]]
    g = Table([[Paragraph(_t(a), st["cellb"] if i else st["cell"]), Paragraph(_t(b), st["cell"])]
               for i, (a, b) in enumerate(glance[1:])], colWidths=[42 * mm, width - 42 * mm])
    g.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    out += [Paragraph("At a glance", st["h"]), g]

    by_type = ledger.get("byType") or {}
    if by_type:
        rows = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in ("Item type", "Migrated", "Failed", "Blocked", "Skipped")]]
        for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]["succeeded"]):
            rows.append([Paragraph(_t(k), st["cell"])] + [Paragraph(f"{v[x]:,}", st["cell"])
                         for x in ("succeeded", "failed", "blocked", "skipped")])
        out += [Paragraph("By item type", st["h"]), _table(rows, [width - 4 * 26 * mm] + [26 * mm] * 4)]

    rows = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in ("Check", "Result", "Target", "Status")]]
    for r in _bench_rows(report, st):
        rows.append([Paragraph(f"<b>{_t(r['category'].title())}</b> &nbsp;{_t(r['label'])}", st["cell"]),
                     Paragraph(_t(r["display"]), st["cell"]), Paragraph(_t(r["threshold"]), st["small"]),
                     _status_cell(r["status"], st)])
    out += [Paragraph("Benchmarks", st["h"]), _table(rows, [width - 122 * mm, 30 * mm, 60 * mm, 32 * mm])]

    fam = facts.get("failures") or []
    if fam:
        rows = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in ("Count", "Users", "Type", "What Google said")]]
        for f in fam[:10]:
            rows.append([Paragraph(f"{f['count']:,}", st["cell"]), Paragraph(f"{f['users']:,}", st["cell"]),
                         Paragraph(_t(f["itemType"]), st["cell"]), Paragraph(_t(f["message"]), st["cell"])])
        # Heading and table together: a heading stranded at the foot of a page
        # over a table on the next reads as a section with nothing in it.
        out.append(KeepTogether([Paragraph("What went wrong", st["h"]),
                                 _table(rows, [20 * mm, 18 * mm, 24 * mm, width - 62 * mm])]))
    else:
        out.append(KeepTogether([Paragraph("What went wrong", st["h"]),
                                 Paragraph("No failures are recorded in the ledger.", st["body"])]))

    unv = [r for r in report["benchmarks"]["results"] if r["status"] == "unknown" and r["required"]]
    if unv:
        out.append(Paragraph("Not verified", st["h"]))
        out.append(Paragraph("These checks could not be made, so they are neither passed nor failed: "
                             + "; ".join(_t(r["label"]) for r in unv) + ".", st["body"]))

    out.append(Paragraph("What to do next", st["h"]))
    for step in report["nextSteps"] or ["Nothing outstanding."]:
        out.append(Paragraph(f"- {_t(step)}", st["body"]))
    out += [Spacer(1, 10), Paragraph(
        "How to read this. Counts come from the migration ledger, which records what the engine did; "
        "the fidelity checks compare the two tenants directly, because the ledger cannot say what is "
        "actually there. SKIPPED means the engine decided not to copy something (too large, "
        "unexportable) and is not a failure. UNVERIFIED means a check could not be made, which is not "
        "the same as passing it.", st["small"])]
    return out


def _claude(report, st, width):
    facts = report["facts"]
    run, ledger, users = _pair_run(facts)
    out = [Paragraph(f"Run report for Claude Code - {_t(report['id'])}", st["title"]),
           Paragraph("Everything below is measured from the ledger, the job record and the host unless "
                     "labelled otherwise. Read the failing and unverified benchmarks first, then the "
                     "failure families, then the log tail. Before deploying any fix, ask the operator: a "
                     "deploy restarts services and can kill a running seed or migration (see CLAUDE.md).",
                     st["sub"]),
           _banner(report, st, width)]

    def kv(pairs):
        t = Table([[Paragraph(f"<b>{_t(k)}</b>", st["cell"]), Paragraph(_t(v), st["cell"])] for k, v in pairs],
                  colWidths=[40 * mm, width - 40 * mm])
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return t

    out += [Paragraph("Identity", st["h"]), kv([
        ("run id", report["id"]), ("kind", report["kind"]), ("generated", report["generatedAt"]),
        ("tenants", f"{(report.get('tenants') or {}).get('source')} -> {(report.get('tenants') or {}).get('target')}"),
        ("account", report.get("accountId")), ("exit code", run.get("returnCode")),
        ("started / finished", f"{run.get('startedAt')} / {run.get('finishedAt')} ({run.get('timingSource')})"),
        ("duration", _dur(run.get("durationSec"))),
        ("ledger", (facts.get("paths") or {}).get("ledger"))])]

    bad = _bench_rows(report, st, only=("fail", "warn", "unknown"))
    out.append(Paragraph("Benchmarks that are not a clean pass", st["h"]))
    if bad:
        rows = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in ("Status", "Check", "Value", "Threshold / metric / why")]]
        for r in bad:
            rows.append([_status_cell(r["status"], st), Paragraph(f"<b>{_t(r['id'])}</b><br/>{_t(r['category'])}"
                                                                 f"{' (required)' if r['required'] else ''}", st["small"]),
                         Paragraph(_t(r["display"]), st["cell"]),
                         Paragraph(f"{_t(r['threshold'])}<br/><font color='#667085'>metric {_t(r['metric'])}<br/>"
                                   f"{_t(r['why'])}</font>", st["cell"])])
        out.append(_table(rows, [23 * mm, 50 * mm, 24 * mm, width - 97 * mm]))
    else:
        out.append(Paragraph("None: every benchmark passed.", st["body"]))

    out.append(Paragraph("Failure families (audit_log, FAILED and BLOCKED)", st["h"]))
    fam = facts.get("failures") or []
    if fam:
        rows = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in ("Count", "Users", "Type", "Message (first 160 chars)")]]
        for f in fam:
            rows.append([Paragraph(f"{f['count']:,}", st["cell"]), Paragraph(f"{f['users']:,}", st["cell"]),
                         Paragraph(_t(f["itemType"]), st["cell"]), Paragraph(_t(f["message"]), st["cell"])])
        out.append(_table(rows, [18 * mm, 16 * mm, 22 * mm, width - 56 * mm]))
    else:
        out.append(Paragraph("None recorded.", st["body"]))

    sus = report.get("suspected") or []
    if sus:
        out.append(Paragraph("Where to look first (pattern match on the messages above, not a diagnosis)", st["h"]))
        for s in sus[:8]:
            out.append(Paragraph(f"- <b>{_t(s['where'])}</b> - {s['failures']:,} failure(s), e.g. "
                                 f"{_t(' | '.join(s['examples']))}", st["body"]))

    out.append(Paragraph("Ledger totals", st["h"]))
    out.append(kv([("succeeded", ledger.get("succeeded")), ("failed", ledger.get("failed")),
                   ("blocked", ledger.get("blocked")), ("skipped (deliberate)", ledger.get("skipped")),
                   ("in progress", ledger.get("inProgress")),
                   ("failure rate", None if ledger.get("failureRate") is None else f"{ledger['failureRate']:.4%}"),
                   ("users", f"{users.get('done')} done / {users.get('failed')} failed / {users.get('pending')} pending "
                             f"/ {users.get('running')} running of {users.get('total')}")]))
    m = facts.get("metrics") or {}
    if m:
        out.append(Paragraph("Metrics snapshot (recorded by the migrating process)", st["h"]))
        out.append(kv([("recorded at", m.get("recordedAt")), ("calls / retries", f"{m.get('calls')} / {m.get('retries')}"),
                       ("retry rate", m.get("retryRate")), ("p50 / p95 / p99 (s)", f"{m.get('p50')} / {m.get('p95')} / {m.get('p99')}"),
                       ("requests/s", m.get("requestsPerSec")), ("quota pushbacks", m.get("pushbacks")),
                       ("limiters", "; ".join(f"{k}: {v.get('rate')}/s, {v.get('backoffs')} backoffs"
                                              for k, v in (m.get("limiters") or {}).items()) or None)]))
    out += [Paragraph("Configuration", st["h"]),
            kv(list((facts.get("config") or {}).items())),
            Paragraph("Environment", st["h"]), kv(list((facts.get("environment") or {}).items()))]
    if facts.get("errors"):
        out += [Paragraph("Sections that could not be gathered", st["h"]),
                kv([(e["section"], e["error"]) for e in facts["errors"]])]

    out.append(Paragraph("Read-only ways to look further", st["h"]))
    ledger_path = (facts.get("paths") or {}).get("ledger") or "<ledger>"
    out.append(Preformatted(
        f"DB='{ledger_path}'\n"
        "sqlite3 \"$DB\" \"SELECT item_type, status, COUNT(*) FROM audit_log GROUP BY 1, 2\"\n"
        "sqlite3 \"$DB\" \"SELECT source_user, item_id, error_message FROM audit_log\n"
        "                  WHERE status = 'FAILED' LIMIT 20\"\n"
        "python main.py report --max-failures 50\n"
        "python main.py status\n"
        "python repair.py     # survey only; --apply changes things, so ask first", st["mono"]))

    tail = (facts.get("transcript") or {}).get("tail") or []
    out.append(Paragraph(f"Log tail (last {len(tail)} lines of the run's transcript)", st["h"]))
    out.append(Preformatted("\n".join(_plain(l) for l in tail) or "(no transcript was available)", st["mono"]))
    return out


def _plain(s: str) -> str:
    return s.encode("cp1252", "replace").decode("cp1252")


def write_pdf(report: dict, path: str, audience: str) -> None:
    if audience not in ("human", "claude"):
        raise ValueError(f"unknown audience {audience!r}")
    st = _styles()
    margin = 16 * mm
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"{report['kind'].title()} report {report['id']}",
                            author="Bitport", pageCompression=0 if audience == "claude" else 1)
    width = A4[0] - 2 * margin

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(margin, 9 * mm, f"{report['id']} - {audience} report - {report['verdict']}")
        canvas.drawRightString(A4[0] - margin, 9 * mm, f"page {d.page}")
        canvas.restoreState()

    story = _human(report, st, width) if audience == "human" else _claude(report, st, width)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
