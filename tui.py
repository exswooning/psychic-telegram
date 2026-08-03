"""
tui.py
======
Terminal dashboard for operating the migration engine.

Architectural choice worth understanding
----------------------------------------
The TUI is a **separate process that reads `migration.db`**. It is not coupled
to the migration runner at all. Because every piece of state already lives in
SQLite (WAL mode, so readers never block the writer), this gives you:

  * Run the migration under systemd / tmux / nohup on a jump box, and attach the
    dashboard from any SSH session — or three sessions at once.
  * Kill the dashboard without touching the migration.
  * Survive the dashboard crashing during a 40-hour run.

The connection sets `PRAGMA query_only=ON`, so the UI is structurally incapable
of corrupting the ledger that makes the migration idempotent.

Jobs launched from the UI are spawned as `main.py <subcommand>` subprocesses with
output tee'd to a job log. Stopping a job sends SIGINT, which `main.py` handles
cooperatively: in-flight items finish and commit, then it exits cleanly.

Dependencies: standard library only. `curses` ships with CPython on Linux and
macOS; on Windows install `windows-curses`.

Usage
-----
    python tui.py                      # attach to ./migration.db
    python tui.py --db /srv/migration.db
    python tui.py --refresh 1.0
"""

from __future__ import annotations

import argparse
import curses
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from config import Settings

# ======================================================================
# Formatting helpers (pure — unit-testable without a terminal)
# ======================================================================
def human_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:,.0f}{unit}" if unit == "B" else f"{n:,.1f}{unit}"
        n /= 1024.0
    return f"{n:,.1f}EB"


def human_count(n: int) -> str:
    return f"{n:,}"


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def bar(fraction: Optional[float], width: int) -> str:
    """Render a progress bar. `None` means 'expected total unknown'."""
    width = max(4, width)
    if fraction is None:
        return "?" * width
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * width))
    return "#" * filled + "." * (width - filled)


def truncate(text: str, width: int) -> str:
    text = str(text or "")
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "\u2026"


def tail_lines(path: str, n: int = 400, max_bytes: int = 512 * 1024) -> list[str]:
    """Read the last `n` lines without loading a multi-gigabyte log."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]  # drop the partial first line
        return lines[-n:]
    except OSError:
        return []


# ======================================================================
# Snapshot collection (pure — takes a connection, returns plain data)
# ======================================================================
@dataclass
class UserRow:
    source: str = ""
    target: str = ""
    status: str = "PENDING"
    drive_done: int = 0
    drive_failed: int = 0
    drive_skipped: int = 0
    mail_done: int = 0
    mail_failed: int = 0
    mail_skipped: int = 0
    cal_done: int = 0
    cal_failed: int = 0
    acl_failed: int = 0
    bytes_moved: int = 0
    exp_drive: int = 0
    exp_mail: int = 0
    gb_today: float = 0.0

    @property
    def done(self) -> int:
        return self.drive_done + self.mail_done + self.cal_done

    @property
    def failed(self) -> int:
        return self.drive_failed + self.mail_failed + self.cal_failed

    @property
    def expected(self) -> int:
        return self.exp_drive + self.exp_mail

    @property
    def fraction(self) -> Optional[float]:
        """None when discovery has not run, so the UI can say so honestly."""
        if self.expected <= 0:
            return None
        return min(1.0, self.done / self.expected)


@dataclass
class Snapshot:
    users: list[UserRow] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    collected_at: float = 0.0
    error: str = ""


def collect_snapshot(conn: sqlite3.Connection, cap_bytes: int,
                     max_failures: int = 500) -> Snapshot:
    """Single-pass read of every table the dashboard needs."""
    snap = Snapshot(collected_at=time.time())

    rows = conn.execute(
        "SELECT source_email, target_email, status FROM identity_map "
        "ORDER BY source_email"
    ).fetchall()
    # Keyed lowercase: identity_map normalises on write, but a hand-edited DB
    # or a CSV with mixed case should not silently produce a blank dashboard.
    users = {
        r["source_email"].lower(): UserRow(
            source=r["source_email"], target=r["target_email"], status=r["status"]
        )
        for r in rows
    }

    # Aggregate audit_log in one grouped query rather than N per-user queries.
    for r in conn.execute(
        """SELECT source_user, item_type, status, COUNT(*) n,
                  COALESCE(SUM(bytes_moved),0) b
           FROM audit_log GROUP BY source_user, item_type, status"""
    ):
        u = users.get((r["source_user"] or "").lower())
        if u is None:
            continue
        t, st, n = r["item_type"], r["status"], r["n"]
        u.bytes_moved += r["b"]
        ok = st == "SUCCESS"
        failed = st.startswith("FAILED")
        skipped = st.startswith("SKIPPED") or st.startswith("DEFERRED")

        if t in ("file", "folder"):
            u.drive_done += n if ok else 0
            u.drive_failed += n if failed else 0
            u.drive_skipped += n if skipped else 0
        elif t == "message":
            u.mail_done += n if ok else 0
            u.mail_failed += n if failed else 0
            u.mail_skipped += n if skipped else 0
        elif t == "event":
            u.cal_done += n if ok else 0
            u.cal_failed += n if failed else 0
        elif t == "acl":
            u.acl_failed += n if failed else 0

    # Expected totals from the most recent discovery row per user.
    for r in conn.execute(
        """SELECT d.* FROM discovery d
           JOIN (SELECT source_user, MAX(scanned_at) ts FROM discovery
                 GROUP BY source_user) m
             ON d.source_user=m.source_user AND d.scanned_at=m.ts"""
    ):
        u = users.get((r["source_user"] or "").lower())
        if u is None:
            continue
        u.exp_drive = (r["file_count"] or 0) + (r["folder_count"] or 0)
        try:
            u.exp_mail = r["messages_total"] or 0
        except (IndexError, KeyError):
            u.exp_mail = 0

    # Today's upload consumption, keyed by target address.
    ledger = {}
    for r in conn.execute(
        "SELECT target_user, bytes_sent FROM upload_ledger "
        "WHERE day_utc = strftime('%Y-%m-%d','now')"
    ):
        ledger[(r["target_user"] or "").lower()] = r["bytes_sent"]
    for u in users.values():
        u.gb_today = ledger.get(u.target.lower(), 0) / 1024**3

    snap.users = list(users.values())

    for r in conn.execute(
        """SELECT source_user, item_type, item_id, status, error_message, timestamp
           FROM audit_log
           WHERE status LIKE 'FAILED%'
           ORDER BY timestamp DESC LIMIT ?""",
        (max_failures,),
    ):
        snap.failures.append(dict(r))

    done = sum(u.done for u in snap.users)
    expected = sum(u.expected for u in snap.users)
    cap_gb_per_user = cap_bytes / 1024**3
    # The 750 GB ceiling is PER USER, so aggregate usage must be compared
    # against aggregate capacity. But a single user pinned at their cap is the
    # thing an operator needs to see, so we also surface the worst offender and
    # colour the header by that rather than by the (reassuring) average.
    worst = max((u.gb_today / cap_gb_per_user for u in snap.users), default=0.0)
    snap.totals = {
        "users": len(snap.users),
        "users_done": sum(1 for u in snap.users if u.status == "DONE"),
        "users_running": sum(1 for u in snap.users if u.status == "RUNNING"),
        "users_failed": sum(1 for u in snap.users if u.status == "FAILED"),
        "users_paused": sum(1 for u in snap.users if u.status == "PAUSED_QUOTA"),
        "items_done": done,
        "items_expected": expected,
        "items_failed": sum(u.failed for u in snap.users),
        "items_skipped": sum(u.drive_skipped + u.mail_skipped for u in snap.users),
        "bytes_moved": sum(u.bytes_moved for u in snap.users),
        "gb_today": sum(u.gb_today for u in snap.users),
        "cap_gb_per_user": cap_gb_per_user,
        "cap_gb_total": cap_gb_per_user * max(1, len(snap.users)),
        "worst_user_quota_frac": min(1.0, worst),
        "fraction": (done / expected) if expected > 0 else None,
    }
    return snap


# ======================================================================
# Job control
# ======================================================================
class JobRunner:
    """Spawns and supervises one `main.py` subprocess at a time."""

    def __init__(self, workdir: str, job_log: str):
        self.workdir = workdir
        self.job_log = job_log
        self.proc: Optional[subprocess.Popen] = None
        self.command: str = ""
        self.started_at: float = 0.0

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, argv: list[str]) -> tuple[bool, str]:
        if self.is_running():
            return False, "a job is already running"
        cmd = [sys.executable, "main.py", *argv]
        try:
            fh = open(self.job_log, "ab", buffering=0)
            fh.write(
                f"\n{'='*70}\n$ {' '.join(cmd)}\n{'='*70}\n".encode()
            )
            self.proc = subprocess.Popen(
                cmd, cwd=self.workdir, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group: SIGINT is targeted
            )
            self.command = " ".join(argv)
            self.started_at = time.time()
            return True, f"started: {self.command}"
        except OSError as exc:
            return False, f"launch failed: {exc}"

    def stop(self) -> str:
        """Graceful stop. main.py traps SIGINT and drains in-flight work."""
        if not self.is_running():
            return "no job running"
        assert self.proc is not None
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
            return "SIGINT sent — finishing in-flight items"
        except (OSError, AttributeError):
            self.proc.terminate()
            return "terminate sent"

    def status(self) -> str:
        if self.is_running():
            return f"RUNNING {self.command} ({fmt_duration(time.time()-self.started_at)})"
        if self.proc is not None:
            rc = self.proc.returncode
            return f"{'OK' if rc == 0 else f'EXIT {rc}'}: {self.command}"
        return "idle"


# ======================================================================
# The dashboard
# ======================================================================
DASHBOARD, USERS, FAILURES, SCOPE, LOGS = range(5)
SCREEN_NAMES = ["Dashboard", "Users", "Failures", "Scope", "Logs"]

# Colour pair ids
C_HEAD, C_OK, C_WARN, C_ERR, C_DIM, C_ACCENT, C_SEL = range(1, 8)


class Dashboard:
    def __init__(self, stdscr, settings: Settings, refresh: float):
        self.stdscr = stdscr
        self.settings = settings
        self.refresh = refresh
        self.screen = DASHBOARD
        self.scroll = 0
        self.selected = 0
        self.message = ""
        self.message_until = 0.0
        self.snapshot = Snapshot()
        self.started = time.time()
        self.last_collect = 0.0

        self.services = {"drive": True, "gmail": True, "calendar": True,
                         "chat": False}
        self.dry_run = False

        workdir = os.path.dirname(os.path.abspath(__file__))
        self.job_log = os.path.join(
            os.path.dirname(os.path.abspath(settings.log_file)) or ".",
            "job.log",
        )
        self.runner = JobRunner(workdir, self.job_log)

        self.conn = self._connect()
        self._init_colors()

    # -- plumbing --------------------------------------------------------
    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(self.settings.db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            # Structurally prevent the UI from ever writing to the ledger.
            conn.execute("PRAGMA query_only=ON;")
            conn.execute("PRAGMA busy_timeout=5000;")
            return conn
        except sqlite3.Error:
            return None

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(C_HEAD, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
        curses.init_pair(C_ERR, curses.COLOR_RED, bg)
        curses.init_pair(C_DIM, curses.COLOR_WHITE, bg)
        curses.init_pair(C_ACCENT, curses.COLOR_CYAN, bg)
        curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_WHITE)

    def _c(self, pair: int, bold: bool = False) -> int:
        attr = curses.color_pair(pair) if curses.has_colors() else 0
        return attr | (curses.A_BOLD if bold else 0)

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Bounds-safe addstr. curses raises if you touch the last cell."""
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = truncate(text, max(0, w - x - 1))
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _flash(self, msg: str, seconds: float = 4.0) -> None:
        self.message = msg
        self.message_until = time.time() + seconds

    def _status_attr(self, status: str) -> int:
        return {
            "DONE": self._c(C_OK, True),
            "RUNNING": self._c(C_ACCENT, True),
            "FAILED": self._c(C_ERR, True),
            "PAUSED_QUOTA": self._c(C_WARN, True),
            "INTERRUPTED": self._c(C_WARN),
        }.get(status, self._c(C_DIM))

    # -- data ------------------------------------------------------------
    def _collect(self, force: bool = False) -> None:
        if not force and time.time() - self.last_collect < self.refresh:
            return
        self.last_collect = time.time()
        if self.conn is None:
            self.conn = self._connect()
        if self.conn is None:
            self.snapshot = Snapshot(
                error=f"cannot open {self.settings.db_path} — run "
                      f"'python main.py init-db' first"
            )
            return
        try:
            self.snapshot = collect_snapshot(
                self.conn, self.settings.effective_upload_cap()
            )
        except sqlite3.Error as exc:
            self.snapshot = Snapshot(error=f"db read error: {exc}")

    # ==================================================================
    # Rendering
    # ==================================================================
    def draw(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if h < 12 or w < 62:
            self._put(0, 0, "Terminal too small — need at least 62x12",
                      self._c(C_ERR, True))
            self.stdscr.refresh()
            return

        self._draw_header(w)
        body_top, body_bottom = 6, h - 3
        if self.snapshot.error:
            self._put(body_top, 2, self.snapshot.error, self._c(C_ERR, True))
        elif self.screen == DASHBOARD:
            self._draw_dashboard(body_top, body_bottom, w)
        elif self.screen == USERS:
            self._draw_users(body_top, body_bottom, w)
        elif self.screen == FAILURES:
            self._draw_failures(body_top, body_bottom, w)
        elif self.screen == SCOPE:
            self._draw_scope(body_top, body_bottom, w)
        elif self.screen == LOGS:
            self._draw_logs(body_top, body_bottom, w)
        self._draw_footer(h, w)
        self.stdscr.refresh()

    def _draw_header(self, w: int) -> None:
        s, t = self.snapshot.totals, self.settings
        title = f" {t.source_domain}  ->  {t.target_domain} "
        mode = " DRY RUN " if self.dry_run else ""
        job = self.runner.status()
        self._put(0, 0, " " * w, self._c(C_HEAD, True))
        self._put(0, 1, title, self._c(C_HEAD, True))
        self._put(0, max(0, w - len(job) - len(mode) - 2), mode + job,
                  self._c(C_HEAD, True))

        frac = s.get("fraction")
        bw = max(10, w - 46)
        pct = "  n/a" if frac is None else f"{frac*100:5.1f}%"
        self._put(1, 1, "Overall", self._c(C_DIM, True))
        self._put(1, 10, f"[{bar(frac, bw)}]",
                  self._c(C_OK if (frac or 0) > 0 else C_DIM))
        self._put(1, 12 + bw, pct, self._c(C_ACCENT, True))

        exp = s.get("items_expected", 0)
        exp_str = human_count(exp) if exp else "unknown (run discover)"
        self._put(2, 1,
                  f"Items {human_count(s.get('items_done',0))} / {exp_str}"
                  f"   Skipped {human_count(s.get('items_skipped',0))}"
                  f"   Moved {human_bytes(s.get('bytes_moved',0))}",
                  self._c(C_DIM))
        fails = s.get("items_failed", 0)
        self._put(2, max(1, w - 22), f"Failures {human_count(fails)}",
                  self._c(C_ERR, True) if fails else self._c(C_OK))

        gb = s.get("gb_today", 0.0)
        cap_total = s.get("cap_gb_total", 1.0) or 1.0
        worst = s.get("worst_user_quota_frac", 0.0)
        agg_frac = min(1.0, gb / cap_total)
        # Colour by the worst individual user: one account at its cap stalls,
        # even when the fleet-wide average looks comfortable.
        qcolor = C_ERR if worst > 0.9 else (C_WARN if worst > 0.7 else C_OK)
        self._put(3, 1,
                  f"Users  {s.get('users_done',0)} done / "
                  f"{s.get('users_running',0)} running / "
                  f"{s.get('users_failed',0)} failed / "
                  f"{s.get('users_paused',0)} quota-paused "
                  f"of {s.get('users',0)}", self._c(C_DIM))
        self._put(3, max(1, w - 42),
                  f"24h {gb:,.0f}/{cap_total:,.0f}GB [{bar(agg_frac, 8)}] "
                  f"peak {worst*100:3.0f}%",
                  self._c(qcolor))

        svc = " ".join(
            f"{k[0].upper()}{'+' if v else '-'}" for k, v in self.services.items()
        )
        self._put(4, 1, f"Services {svc}   Uptime {fmt_duration(time.time()-self.started)}",
                  self._c(C_DIM))

        tabs = "  ".join(
            f"[{i+1}]{n}" for i, n in enumerate(SCREEN_NAMES)
        )
        self._put(5, 1, tabs, self._c(C_DIM))
        # Highlight the active tab.
        x = 1
        for i, n in enumerate(SCREEN_NAMES):
            label = f"[{i+1}]{n}"
            if i == self.screen:
                self._put(5, x, label, self._c(C_ACCENT, True))
            x += len(label) + 2

    def _draw_dashboard(self, top: int, bottom: int, w: int) -> None:
        y = top
        self._put(y, 1, "SERVICE PROGRESS", self._c(C_ACCENT, True)); y += 1
        us = self.snapshot.users
        agg = [
            ("Drive  ", sum(u.drive_done for u in us), sum(u.drive_failed for u in us),
             sum(u.drive_skipped for u in us), sum(u.exp_drive for u in us)),
            ("Gmail  ", sum(u.mail_done for u in us), sum(u.mail_failed for u in us),
             sum(u.mail_skipped for u in us), sum(u.exp_mail for u in us)),
            ("Calendar", sum(u.cal_done for u in us), sum(u.cal_failed for u in us),
             0, 0),
        ]
        bw = max(10, w - 60)
        for name, done, failed, skipped, exp in agg:
            self._put(y, 2, f"{name:<9}", self._c(C_DIM, True))
            if exp:
                self._put(y, 12, f"[{bar(done / exp, bw)}]", self._c(C_OK))
            else:
                # Be explicit rather than showing a meaningless bar. Discovery
                # sizes Drive and Gmail; it does not pre-count calendar events.
                self._put(y, 12, "[" + "no baseline".center(bw, " ") + "]",
                          self._c(C_DIM))
            self._put(y, 14 + bw,
                      f"{human_count(done):>9} ok  {human_count(skipped):>7} skip  "
                      f"{human_count(failed):>6} fail",
                      self._c(C_ERR) if failed else self._c(C_DIM))
            y += 1

        y += 1
        self._put(y, 1, "ACTIVE USERS", self._c(C_ACCENT, True)); y += 1
        active = [u for u in us if u.status in ("RUNNING", "PAUSED_QUOTA")]
        active.sort(key=lambda u: -u.done)
        if not active:
            self._put(y, 2, "none — press m to start a migration",
                      self._c(C_DIM)); y += 1
        for u in active[: max(0, bottom - y - 6)]:
            frac = u.fraction
            self._put(y, 2, truncate(u.source, 30), self._c(C_DIM))
            self._put(y, 34, f"[{bar(frac, max(8, w - 78))}]", self._c(C_OK))
            self._put(y, 36 + max(8, w - 78),
                      f"{human_count(u.done):>8}  {human_bytes(u.bytes_moved):>9}  "
                      f"{u.status}", self._status_attr(u.status))
            y += 1

        y += 1
        self._put(y, 1, "RECENT FAILURES", self._c(C_ACCENT, True)); y += 1
        for f in self.snapshot.failures[: max(0, bottom - y)]:
            self._put(y, 2,
                      f"{f['timestamp'][11:19]}  {f['item_type']:<8} "
                      f"{truncate(f['source_user'], 22):<22} "
                      f"{truncate(f['error_message'] or '', max(10, w - 60))}",
                      self._c(C_ERR))
            y += 1
        if not self.snapshot.failures:
            self._put(y, 2, "none", self._c(C_OK))

    def _draw_users(self, top: int, bottom: int, w: int) -> None:
        rows = self.snapshot.users
        hdr = (f"{'SOURCE':<28}{'STATUS':<14}{'DRIVE':>10}{'MAIL':>10}"
               f"{'CAL':>7}{'FAIL':>7}{'MOVED':>11}  PROGRESS")
        self._put(top, 1, hdr, self._c(C_HEAD, True))
        visible = bottom - top - 1
        self.selected = max(0, min(self.selected, max(0, len(rows) - 1)))
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + visible:
            self.scroll = self.selected - visible + 1

        for i, u in enumerate(rows[self.scroll: self.scroll + visible]):
            y = top + 1 + i
            idx = self.scroll + i
            sel = idx == self.selected
            base = self._c(C_SEL) if sel else 0
            self._put(y, 1, " " * (w - 2), base)
            self._put(y, 1, truncate(u.source, 27), base or self._c(C_DIM))
            self._put(y, 29, truncate(u.status, 13),
                      base or self._status_attr(u.status))
            self._put(y, 43, f"{human_count(u.drive_done):>9}", base)
            self._put(y, 53, f"{human_count(u.mail_done):>9}", base)
            self._put(y, 63, f"{human_count(u.cal_done):>6}", base)
            self._put(y, 70, f"{human_count(u.failed):>6}",
                      base or (self._c(C_ERR) if u.failed else self._c(C_DIM)))
            self._put(y, 77, f"{human_bytes(u.bytes_moved):>10}", base)
            self._put(y, 89, f"[{bar(u.fraction, max(6, w - 92))}]",
                      base or self._c(C_OK))

        if rows:
            u = rows[self.selected]
            self._put(bottom, 1,
                      f"-> {u.source} -> {u.target}   "
                      f"drive ok/skip/fail {u.drive_done}/{u.drive_skipped}/"
                      f"{u.drive_failed}   mail {u.mail_done}/{u.mail_skipped}/"
                      f"{u.mail_failed}   acl fail {u.acl_failed}   "
                      f"today {u.gb_today:.1f}GB",
                      self._c(C_ACCENT))

    def _draw_failures(self, top: int, bottom: int, w: int) -> None:
        rows = self.snapshot.failures
        self._put(top, 1,
                  f"{'TIME':<20}{'TYPE':<10}{'USER':<26}{'ITEM':<24}ERROR",
                  self._c(C_HEAD, True))
        visible = bottom - top
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - visible)))
        if not rows:
            self._put(top + 1, 2, "No failures recorded.", self._c(C_OK, True))
            return
        for i, f in enumerate(rows[self.scroll: self.scroll + visible]):
            y = top + 1 + i
            self._put(y, 1, truncate(f["timestamp"], 19), self._c(C_DIM))
            self._put(y, 21, truncate(f["item_type"], 9), self._c(C_DIM))
            self._put(y, 31, truncate(f["source_user"], 25), self._c(C_DIM))
            self._put(y, 57, truncate(f["item_id"], 23), self._c(C_DIM))
            self._put(y, 81, truncate(f["error_message"] or "", max(10, w - 83)),
                      self._c(C_ERR))
        self._put(bottom, 1,
                  f"{len(rows)} failure(s) — showing {self.scroll+1}-"
                  f"{min(len(rows), self.scroll+visible)}", self._c(C_DIM))

    def _draw_scope(self, top: int, bottom: int, w: int) -> None:
        lines = self._scope_lines(w)
        visible = bottom - top
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - visible)))
        for i, line in enumerate(lines[self.scroll: self.scroll + visible]):
            y = top + i
            attr = self._c(C_DIM)
            if line.startswith("=="):
                attr = self._c(C_ACCENT, True)
            elif " [+] " in line or line.strip().startswith("[+]"):
                attr = self._c(C_OK)
            elif line.strip().startswith("[~]"):
                attr = self._c(C_WARN)
            elif line.strip().startswith("[-]"):
                attr = self._c(C_ERR)
            self._put(y, 1, line, attr)

    def _scope_lines(self, w: int) -> list[str]:
        if getattr(self, "_scope_cache", None) and self._scope_w == w:
            return self._scope_cache
        import scope as scope_mod

        tally = scope_mod.counts()
        head = [
            "MIGRATION SCOPE — what this engine moves",
            "",
            "  [+] FULL     high fidelity",
            "  [~] PARTIAL  migrated with a named fidelity loss",
            "  [-] NONE     not migrated by this engine",
            "",
        ]
        for svc in scope_mod.SERVICES:
            t = tally.get(svc, {})
            head.append(
                f"  {svc:<10} full {t.get('FULL',0):>2}   "
                f"partial {t.get('PARTIAL',0):>2}   none {t.get('NONE',0):>2}"
            )
        vol = {}
        if self.conn is not None:
            try:
                class _Shim:  # planned_volume expects an object with .conn
                    pass
                shim = _Shim()
                shim.conn = self.conn
                vol = scope_mod.planned_volume(shim)
            except Exception:  # noqa: BLE001
                vol = {}
        if vol.get("users"):
            head += [
                "",
                f"  Discovered volume: {human_count(vol['files'])} files, "
                f"{human_count(vol['folders'])} folders, "
                f"{human_count(vol['native'])} native docs, "
                f"{human_bytes(vol['bytes'])}, "
                f"{human_count(vol['messages'])} messages, "
                f"max depth {vol['max_depth']}",
            ]
        else:
            head += ["", "  Discovered volume: run 'discover' (key d) to populate."]

        self._scope_cache = head + scope_mod.as_text(width=max(60, w - 4))
        self._scope_w = w
        return self._scope_cache

    def _draw_logs(self, top: int, bottom: int, w: int) -> None:
        path = (self.job_log if os.path.exists(self.job_log)
                else self.settings.log_file)
        lines = tail_lines(path, n=1000)
        visible = bottom - top
        # Negative scroll offset from the tail: 0 == follow the end.
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - visible)))
        start = max(0, len(lines) - visible - self.scroll)
        for i, line in enumerate(lines[start: start + visible]):
            attr = self._c(C_DIM)
            if " ERROR " in line or "Traceback" in line:
                attr = self._c(C_ERR)
            elif " WARNING " in line:
                attr = self._c(C_WARN)
            self._put(top + i, 1, line, attr)
        self._put(bottom, 1,
                  f"tail: {path}   {'FOLLOWING' if self.scroll == 0 else 'PAUSED'}"
                  f"   (arrows scroll, End resumes follow)", self._c(C_ACCENT))

    def _draw_footer(self, h: int, w: int) -> None:
        if self.message and time.time() < self.message_until:
            self._put(h - 2, 1, truncate(self.message, w - 2),
                      self._c(C_WARN, True))
        keys = ("q quit  1-5 view  d discover  p preflight  m migrate  "
                "x delta  k stop  t dry-run  s/g/c/h services  e export  r refresh")
        self._put(h - 1, 0, " " * (w - 1), self._c(C_HEAD))
        self._put(h - 1, 1, keys, self._c(C_HEAD))

    # ==================================================================
    # Input
    # ==================================================================
    def _confirm(self, prompt: str) -> bool:
        h, w = self.stdscr.getmaxyx()
        self._put(h - 2, 1, " " * (w - 2))
        self._put(h - 2, 1, f"{prompt} [y/N] ", self._c(C_WARN, True))
        self.stdscr.refresh()
        self.stdscr.nodelay(False)
        try:
            ch = self.stdscr.getch()
        finally:
            self.stdscr.nodelay(True)
        return ch in (ord("y"), ord("Y"))

    def _service_args(self) -> list[str]:
        chosen = [k for k, v in self.services.items() if v]
        return ["--services", ",".join(chosen)] if chosen else []

    def _launch(self, argv: list[str], confirm_msg: Optional[str] = None) -> None:
        if self.runner.is_running():
            self._flash("a job is already running — press k to stop it first")
            return
        if confirm_msg and not self._confirm(confirm_msg):
            self._flash("cancelled")
            return
        if self.dry_run:
            argv = ["--dry-run", *argv]
        ok, msg = self.runner.start(argv)
        self._flash(msg, 6.0)

    def handle_key(self, ch: int) -> bool:
        """Returns False to quit."""
        if ch in (ord("q"), ord("Q")):
            if self.runner.is_running():
                if not self._confirm("A job is running. Quit the UI anyway? "
                                     "(the job keeps running)"):
                    return True
            return False

        if ord("1") <= ch <= ord("5"):
            self.screen = ch - ord("1")
            self.scroll = 0
            return True

        if ch == curses.KEY_DOWN:
            if self.screen == USERS:
                self.selected += 1
            else:
                self.scroll += 1
        elif ch == curses.KEY_UP:
            if self.screen == USERS:
                self.selected = max(0, self.selected - 1)
            else:
                self.scroll = max(0, self.scroll - 1)
        elif ch == curses.KEY_NPAGE:
            h, _ = self.stdscr.getmaxyx()
            step = max(1, h - 10)
            if self.screen == USERS:
                self.selected += step
            else:
                self.scroll += step
        elif ch == curses.KEY_PPAGE:
            h, _ = self.stdscr.getmaxyx()
            step = max(1, h - 10)
            if self.screen == USERS:
                self.selected = max(0, self.selected - step)
            else:
                self.scroll = max(0, self.scroll - step)
        elif ch == curses.KEY_HOME:
            self.scroll = 0
            self.selected = 0
        elif ch == curses.KEY_END:
            self.scroll = 0  # in Logs, 0 == follow the tail

        elif ch == ord("r"):
            self._collect(force=True)
            self._flash("refreshed", 1.5)
        elif ch == ord("t"):
            self.dry_run = not self.dry_run
            self._flash(f"dry-run {'ON' if self.dry_run else 'OFF'} for next launch")
        elif ch == ord("s"):
            self.services["drive"] = not self.services["drive"]
        elif ch == ord("g"):
            self.services["gmail"] = not self.services["gmail"]
        elif ch == ord("c"):
            self.services["calendar"] = not self.services["calendar"]
        elif ch == ord("h"):
            self.services["chat"] = not self.services["chat"]

        elif ch == ord("p"):
            self._launch(["preflight"])
        elif ch == ord("d"):
            self._launch(["discover", "--include-mail"])
        elif ch == ord("m"):
            self._launch(
                ["migrate", *self._service_args()],
                f"Start FULL migration to {self.settings.target_domain} "
                f"({', '.join(k for k,v in self.services.items() if v)})?",
            )
        elif ch == ord("x"):
            self._launch(["delta", *self._service_args()],
                         "Start delta pass?")
        elif ch == ord("k"):
            self._flash(self.runner.stop(), 6.0)
        elif ch == ord("e"):
            import scope as scope_mod
            out = os.path.join(os.path.dirname(
                os.path.abspath(self.settings.db_path)) or ".", "SCOPE.md")
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(scope_mod.as_markdown())
                self._flash(f"scope exported to {out}", 6.0)
            except OSError as exc:
                self._flash(f"export failed: {exc}", 6.0)

        return True

    # ==================================================================
    def loop(self) -> None:
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        while True:
            self._collect()
            self.draw()
            ch = self.stdscr.getch()
            if ch == -1:
                time.sleep(0.05)
                continue
            if ch == curses.KEY_RESIZE:
                self._scope_cache = None
                continue
            if not self.handle_key(ch):
                break


def run(stdscr, settings: Settings, refresh: float) -> None:
    Dashboard(stdscr, settings, refresh).loop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tui", description="Operator dashboard for the migration engine."
    )
    ap.add_argument("--db", help="path to migration.db")
    ap.add_argument("--refresh", type=float, default=2.0,
                    help="seconds between DB polls (default 2.0)")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.db:
        settings.db_path = args.db

    if not os.path.exists(settings.db_path):
        print(f"No database at {settings.db_path}.\n"
              f"Run:  python main.py init-db --identities identities.csv")
        return 1

    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(run, settings, args.refresh)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
