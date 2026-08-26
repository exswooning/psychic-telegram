"""A sidebar page must be about the migration on screen.

Walked live, signed in as a superadmin whose own tenant is empty while
account 7 was running 201 users and 158,204 items:

    Mission Control -> "11 users tracked - overall 28%"
    Final Report    -> "11 of 11 users migrated successfully"
    Failures        -> rows from a different tenant, dated three weeks back

Every one of those readers fell through to the shared control-plane
ledger. ro() has always accepted an explicit path; nothing passed one.

The Final Report is the worst of them: it does not merely show the wrong
number, it declares a migration complete.
"""
import inspect

import control_plane_db as cpdb


class TestTheReadersCanTargetOneAccount:
    def test_user_progress_takes_a_ledger(self):
        assert "db_path" in inspect.signature(cpdb.user_progress).parameters

    def test_failure_feed_takes_a_ledger(self):
        assert "db_path" in inspect.signature(cpdb.failure_feed).parameters

    def test_forensic_detail_takes_a_ledger(self):
        assert "db_path" in inspect.signature(cpdb.forensic_detail).parameters

    def test_they_read_the_ledger_they_are_given(self, tmp_path):
        from db import MigrationDB
        path = str(tmp_path / "acct.db")
        d = MigrationDB(path)
        d.conn.execute("INSERT INTO identity_map(source_email,target_email,"
                       "status) VALUES('only@here','only@there','DONE')")
        d.conn.commit()
        d.close()
        rows = cpdb.user_progress(path)
        assert [r["source_email"] for r in rows] == ["only@here"]

    def test_a_failure_in_one_ledger_is_not_reported_from_another(self,
                                                                  tmp_path):
        from db import MigrationDB
        mine = str(tmp_path / "mine.db")
        theirs = str(tmp_path / "theirs.db")
        for path, user in ((mine, "me@src"), (theirs, "them@src")):
            d = MigrationDB(path)
            d.log_audit(user, "i1", "file", "FAILED", "boom")
            d.close()
        assert [r["source_user"] for r in cpdb.failure_feed(db_path=mine)] \
            == ["me@src"]


class TestTheEndpointsPassOne:
    def test_no_shared_reader_is_called_unscoped(self):
        """The property is that a ledger is named, not how it was chosen.

        A request page resolves it with _account_in_context; the tailer
        already has the account it is building for and passes it directly.
        Either is fine -- falling through to the shared control-plane
        database is not.
        """
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        for reader in ("cpdb.user_progress", "cpdb.failure_feed",
                       "cpdb.forensic_detail"):
            start = 0
            while True:
                idx = src.find(reader, start)
                if idx == -1:
                    break
                start = idx + len(reader)
                window = src[max(0, idx - 260):idx + 340]
                assert ("_ledger_for" in window
                        or "_account_db_path" in window
                        or "_ws_ledger" in window), (
                    f"{reader} at offset {idx} falls through to the shared "
                    f"ledger: {window[-160:]!r}")

    def test_the_websocket_snapshot_is_scoped_too(self):
        # It pushes user progress on connect, so an unscoped snapshot shows
        # the wrong tenant before any page has rendered.
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        snap = src.split('async def ws_endpoint')[1][:1500]
        # The socket resolves its own tenant's ledger before joining, and
        # sends [] rather than another tenant's users when there is none.
        assert "_ledger_for(op)" in snap
        assert "cpdb.user_progress, _ws_ledger" in snap

    def test_metrics_uses_the_same_helper_rather_than_its_own_copy(self):
        # It had a private copy that read the wrong key for months.
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        assert src.count("def _account_in_context") == 1
        assert 'j.get("name") in _OWNED_JOB_NAMES' not in src


class TestTheWebsocketDoesNotFanOutOneTenantToEveryone:
    """The hub was a bare set of sockets.

    The tailer built one per-user progress payload and broadcast it to
    every connected browser, so a tenant watching their own idle migration
    received somebody else's users. That is a leak, not a wrong number.
    """

    def _hub(self):
        import api_server
        return api_server.Hub()

    def test_a_frame_for_one_tenant_reaches_only_that_tenant(self):
        import asyncio

        class _Sock:
            def __init__(self):
                self.sent = []

            async def accept(self):
                pass

            async def send_text(self, payload):
                self.sent.append(payload)

        async def _run():
            hub = self._hub()
            mine, theirs = _Sock(), _Sock()
            await hub.join(mine, 7)
            await hub.join(theirs, 66)
            await hub.broadcast({"type": "JOB_PROGRESS", "users": ["a"]}, 7)
            return mine.sent, theirs.sent

        mine, theirs = asyncio.run(_run())
        assert len(mine) == 1
        assert theirs == [], "another tenant's browser received these users"

    def test_a_genuinely_global_frame_still_reaches_everyone(self):
        # Node heartbeats and tailer errors are about the machine, not a
        # tenant, and must keep fanning out.
        import asyncio

        class _Sock:
            def __init__(self):
                self.sent = []

            async def accept(self):
                pass

            async def send_text(self, payload):
                self.sent.append(payload)

        async def _run():
            hub = self._hub()
            a, b = _Sock(), _Sock()
            await hub.join(a, 7)
            await hub.join(b, 66)
            await hub.broadcast({"type": "NODE_HEARTBEAT"})
            return a.sent, b.sent

        a, b = asyncio.run(_run())
        assert len(a) == 1 and len(b) == 1

    def test_the_tailer_builds_one_payload_per_watching_tenant(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        tail = src.split("async def _tailer")[1].split("\n@app")[0]
        assert "HUB.accounts()" in tail
        assert "_account_db_path(account_id)" in tail
        # and the diff is per tenant, or one busy account starves the others
        assert "_last_snapshot.get(account_id)" in tail


class TestTheSocketIsKeyedToTheMigrationItShowed:
    """The snapshot and the frames after it must be about one migration.

    The ledger came from _account_in_context and the delivery key from
    op.account_id. Those are the same number for a tenant, so nothing broke
    for the audience the scoping was written for -- and they differ for
    exactly the caller who reported the bug: a superadmin watching somebody
    else's run. Their snapshot showed account 7, their socket was filed
    under their own empty tenant, and every later frame was addressed to 7.

    The page rendered correct once and then never moved again, which reads
    as a stalled migration rather than a mis-addressed socket.
    """

    def _superadmin_watching(self, monkeypatch, running_account):
        import api_server
        monkeypatch.setattr(api_server.job_admission, "list_active",
                            lambda: [{"job_name": "migrate",
                                      "account_id": running_account,
                                      "pid": 1}])
        monkeypatch.setattr(api_server.job_admission, "is_live",
                            lambda j: True)
        return api_server.Operator(name="boss", role="admin",
                                   account_id=66, is_superadmin=True)

    def test_the_key_and_the_ledger_name_the_same_account(self, monkeypatch):
        import api_server
        op = self._superadmin_watching(monkeypatch, 7)
        assert op.account_id == 66              # their own tenant, empty
        assert api_server._account_in_context(op) == 7
        # so keying the socket by op.account_id would file it under 66
        # while its snapshot was built from 7's ledger.

    def test_the_handler_keys_by_context_not_by_the_caller(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        body = src.split("async def ws_endpoint")[1].split("\n@app")[0]
        assert "HUB.join(ws, _ws_account)" in body
        assert "_ws_account = _account_in_context(op)" in body
        assert "HUB.join(ws, op.account_id)" not in body, (
            "the socket is filed under the caller's own tenant while its "
            "snapshot came from the migration in context")

    def test_frames_for_the_watched_run_reach_the_watcher(self, monkeypatch):
        """The end-to-end shape: what the tailer sends is what arrives."""
        import asyncio

        import api_server
        op = self._superadmin_watching(monkeypatch, 7)

        class _Sock:
            def __init__(self):
                self.sent = []

            async def accept(self):
                pass

            async def send_text(self, payload):
                self.sent.append(payload)

        async def _run():
            hub = api_server.Hub()
            ws = _Sock()
            await hub.join(ws, api_server._account_in_context(op))
            # the tailer builds this frame for the account it scanned
            await hub.broadcast({"type": "JOB_PROGRESS"}, 7)
            return ws.sent

        assert len(asyncio.run(_run())) == 1, (
            "the watcher saw a snapshot of account 7 and then nothing")
