"""Exercise the real heartbeat callback before any item detail is available."""
import threading
from types import SimpleNamespace

import verify_sample as V


def test_heartbeat_survives_initialisation_and_reports_later_item(monkeypatch):
    real_thread, real_event = threading.Thread, threading.Event
    heartbeat = []
    events = []

    class TickEvent:
        def __init__(self):
            self.calls = 0
        def wait(self, timeout):
            self.calls += 1
            return self.calls % 2 == 0
        def set(self):
            pass

    def event_factory():
        # Only run()'s stop_heartbeats event; leave executor events unchanged.
        if not events:
            events.append(TickEvent())
            return events[0]
        return real_event()

    def thread_factory(*args, **kwargs):
        if kwargs.get('name') == 'verify-heartbeat':
            heartbeat.append(kwargs['target'])
            return SimpleNamespace(start=lambda: None, join=lambda **kw: None)
        return real_thread(*args, **kwargs)

    ticks = iter(range(0, 100000, 61))
    monkeypatch.setattr(threading, 'Event', event_factory)
    monkeypatch.setattr(threading, 'Thread', thread_factory)
    monkeypatch.setattr(V.time, 'monotonic', lambda: next(ticks))
    blank = V.Verifier._blank

    class Verifier:
        _blank = staticmethod(blank)
        def __init__(self, *args, on_progress=None, **kwargs):
            self.evidence = []
            self.progress = on_progress
        def _report_progress(self, *args):
            pass
        def contacts(self):
            heartbeat[0]()  # No item callback has populated 'detail'.
            r = blank()
            r.update(checked=1, identical=1)
            return r
        def tasks(self):
            self.progress(item='task-42', completed=0, total=1)
            heartbeat[0]()
            r = blank()
            r.update(checked=1, identical=1)
            return r

    monkeypatch.setattr(V, 'Verifier', Verifier)
    db = SimpleNamespace(all_identities=lambda: [dict(entity_type='user', source_email='u@s', target_email='u@t')])
    lines = []
    result = V.run(None, db, SimpleNamespace(source_domain='s', target_domain='t'),
                   services=('contacts', 'tasks'), progress=lines.append)
    beats = [line for line in lines if line.startswith('verify: heartbeat')]
    assert len(beats) == 2
    assert 'service=contacts' in beats[0] and 'currentItem=' not in beats[0]
    assert 'service=tasks' in beats[1] and 'currentItem=task-42' in beats[1]
    assert result['totals']['identical'] == 2
