"""Every /api/v2 route that takes an account_id checks who may use it (8 Nov 2026: isolation was fixed one
button at a time; this makes a new route that forgets the check fail here instead of on a tenant)."""
import inspect

import api_server

GUARDS = ("_require_account_access(", "_account_for(", "_resolve_account(", "require_superadmin", "_require_superadmin(",
          "op.is_superadmin", "op.superadmin")
# Routes that hand the account to a function that does the check, or answer a machine (node token), not a person.
DELEGATES = {
    "/api/v2/user-profiles/export.csv": "calls user_profiles_status, which checks",
    "/api/v2/reports/{run_id}": "_report_or_404 checks",
    "/api/v2/reports/{run_id}/pdf": "_report_or_404 checks",
    "/api/v2/nodes/directive": "GET: node_auth token, one tenant's own directive",
}


def _takes_account(fn) -> bool:
    params = inspect.signature(fn).parameters
    if "account_id" in params:
        return True
    return any("account_id" in (getattr(p.annotation, "model_fields", None) or {}) for p in params.values())


def test_every_route_that_takes_an_account_checks_access():
    seen, loose = 0, []
    for r in api_server.app.routes:
        path, fn = getattr(r, "path", ""), getattr(r, "endpoint", None)
        if not fn or not path.startswith("/api/v2") or not _takes_account(fn):
            continue
        seen += 1
        if not any(g in inspect.getsource(fn) for g in GUARDS) and path not in DELEGATES:
            loose.append(f"{sorted(r.methods or [''])[0]} {path}")
    assert seen > 30                                   # a collection bug would make this vacuous
    assert not loose, f"routes that take an account_id and check nothing: {loose}"


def test_the_delegating_routes_still_exist():
    paths = {getattr(r, "path", "") for r in api_server.app.routes}
    assert set(DELEGATES) <= paths, "remove a route from DELEGATES when it goes"
