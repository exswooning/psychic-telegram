"""The OAuth connection path was reachable only by curl.

/api/oauth/status, /begin and /disconnect have existed from the start,
oauth_store.py and auth.py._oauth_credentials implement it fully, and no
page ever called any of them.

The panel leads with the constraint rather than the button, because this
is a decision somebody can make wrongly and only discover much later:

    an OAuth grant acts as the CONSENTING ADMIN, not as an arbitrary user.
    There is no `subject` to switch into another mailbox, so a tenant
    connected this way can migrate exactly one account.

auth.py refuses loudly rather than silently migrating the wrong mailbox,
which is correct -- but by then a tenant has been configured that cannot
do what it was configured for. Saying it before the button is cheaper.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _panel():
    return open(os.path.join(ROOT, "migration-webui/src/components/OAuthConnect.tsx"),
                encoding="utf-8").read()


def _client():
    return open(os.path.join(ROOT, "migration-webui/src/api/client.ts"),
                encoding="utf-8").read()


class TestTheEndpointsAreCalled:
    def test_status_is_read(self):
        assert "fetchOAuthStatus" in _panel()
        assert "'/api/oauth/status'" in _client()

    def test_connect_and_disconnect_exist(self):
        src = _client()
        assert "'/api/oauth/begin'" in src
        assert "'/api/oauth/disconnect'" in src

    def test_both_tenants_are_offered(self):
        src = _panel()
        assert "row('source')" in src and "row('target')" in src


class TestItLeadsWithTheConstraint:
    def test_the_single_account_limit_is_stated(self):
        import re
        src = re.sub(r"\s+", " ", _panel())
        assert "migrate exactly one account" in src

    def test_it_points_at_delegation_for_a_whole_tenant(self):
        import re
        src = re.sub(r"\s+", " ", _panel())
        assert "domain-wide delegation" in src

    def test_the_warning_outranks_the_buttons(self):
        # A caveat below the button is a caveat nobody reads.
        src = _panel()
        assert src.index("acts as the admin who consented") < src.index("row('source')")

    def test_the_localhost_redirect_is_called_out(self):
        """It redirects to localhost, so it cannot be finished from a
        browser pointed at the public host -- which is how most people
        would first try it."""
        import re
        src = re.sub(r"\s+", " ", _panel())
        assert "localhost" in src and "SSH tunnel" in src


class TestItDegradesHonestly:
    def test_missing_client_secrets_says_what_to_do(self):
        import re
        src = re.sub(r"\s+", " ", _panel())
        assert "No OAuth client secrets on file" in src
        assert "done once, by you, not by each tenant" in src

    def test_connect_is_disabled_without_them(self):
        assert "disabled={!st?.configured" in _panel()

    def test_disconnect_is_disabled_when_not_connected(self):
        assert "disabled={!on" in _panel()

    def test_it_is_mounted_next_to_the_delegation_setup(self):
        # It is the alternative to DWD; anywhere else invites configuring
        # both.
        src = open(os.path.join(ROOT, "migration-webui/src/pages/SeedWizard.tsx"),
                   encoding="utf-8").read()
        assert "<OAuthConnect />" in src
        assert src.index("<DwdSetup />") < src.index("<OAuthConnect />")
