from __future__ import annotations

import pytest


def test_split_preflight_spools_classifications_and_rereads_refs(gmail_migrator, db, monkeypatch):
    count = 1_200
    list_calls = []

    def refs(_query):
        list_calls.append(True)
        yield from ({"id": f"message-{i}"} for i in range(count))

    monkeypatch.setattr(gmail_migrator, "_iter_messages", refs)
    monkeypatch.setattr(
        gmail_migrator,
        "_split_classify",
        lambda _mid: ({"labelIds": []}, {
            "route": "DMS", "changed": {}, "unresolved": [], "linkIds": [], "why": "test"
        }),
    )
    monkeypatch.setattr(gmail_migrator, "_save_link_scan", lambda: None)

    result = gmail_migrator._scan_split_before_delivery("")

    assert len(list_calls) == 1
    assert len(getattr(gmail_migrator, "_split_scan", {})) == 0
    table_rows = db.conn.execute(
        "SELECT COUNT(*) FROM gmail_split_scan WHERE source_user=?",
        (gmail_migrator.source_user,),
    ).fetchone()[0]
    assert table_rows == count
    assert len(list(result)) == count
    assert len(list_calls) == 2


def test_split_preflight_does_not_materialize_a_completed_link_inventory(
        gmail_migrator, db, monkeypatch):
    import mail_dead_links

    count = 20
    with db.write() as conn:
        conn.execute(mail_dead_links.SEEN_SCHEMA)
        conn.execute(mail_dead_links.INVENTORIES_SCHEMA)
        conn.execute("INSERT INTO mail_link_inventories VALUES (?,?,?)",
                     (gmail_migrator.source_user, "2026-11-27T00:00:00Z", count))
        conn.executemany("INSERT INTO mail_link_seen VALUES (?,?,?)",
                         [(gmail_migrator.source_user, f"message-{i}", mail_dead_links.READ)
                          for i in range(count)])
    monkeypatch.setattr(mail_dead_links, "proven", lambda *_: pytest.fail("inventory materialized"))
    monkeypatch.setattr(gmail_migrator, "_iter_messages",
                        lambda _query: ( {"id": f"message-{i}"} for i in range(count)))
    monkeypatch.setattr(gmail_migrator, "_split_classify",
                        lambda _mid: ({"labelIds": []}, {"route": "DMS", "changed": {},
                                       "unresolved": [], "linkIds": [], "why": "test"}))
    monkeypatch.setattr(gmail_migrator, "_save_link_scan", lambda: None)

    refs = gmail_migrator._scan_split_before_delivery("")

    assert len(list(refs)) == count


def test_search_mode_spools_candidate_ids_instead_of_building_mailbox_sets(
        gmail_migrator, db, monkeypatch):
    import gmail_engine

    count = 1_200
    gmail_migrator.settings.mail_link_scan = "search"
    monkeypatch.setattr(gmail_engine, "LINK_SEARCHES", ("has:drive",))
    monkeypatch.setattr(gmail_migrator, "_iter_search_ids",
                        lambda _query: (f"message-{i}" for i in range(count)), raising=False)
    monkeypatch.setattr(gmail_migrator, "_list_ids",
                        lambda *_: pytest.fail("search mode materialized a candidate set"))
    monkeypatch.setattr(gmail_migrator, "_iter_messages",
                        lambda _query: ({"id": f"message-{i}"} for i in range(count)))
    monkeypatch.setattr(gmail_migrator, "_split_classify",
                        lambda _mid: ({"labelIds": []}, {"route": "DMS", "changed": {},
                                       "unresolved": [], "linkIds": [], "why": "test"}))
    monkeypatch.setattr(gmail_migrator, "_save_link_scan", lambda: None)

    refs = gmail_migrator._scan_split_before_delivery("")

    candidates = db.conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM gmail_split_candidates "
        "WHERE source_user=? AND scan_id=? AND category LIKE 'term:%'",
        (gmail_migrator.source_user, gmail_migrator._split_scan_id),
    ).fetchone()[0]
    assert candidates == count
    assert len(list(refs)) == count
