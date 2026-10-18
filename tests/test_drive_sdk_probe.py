"""Live, dharan.gov.np: its read-only grant holds drive.readonly, not `drive`; probing
with `drive` got "unauthorized_client" and the Drive SDK was never switched on."""
import full_setup as fs


def _probe(monkeypatch, answers):
    from google.oauth2 import service_account
    import googleapiclient.discovery as disc

    class Creds:
        def __init__(self, scope):
            self.scope = scope

        def with_subject(self, _who):
            return self

    monkeypatch.setattr(service_account.Credentials, "from_service_account_file",
                        lambda path, scopes: Creds(scopes[0].rsplit("/", 1)[-1]))

    def build(api, version, credentials, cache_discovery):
        outcome = answers[credentials.scope]

        class Svc:
            def about(self):
                return self

            def get(self, fields):
                return self

            def execute(self):
                if outcome:
                    raise Exception(outcome)
                return {}
        return Svc()
    monkeypatch.setattr(disc, "build", build)
    return fs._drive_sdk_off("k.json", "it@gov.example")


def test_a_read_only_grant_still_detects_drive_apps_disabled(monkeypatch):
    assert _probe(monkeypatch, {"drive.readonly": "The domain administrators have disabled Drive apps.",
                                "drive": "unauthorized_client"}) is True


def test_a_target_holding_only_drive_is_asked_with_it(monkeypatch):
    assert _probe(monkeypatch, {"drive.readonly": "unauthorized_client",
                                "drive": "The domain administrators have disabled Drive apps."}) is True


def test_drive_answering_is_not_this(monkeypatch):
    assert _probe(monkeypatch, {"drive.readonly": "", "drive": "unauthorized_client"}) is False
