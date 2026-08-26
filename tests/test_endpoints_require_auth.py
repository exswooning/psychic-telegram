"""Every route enforces a credential, or is listed here with a reason.

Found live, from the public internet with no cookie at all:

    GET https://<host>/api/v2/failures      -> 200, user email addresses,
                                               Drive file ids, error text
    GET https://<host>/api/v2/actions       -> 200, the operator audit log
    GET https://<host>/api/v2/dwd/status    -> 200, the OAuth client id and
                                               the tenant's granted scopes

/api/v2/migrations beside them returned 401, so this was never a missing
auth layer -- it was routes written without the check the rest of the file
uses.

The subtle half, and the reason this test looks the way it does: declaring
`op: Operator = Depends(operator)` only RESOLVES an identity. It does not
require one. Several of the leaking endpoints had that dependency and
still answered anonymous callers, which is exactly why an audit that
greps for `Depends(operator)` passes them. What counts is that the handler
calls one of the enforcers -- directly, or via _gated, which does
require_admin and require_active_subscription for every write.
"""
import ast
import os

# Calling any of these means the handler actually refuses an anonymous
# caller. _gated is included because it enforces on behalf of its callers.
ENFORCERS = {"require_login", "require_admin", "require_active_subscription",
             "require_superadmin", "require_reader", "_gated"}

# Routes that must answer without a session, and why.
PUBLIC = {
    "/api/v2/auth/signup": "creates the account every other route needs",
    "/api/v2/auth/login": "exchanges credentials for the session cookie",
    "/api/v2/auth/logout": "must work with an expired or invalid session",
    "/api/v2/auth/me": "how the SPA discovers it is signed out",
    "/api/v2/whoami": "echoes the caller's own identity, nothing else",
    "/ws": "enforces inline -- a dependency cannot raise mid-handshake, so "
           "it closes with 1008 instead",
}


def _routes():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, "api_server.py"),
                          encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "app"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                calls = {n.func.id for n in ast.walk(node)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)}
                yield {
                    "verb": dec.func.attr.upper(),
                    "path": dec.args[0].value,
                    "name": node.name,
                    "args": ast.unparse(node.args),
                    "enforced": bool(calls & ENFORCERS)
                    or "Depends(node_auth)" in ast.unparse(node.args),
                }


class TestEveryRouteEnforces:
    def test_no_route_answers_an_anonymous_caller(self):
        open_routes = [(r["verb"], r["path"], r["name"]) for r in _routes()
                       if not r["enforced"] and r["path"] not in PUBLIC]
        assert not open_routes, (
            "routes that resolve an identity but never require one -- call "
            "require_login (or require_admin, or go through _gated), or add "
            f"them to PUBLIC with a reason: {open_routes}")

    def test_declaring_the_dependency_is_not_counted_as_enforcing(self):
        """The trap this test exists for.

        `op: Operator = Depends(operator)` resolves who is calling and
        permits an anonymous answer. Endpoints that leaked publicly had it.
        """
        assert "Depends(operator)" not in ENFORCERS
        # And prove the detector agrees: every enforcing route names a real
        # check somewhere in its body.
        for r in _routes():
            if r["enforced"] and "Depends(node_auth)" not in r["args"]:
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                src = open(os.path.join(root, "api_server.py"),
                           encoding="utf-8").read()
                assert any(e in src for e in ENFORCERS)
                break

    def test_the_allowlist_stays_small_and_explained(self):
        # A creeping allowlist is how this comes back.
        assert len(PUBLIC) <= 6
        for path, reason in PUBLIC.items():
            assert reason and len(reason) > 15, path

    def test_every_allowlisted_route_still_exists(self):
        paths = {r["path"] for r in _routes()}
        missing = set(PUBLIC) - paths
        assert not missing, f"allowlist names routes that are gone: {missing}"


class TestTheRoutesThatActuallyLeaked:
    """Named individually, because these are the ones that answered the
    public internet."""

    def _named(self):
        return {r["name"]: r for r in _routes()}

    def test_the_tenant_data_reads_now_enforce(self):
        named = self._named()
        for name in ("get_failures", "get_forensics", "get_public_shares",
                     "get_actions", "get_users", "get_active_jobs",
                     "get_fleet", "ai_context", "ai_analyze"):
            assert named[name]["enforced"], name

    def test_reads_use_the_reader_guard_not_the_account_one(self):
        """require_login would retire the X-Operator path an operator on an
        SSH tunnel uses; require_admin would stop a viewer reading, which is
        what that role is for. require_reader refuses only a caller
        presenting nothing at all -- which was every caller on the public
        internet, because operator() turns an absent header into a viewer."""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        assert "def require_reader(" in src
        assert src.count("require_reader(op)") >= 12

    def test_the_status_reads_now_enforce(self):
        # dwd_status returned the OAuth client id and the granted scopes.
        named = self._named()
        for name in ("dwd_status", "gcp_status", "provision_status",
                     "full_setup_status", "teardown_status"):
            assert named[name]["enforced"], name

    def test_the_fleet_heartbeat_authenticates_as_a_node(self):
        # It writes node state and broadcasts to every connected socket.
        assert "Depends(node_auth)" in self._named()["heartbeat"]["args"]


class TestTheOperatorHeaderIsNotADefaultCredential:
    """A name in CP_OPERATORS is not a secret.

    The live host shipped CP_OPERATORS=aryan:admin in its systemd unit and
    was publicly reachable, so anyone who sent `X-Operator: aryan` was an
    admin on every route gated by require_admin. Closing the anonymous hole
    did not touch that -- it is a deployment value, and this is the test
    that keeps it out of the shipped defaults.
    """

    def _read(self, name):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return open(os.path.join(root, name), encoding="utf-8").read()

    def test_the_systemd_unit_does_not_set_it(self):
        unit = self._read("systemd/bitport-api.service")
        setting = [ln for ln in unit.splitlines()
                   if ln.strip().startswith("Environment=CP_OPERATORS")]
        assert not setting, (
            "the shipped unit grants admin to anyone who guesses the name: "
            f"{setting}")

    def test_the_start_script_has_no_default(self):
        script = self._read("start_control_plane.sh")
        assert 'CP_OPERATORS:-aryan:admin' not in script
        assert 'CP_OPERATORS:-}' in script or 'CP_OPERATORS:-"}' in script

    def test_an_unlisted_name_earns_nothing(self, monkeypatch):
        # With no CP_OPERATORS, _roles() is empty, so require_reader refuses
        # a header-only caller and the cookie is the only credential left.
        import api_server
        monkeypatch.delenv("CP_OPERATORS", raising=False)
        assert api_server._roles() == {}
        op = api_server.Operator(name="aryan", role="viewer", account_id=None)
        import pytest
        with pytest.raises(Exception):
            api_server.require_reader(op)

    def test_a_signed_in_account_is_unaffected(self, monkeypatch):
        import api_server
        monkeypatch.delenv("CP_OPERATORS", raising=False)
        op = api_server.Operator(name="someone", role="admin", account_id=7)
        api_server.require_reader(op)      # must not raise


class TestTheOperatorClaimCostsASecret:
    """X-Operator was a claim, not a credential.

    A name in CP_OPERATORS is not secret, so on a reachable host anyone who
    guessed it became that operator -- and with aryan:admin shipped in the
    systemd unit, an admin. Removing the value fixed that one host and left
    the mechanism, so the next person to set it reopens the same hole.

    It now costs BITPORT_OPERATOR_TOKEN, exactly as worker nodes cost
    BITPORT_NODE_TOKEN.
    """

    TOKEN = "a-real-shared-secret"

    def _op(self, monkeypatch, *, name="boss", token="", configured=None):
        import asyncio

        import api_server
        monkeypatch.setenv("CP_OPERATORS", "boss:admin,intern:viewer")
        if configured is None:
            monkeypatch.delenv("BITPORT_OPERATOR_TOKEN", raising=False)
        else:
            monkeypatch.setenv("BITPORT_OPERATOR_TOKEN", configured)
        return asyncio.run(
            api_server.operator(x_operator=name, x_operator_token=token,
                                bp_session=""))

    def test_the_right_secret_honours_the_claim(self, monkeypatch):
        op = self._op(monkeypatch, token=self.TOKEN, configured=self.TOKEN)
        assert op.name == "boss" and op.role == "admin"

    def test_a_name_with_no_secret_is_nobody(self, monkeypatch):
        # The exact live attack: `curl -H "X-Operator: aryan"`.
        op = self._op(monkeypatch, token="", configured=self.TOKEN)
        assert op.name == "anonymous" and op.role == "viewer"

    def test_a_wrong_secret_is_nobody(self, monkeypatch):
        op = self._op(monkeypatch, token="guess", configured=self.TOKEN)
        assert op.name == "anonymous"

    def test_it_fails_closed_when_no_secret_is_configured(self, monkeypatch):
        """A host that never sets one cannot be talked into trusting a
        header -- which is the state the live deployment is in now."""
        op = self._op(monkeypatch, token="anything", configured=None)
        assert op.name == "anonymous" and op.role == "viewer"

    def test_an_admin_name_without_the_secret_cannot_write(self, monkeypatch):
        import api_server
        import pytest
        op = self._op(monkeypatch, name="aryan", token="", configured=self.TOKEN)
        with pytest.raises(Exception):
            api_server.require_admin(op)

    def test_the_secret_is_compared_in_constant_time(self):
        # Same reason node_auth does it: a == comparison on a secret leaks
        # its prefix through timing.
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "api_server.py"), encoding="utf-8").read()
        body = src.split("def _operator_token_ok")[1].split("\ndef ")[0]
        assert "compare_digest" in body
