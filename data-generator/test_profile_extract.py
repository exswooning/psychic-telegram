"""profile_extract writes numbers and file types only: no address, subject, file name, label name or id."""
import json
import sqlite3

import profile_extract as P

SECRETS = ("ramesh.shrestha@org.example", "Budget for Ward 4", "salary-list.xlsx", "Finance-secret-label", "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
           "private-subject")


def _ledger(path):
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE identity_map (entity_type TEXT, source_email TEXT);
      CREATE TABLE discovery (source_user TEXT, scanned_at TEXT, file_count INT, folder_count INT, native_count INT, shortcut_count INT,
        max_depth INT, total_bytes INT, largest_bytes INT, oversized_native INT, messages_total INT, threads_total INT,
        user_label_count INT, est_days REAL, mime_histogram TEXT);
      CREATE TABLE id_mapping (source_user TEXT, source_id TEXT, target_id TEXT, type TEXT, parent_target_id TEXT, source_name TEXT, created_at TEXT);
      CREATE TABLE mail_link_messages (source_user TEXT, message_id TEXT, file_ids TEXT, by_search INT, checked_at TEXT);
      CREATE TABLE label_map (source_user TEXT, source_label_id TEXT, target_label_id TEXT, label_name TEXT);
    """)
    u = SECRETS[0]
    c.execute("INSERT INTO identity_map VALUES ('user', ?)", (u,))
    c.execute("INSERT INTO discovery VALUES (?, '2026-01-02', 300, 12, 20, 1, 4, 5000000, 900000, 0, 900, 700, 2, 1.0, ?)",
              (u, json.dumps({"application/pdf": 100, "image/jpeg": 200})))
    c.execute("INSERT INTO discovery VALUES (?, '2026-01-01', 1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1.0, '{}')", (u,))
    c.execute("INSERT INTO id_mapping VALUES (?, ?, 't', 'task', '', ?, '')", (u, SECRETS[4], SECRETS[2]))
    c.execute("INSERT INTO mail_link_messages VALUES (?, ?, ?, 1, '')", (u, SECRETS[5], SECRETS[4] + "," + "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"))
    c.execute("INSERT INTO label_map VALUES (?, 'L1', 'L2', ?)", (u, SECRETS[3]))
    c.commit()
    c.close()


def test_the_profile_holds_the_numbers_and_none_of_the_names(tmp_path):
    db = tmp_path / "l.db"
    _ledger(str(db))
    out = tmp_path / "p.json"
    assert P.main(["--db", str(db), "--out", str(out)]) == 0
    text = out.read_text()
    for secret in SECRETS:
        assert secret not in text, secret
    assert "@" not in text
    prof = json.loads(text)
    row = prof["per_user"][0]
    assert prof["users"] == 1 and row["file_count"] == 300 and row["messages_total"] == 900     # the later scan wins
    assert row["task"] == 1 and row["link_mail"] == 1 and row["labels_mapped"] == 1
    assert prof["mime_totals"] == {"image/jpeg": 200, "application/pdf": 100}
    assert prof["link_mail"]["files_per_message"]["max"] == 2
