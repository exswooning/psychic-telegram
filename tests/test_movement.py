"""How a migration is moving, for the Jobs detail: the pass, each service's method as the
running process has it (a government source always reads download_upload), and counts."""
import api_server


def test_movement_reads_the_pass_the_methods_and_the_counts(db, monkeypatch):
    monkeypatch.setenv("SOURCE_DOMAIN", "dharan.gov.np")
    monkeypatch.setenv("TRANSFER_MODE", "server_side")
    monkeypatch.setattr(api_server.job_admission, "list_active", lambda: [])
    monkeypatch.setattr(api_server, "_job_log_lines", lambda *a, **k: [
        "=== run started old", "PASS 3/3 pid=1: calendar", "=== run started 2026-10-19T13:29:41Z: migrate ===",
        "PASS 1/3 pid=9: drive", "PASS 2/3 pid=9: gmail"])
    with db.write() as c:
        c.execute("INSERT INTO discovery(source_user, scanned_at, file_count, messages_total) VALUES ('a', '1', 10, 100)")
        c.execute("INSERT INTO discovery(source_user, scanned_at, file_count, messages_total) VALUES ('a', '0', 99, 999)")
        for i in range(4):
            c.execute("INSERT INTO id_mapping(source_user, source_id, target_id, type) VALUES ('a', ?, ?, 'file')", (f"f{i}", f"t{i}"))
        c.execute("INSERT INTO id_mapping(source_user, source_id, target_id, type) VALUES ('a', 'm1', 'n1', 'message')")
    mv = api_server._movement(db.conn, None)
    assert mv["stage"] == {"pass": 2, "of": 3, "services": "gmail"}
    assert mv["drive"]["method"] == "download_upload" and "source is only read" in mv["drive"]["how"]
    assert (mv["drive"]["filesCopied"], mv["drive"]["filesOnSource"]) == (4, 10)        # latest scan only
    assert (mv["mail"]["method"], mv["mail"]["messagesCopied"], mv["mail"]["messagesOnSource"]) == ("engine", 1, 100)
