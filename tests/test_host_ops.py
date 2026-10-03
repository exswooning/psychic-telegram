"""The SSH-only questions, answered by the dashboard: a job's process, the services,
what a restart would kill. /proc is faked -- this Mac has none."""
import sqlite3

import host_ops


def _proc(root, pid, ppid, cpu, start, rss_kb, threads, cmd=b"python main.py migrate"):
    d = root / str(pid)
    d.mkdir()
    fields = ["S", str(ppid)] + ["0"] * 9 + [str(cpu), "0"] + ["0"] * 6 + [str(start)]
    (d / "stat").write_text(f"{pid} (python (x)) " + " ".join(fields) + "\n")
    (d / "status").write_text(f"Name:\tpython\nVmRSS:\t{rss_kb} kB\nThreads:\t{threads}\n")
    (d / "cmdline").write_bytes(cmd.replace(b" ", b"\0"))


def test_a_job_and_its_shards_are_measured_together(tmp_path, monkeypatch):
    _proc(tmp_path, 100, 1, cpu=500, start=1000, rss_kb=204800, threads=40)
    _proc(tmp_path, 101, 100, cpu=300, start=1100, rss_kb=102400, threads=20)
    _proc(tmp_path, 200, 1, cpu=1, start=5, rss_kb=1024, threads=1)        # not ours
    (tmp_path / "uptime").write_text("2000.0 100.0\n")
    monkeypatch.setattr(host_ops, "PROC", str(tmp_path))
    monkeypatch.setattr(host_ops.time, "sleep", lambda s: None)
    s = host_ops.process_stats(100)
    hz = host_ops.os.sysconf("SC_CLK_TCK")
    assert s["processes"] == 2 and s["rss_mb"] == 300.0 and s["threads"] == 60
    assert s["elapsed_s"] == int(2000 - 1000 / hz) and s["cmd"] == "python main.py migrate"
    assert host_ops.process_stats(999) is None


def test_restart_refuses_while_anything_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(host_ops, "_run", lambda argv, timeout=15: ran.append(argv) or "")
    assert host_ops.restart("bitport-api", ["migrate (pid 7)"])[0] is False
    assert host_ops.restart("sshd", [])[0] is False
    assert ran == []
    ok, _ = host_ops.restart("bitport-api", [])
    assert ok and ran == [["systemctl", "restart", "--no-block", "bitport-api"]]


def test_services_reads_systemd_and_skips_a_unit_that_is_not_installed(monkeypatch, tmp_path):
    def fake(argv, timeout=15):
        if argv[:2] == ["systemctl", "show"]:
            return ("LoadState=not-found\n" if argv[2] == "caddy" else
                    "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=2\n")
        if "-k" in argv:
            return "2026-10-03T01:00:00 box kernel: Out of memory: Killed process 42 (python)\n"
        return "-- No entries --\n2026-10-03T01:00:00 box python[1]: WARNING slow\n"
    monkeypatch.setattr(host_ops, "_run", fake)
    monkeypatch.setattr(host_ops, "HERE", str(tmp_path))
    (tmp_path / "DEPLOYED_COMMIT").write_text("abc1234\n")
    r = host_ops.services()
    assert [u["unit"] for u in r["units"]] == ["bitport-api", "bitport-webui", "bitport-fleet"]
    assert r["units"][0]["restarts"] == 2 and r["units"][0]["recent"] == [
        "2026-10-03T01:00:00 box python[1]: WARNING slow"]
    assert len(r["oom"]) == 1 and r["deployed_commit"] == "abc1234"


def test_an_unfinished_repair_counts_as_busy(tmp_path):
    db = tmp_path / "data" / "accounts" / "3"
    db.mkdir(parents=True)
    c = sqlite3.connect(db / "migration.db")
    c.execute("CREATE TABLE repair_runs (id INTEGER, started_at TEXT, finished_at TEXT)")
    c.execute("INSERT INTO repair_runs VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ','now'), NULL)")
    c.execute("INSERT INTO repair_runs VALUES (2, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'x')")
    c.commit(); c.close()
    busy = host_ops.unfinished_repairs(str(tmp_path))
    assert len(busy) == 1 and busy[0].startswith("repair 1 ")


def test_a_log_is_searched_whole_with_context():
    import webui
    lines = [f"line {i}\n" for i in range(1000)]
    lines[3] = "PASS 1/3 pid=42: drive\n"
    r = webui._search_log(lines, "pass 1/3")
    assert r["total"] == 1 and r["matches"][0]["line"] == 4
    assert r["matches"][0]["before"] == ["line 1\n", "line 2\n"]
    assert r["matches"][0]["after"] == ["line 4\n", "line 5\n"]
