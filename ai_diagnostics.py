"""
ai_diagnostics.py
=================
"What is actually going on right now?", answered by an LLM reading the
ledger and the log instead of a human reading both.

Why it is worth having. Diagnosing this migration means correlating four
things that live in four places: the process table (is anything running),
migration.db (what landed, what failed), the run log (why it failed), and
metrics (how fast). Every investigation in this project has been a person
joining those by hand -- and the expensive mistakes were all *missed*
signals rather than misread ones: 20,714 ACL grants 404ing under a "0 file
failures" summary, 93 files left world-readable, a benchmark reporting PASS
for a run that copied nothing. A summariser that reads all four at once and
is told not to invent anything is a cheap second pair of eyes on exactly
that failure mode.

Two rules it is built around
----------------------------
**It never decides anything.** Output is prose for a human. Nothing here
gates a migration, retries an item, or writes to a tenant. An LLM that can
be wrong is fine as a reader and unacceptable as a control.

**It says what it sends.** `gather_context()` is the whole payload and it
is returned to the caller *before* any request goes out, so the UI can show
the operator the actual bytes leaving the building. That matters: the log
tail contains real user email addresses and real file names, and shipping
those to a third-party API is a decision the operator makes knowingly, not
one buried behind an "Analyze" button.

stdlib-only, so webui.py (which has no third-party imports by design) can
share it with api_server.py rather than keeping a second copy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.request

DEFAULT_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are a migration engineer's diagnostic assistant. The user runs a "
    "Google Workspace tenant-to-tenant migration. You are given a structured "
    "snapshot of live state (running processes, per-user progress, failure "
    "counts grouped by cause) and the tail of the run log.\n\n"
    "Produce concise Markdown with these sections, omitting any that have no "
    "content:\n"
    "**Status** — one or two lines: is it healthy, and why.\n"
    "**Problems** — each distinct failure, its count, its likely cause, and "
    "the next action. Order by how much data is at risk, not by count.\n"
    "**Progress** — rates and throughput if the numbers support it.\n\n"
    "Hard rules. Do not invent failures, counts, or causes; every number you "
    "state must appear in the input. If something looks wrong but you cannot "
    "tell why from the data, say that explicitly rather than guessing. If a "
    "count is zero, do not describe it as a problem. Quote the actual log "
    "line when you cite one. Prefer 'the data does not show' over a plausible "
    "story."
)


# ======================================================================
# Key storage
#
# One env.sh entry, read and written the same way by both UIs. Keeping two
# implementations would let webui.py and the control plane disagree about
# whether a key is configured, which is a confusing thing to debug for a
# feature whose whole job is reducing confusion.
# ======================================================================
def read_key(env_path: str) -> str:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export GROQ_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def write_key(env_path: str, key: str) -> None:
    """Merge into env.sh, preserving every other entry.

    Read-modify-write rather than append: setup.sh's service-account paths
    and the tenant domains live in this file, and truncating it to one line
    would take the whole deployment down.
    """
    key = (key or "").strip()
    lines: list[str] = []
    try:
        with open(env_path, encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    except OSError:
        pass
    lines = [ln for ln in lines if not ln.strip().startswith("export GROQ_API_KEY=")]
    lines.append(f"export GROQ_API_KEY={key}")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.environ["GROQ_API_KEY"] = key


# ======================================================================
# Context gathering -- all read-only
# ======================================================================
def _ro(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _running_processes() -> list[str]:
    try:
        ps = subprocess.run(["ps", "-eo", "etime=,args="], capture_output=True,
                            text=True, timeout=10).stdout
    except Exception:      # noqa: BLE001
        return []
    keep = ("main.py migrate", "main.py delta", "benchmark_run.py",
            "acl_audit.py", "reset_target.py", "seed_sandbox.py")
    return [ln.strip() for ln in ps.splitlines()
            if any(k in ln for k in keep) and "grep" not in ln]


def _failures_by_cause(conn: sqlite3.Connection, since_iso: str | None,
                       limit: int = 12) -> list[dict]:
    """Distinct failure causes with counts.

    Grouped rather than listed: 636 identical storageQuotaExceeded lines
    tell an operator one thing, and pasting all 636 into a prompt buys
    nothing but tokens and a worse answer.
    """
    where = "WHERE status='FAILED'"
    args: list = []
    if since_iso:
        where += " AND timestamp >= ?"
        args.append(since_iso)
    rows = conn.execute(
        f"SELECT item_type, error_message, COUNT(*) n FROM audit_log {where} "
        f"GROUP BY item_type, substr(COALESCE(error_message,''), 1, 120) "
        f"ORDER BY n DESC LIMIT ?", (*args, limit)).fetchall()
    return [{"item_type": r["item_type"], "count": r["n"],
             "error": (r["error_message"] or "")[:220]} for r in rows]


def gather_context(db_path: str, log_path: str | None = None,
                   since_iso: str | None = None, tail_lines: int = 120) -> str:
    """The complete payload sent to the model. Returned so it can be shown."""
    out: list[str] = []

    procs = _running_processes()
    out.append("## Running now")
    out.extend(f"- {p}" for p in procs) if procs else out.append("- nothing running")

    try:
        conn = _ro(db_path)
    except Exception as exc:      # noqa: BLE001
        out.append(f"\n## Ledger\n- unreadable: {exc}")
        return "\n".join(out)

    try:
        out.append("\n## Migrated so far (id_mapping = exists on target)")
        for r in conn.execute("SELECT type, COUNT(*) n FROM id_mapping "
                              "GROUP BY type ORDER BY n DESC"):
            out.append(f"- {r['type']}: {r['n']}")

        out.append("\n## Per-user state")
        for r in conn.execute(
                "SELECT source_email, status, services_done FROM identity_map "
                "ORDER BY source_email"):
            out.append(f"- {r['source_email']}: {r['status']} "
                       f"(done: {r['services_done'] or 'none'})")

        fails = _failures_by_cause(conn, since_iso)
        out.append("\n## Failures grouped by cause"
                   + (f" (since {since_iso})" if since_iso else " (all time)"))
        if fails:
            for f in fails:
                out.append(f"- {f['count']}x {f['item_type']}: {f['error']}")
        else:
            out.append("- none")
    finally:
        conn.close()

    if log_path and os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-tail_lines:]
            # The Python-version FutureWarning repeats on every subprocess and
            # is pure noise in a token budget.
            lines = [ln for ln in lines
                     if "FutureWarning" not in ln and "warnings.warn" not in ln]
            out.append(f"\n## Log tail ({os.path.basename(log_path)})")
            out.append("```")
            out.extend(ln.rstrip() for ln in lines)
            out.append("```")
        except OSError as exc:
            out.append(f"\n## Log tail\n- unreadable: {exc}")

    return "\n".join(out)


# ======================================================================
# The call
# ======================================================================
def analyze(context: str, key: str, prompt: str = "",
            model: str | None = None, timeout: int = 90) -> tuple[str, str]:
    """Returns (markdown, error). Never raises -- a diagnostics panel that
    can crash the page it is diagnosing is worse than no panel."""
    if not key:
        return "", "no Groq API key configured"
    body = json.dumps({
        "model": model or DEFAULT_MODEL,
        "temperature": 0,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": (f"{prompt}\n\n" if prompt else "") + context},
        ],
    }).encode()
    req = urllib.request.Request(
        GROQ_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:      # noqa: BLE001
            pass
        return "", f"Groq API error {exc.code}: {detail or exc.reason}"
    except Exception as exc:   # noqa: BLE001 - network failures are normal
        return "", f"could not reach Groq: {exc}"
    try:
        return data["choices"][0]["message"]["content"], ""
    except (KeyError, IndexError, TypeError):
        return "", f"unexpected Groq response: {str(data)[:200]}"
