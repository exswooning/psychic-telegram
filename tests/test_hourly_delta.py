"""The hourly delta asks the API, like a button press; a refusal is printed, never raised (cron tries again)."""
import urllib.error

import hourly_delta


def test_it_asks_for_a_drive_delta_and_survives_a_refusal(monkeypatch, capsys):
    import campaign
    sent = []

    class D:
        def __init__(self, aid, name): pass
        def api(self, m, path, body):
            sent.append((m, path, body))
            if len(sent) > 1:
                raise urllib.error.HTTPError(path, 409, "busy", {}, None)
            return {"ok": True}
    monkeypatch.setattr(campaign, "LiveDriver", D)
    assert hourly_delta.main(["--account-id", "7", "--services", "drive", "--days", "1"]) == 0
    m, path, body = sent[0]
    assert path == "/api/v2/migrate/delta" and body["services"] == ["drive"] and body["days"] == 1 and body["account_id"] == 7
    assert hourly_delta.main(["--account-id", "7"]) == 0 and "refused 409" in capsys.readouterr().out
