"""The link-email inventory starts beside the Drive pass (split mode), reads each mailbox, and stops on Stop."""
import threading
import time

import main
import mail_dead_links
from tests.conftest import SRC_USER, TGT_USER


def test_the_inventory_runs_every_mailbox_on_its_own_thread(db, settings, monkeypatch):
    done = []
    monkeypatch.setattr(mail_dead_links, "inventory_user", lambda gm, workers, say: done.append(gm.source_user) or {})
    db.record_mapping(SRC_USER, "f", "t", "file")
    from db import bulk_seed_identities
    bulk_seed_identities(db, [(SRC_USER, TGT_USER)])
    t = main._start_link_inventory(object(), db, settings, None)
    assert isinstance(t, threading.Thread) and t.daemon
    t.join(10)
    assert done == [SRC_USER]


def test_a_stop_ends_the_inventory_between_mailboxes(db, settings, monkeypatch):
    from db import bulk_seed_identities
    bulk_seed_identities(db, [(SRC_USER, TGT_USER), ("b@s.x", "b@t.x")])
    seen = []
    monkeypatch.setattr(mail_dead_links, "inventory_user", lambda gm, workers, say: seen.append(gm.source_user) or main.SHUTDOWN.set() or {})
    main.SHUTDOWN.clear()
    try:
        main._start_link_inventory(object(), db, settings, None).join(10)
        assert seen == [SRC_USER]
    finally:
        main.SHUTDOWN.clear()
