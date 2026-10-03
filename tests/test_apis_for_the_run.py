"""Every required Cloud API on before a run: enabled on the target, only reported on
the source -- a source service account can live in the client's own Cloud project.
Live, Docs/Sheets/Slides were off and every in-place link rewrite 403'd."""
import api_server


def _fake(monkeypatch, tgt=None, src=None):
    import ensure_apis
    calls = []

    def ensure(st, tenant, do_enable=False):
        calls.append((tenant, do_enable))
        return (tgt if tenant == "target" else src) or {}
    monkeypatch.setattr(ensure_apis, "ensure", ensure)
    return calls


def test_the_target_is_enabled_and_the_source_only_read(monkeypatch):
    calls = _fake(monkeypatch)
    api_server._apis_for_the_run(None)
    assert calls == [("target", True), ("source", False)]


def test_it_says_what_it_did_and_what_it_left(monkeypatch):
    _fake(monkeypatch,
          tgt={"enabled_now": {"docs.googleapis.com": "", "slides.googleapis.com": "403 denied"}},
          src={"disabled": ["sheets.googleapis.com"]})
    notes = api_server._apis_for_the_run(None)
    assert notes[0] == "enabled on the target project: docs.googleapis.com"
    assert "could not enable on the target project: slides.googleapis.com" in notes[1]
    assert "off on the source project (not changed" in notes[2] and "sheets" in notes[2]


def test_nothing_off_says_nothing(monkeypatch):
    _fake(monkeypatch, tgt={"enabled_now": {}}, src={"disabled": []})
    assert api_server._apis_for_the_run(None) == []


def test_a_failure_never_blocks_the_launch(monkeypatch):
    import ensure_apis
    monkeypatch.setattr(ensure_apis, "ensure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert api_server._apis_for_the_run(None)[0].startswith("Cloud APIs not checked")


def test_the_launch_asks_for_it_on_every_real_run():
    import inspect
    src = inspect.getsource(api_server.migrate_start)
    assert "_apis_for_the_run" in src and "if not body.dry_run" in src
