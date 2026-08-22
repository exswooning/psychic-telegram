"""Run the test suite and turn its output into something a page can show.

The suite is the only evidence that any of this tool's behaviour is what it
claims, and until now it existed solely as scrollback in whoever's terminal
last ran it. An operator deciding whether to trust a migration could not see
whether the thing had been verified at all, let alone when or against what
commit.

Parsing is deliberately separated from running: parse_junit() is a pure
function over XML text, so the shape of a report can be tested without
starting a second pytest inside the first one.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(HERE, "data", "test_report.json")


def parse_junit(xml_text: str) -> dict:
    """JUnit XML -> a summary shaped for display.

    Counts come from the <testsuite> attributes rather than by tallying
    cases: pytest writes the authoritative totals there, and a case that
    errored during collection never becomes a <testcase> element at all, so
    tallying would quietly under-report exactly the failures that matter
    most.
    """
    root = ET.fromstring(xml_text)
    suite = root if root.tag == "testsuite" else (root.find("testsuite") or root)

    def _int(name: str) -> int:
        try:
            return int(suite.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    total = _int("tests")
    failures = _int("failures")
    errors = _int("errors")
    skipped = _int("skipped")

    groups: dict = {}
    failed_cases = []
    slowest = []
    for case in suite.iter("testcase"):
        classname = case.get("classname") or ""
        # "tests.test_drive_engine.TestFoo" -> "tests/test_drive_engine.py"
        module = classname.split(".")[1] if "." in classname else classname
        file_name = f"{module}.py" if module else "(unknown)"
        g = groups.setdefault(file_name, {"file": file_name, "passed": 0,
                                          "failed": 0, "skipped": 0,
                                          "duration": 0.0})
        try:
            duration = float(case.get("time") or 0)
        except ValueError:
            duration = 0.0
        g["duration"] += duration
        slowest.append({"name": f"{classname}::{case.get('name')}",
                        "duration": duration})

        failure = (case.find("failure") if case.find("failure") is not None
                   else case.find("error"))
        if failure is not None:
            g["failed"] += 1
            failed_cases.append({
                "name": f"{classname}::{case.get('name')}",
                "file": file_name,
                "message": (failure.get("message") or "")[:400],
                "detail": (failure.text or "")[-1200:],
            })
        elif case.find("skipped") is not None:
            g["skipped"] += 1
        else:
            g["passed"] += 1

    slowest.sort(key=lambda c: -c["duration"])
    return {
        "total": total,
        "passed": max(0, total - failures - errors - skipped),
        "failed": failures + errors,
        "skipped": skipped,
        "durationSec": float(suite.get("time") or 0),
        "ok": (failures + errors) == 0 and total > 0,
        # Most failures first, then largest: a green file is not what anyone
        # opened this page to read.
        "files": sorted(groups.values(),
                        key=lambda g: (-g["failed"], -g["passed"])),
        "failures": failed_cases[:50],
        "slowest": slowest[:15],
    }


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=HERE, capture_output=True, text=True,
                             timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:      # noqa: BLE001
        return ""


def run(python: str = "", timeout: int = 1800) -> dict:
    """Run the suite, write the report, return it.

    -p no:randomly because a report that reorders itself between runs cannot
    be compared with the previous one, which is most of what a report is for.
    """
    python = python or os.path.join(HERE, ".venv", "bin", "python")
    xml_path = os.path.join(HERE, "data", "junit.xml")
    os.makedirs(os.path.dirname(xml_path), exist_ok=True)
    started = time.time()
    proc = subprocess.run(
        [python, "-m", "pytest", "tests/", "-p", "no:randomly",
         f"--junitxml={xml_path}", "-q"],
        cwd=HERE, capture_output=True, text=True, timeout=timeout)

    if os.path.isfile(xml_path):
        with open(xml_path, encoding="utf-8") as fh:
            report = parse_junit(fh.read())
    else:
        # pytest died before writing anything -- a collection error, a bad
        # interpreter. Reported as a failure with its output rather than as
        # an absent report, which would render as "no tests" and read as fine.
        report = {"total": 0, "passed": 0, "failed": 1, "skipped": 0,
                  "durationSec": 0.0, "ok": False, "files": [], "slowest": [],
                  "failures": [{"name": "pytest did not run",
                                "file": "(none)",
                                "message": "no JUnit report was written",
                                "detail": (proc.stdout + proc.stderr)[-1200:]}]}

    report["ranAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    report["wallSec"] = round(time.time() - started, 1)
    report["commit"] = _git_commit()
    report["exitCode"] = proc.returncode

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    return report


def load() -> dict | None:
    """The last stored report, or None if the suite has never been run here."""
    try:
        with open(REPORT_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    r = run()
    print(f"{r['passed']}/{r['total']} passed, {r['failed']} failed "
          f"in {r['wallSec']}s (commit {r['commit'] or 'unknown'})")
    raise SystemExit(0 if r["ok"] else 1)
