"""
tests/test_account_scoping.py
=============================
A bare `Settings()` inside account-aware code is a bug, every time.

What this catches
-----------------
`Settings()` with no argument reads the LEGACY tenant from env.sh.
`Settings(account_id=N)` reads that SaaS account's `tenant_configs` row.
In any function that already knows which account it is acting for, the bare
form silently operates on somebody else's tenant.

Four instances of this shipped, and none of them announced itself:

* `/api/v2/dwd/status` had no operator dependency at all and reported the
  legacy tenant's delegation to every account -- it answered "0 of 14 scopes"
  for a tenant that was, in the same minute, successfully reading its users'
  mailboxes with that very key.
* `apis_enable` is a *write*. It enabled APIs on the legacy project and
  reported success, while the caller's own project stayed broken.
* `benchmark_start` validated the typed confirmation string for a
  destructive action against the wrong tenant's name.
* `full_setup`'s verification loop read the legacy key, so a setup that had
  actually succeeded burned its entire retry budget reporting "0/11 scopes
  live" and then failed -- whose advertised remedy is to re-run setup, which
  mints another throwaway GCP project.

Every one was found by hand, well after shipping, and three of them looked
exactly like a Google-side problem. The class is invisible in review because
the correct and incorrect calls differ by one keyword argument, so it gets a
static check rather than a convention.

The rule
--------
A function that knows its account -- it takes `account_id`, or an operator
dependency carrying one -- must not call `Settings()` bare. The one accepted
exception is a fallback inside an `except` handler, where the account-scoped
read has already been tried and failed; that is a deliberate degradation and
reads as one at the call site.
"""

from __future__ import annotations

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules that serve more than one tenant. A module absent from this list is
# single-tenant by construction (the CLI, the seeder) and bare Settings() is
# correct there.
ACCOUNT_AWARE_MODULES = ["api_server.py", "full_setup.py"]


def _account_aware(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Does this function already know which account it is acting for?"""
    args = fn.args
    for a in [*args.args, *args.posonlyargs, *args.kwonlyargs]:
        if a.arg == "account_id":
            return True
        # `op: Operator = Depends(operator)` -- the account travels on `op`.
        ann = getattr(a, "annotation", None)
        if isinstance(ann, ast.Name) and ann.id == "Operator":
            return True
    return False


def _none_branch_test(test: ast.AST) -> str:
    """Is this `if` explicitly branching on having no account?

    Returns "body" if the body is the no-account branch, "orelse" if the
    else is, and "" if this test is not an account_id-vs-None check at all.
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return ""
    if not any(isinstance(n, ast.Name) and n.id == "account_id"
               or isinstance(n, ast.Attribute) and n.attr == "account_id"
               for n in ast.walk(test.left)):
        return ""
    comparator = test.comparators[0]
    if not (isinstance(comparator, ast.Constant) and comparator.value is None):
        return ""
    if isinstance(test.ops[0], ast.Is):          # account_id is None
        return "body"
    if isinstance(test.ops[0], ast.IsNot):       # account_id is not None
        return "orelse"
    return ""


def _bare_settings_calls(node: ast.AST) -> list[ast.Call]:
    """`Settings()` with no arguments, in code that claims to have an account.

    Two branches are exempt, because in both the code has already
    established that no account-scoped read is available:

    * inside an `except` handler -- the scoped read was tried and failed;
    * inside the no-account branch of an explicit `account_id is None`
      test -- the legacy/tunnel caller genuinely has no tenant_configs row,
      and env.sh is its real source of truth.
    """
    found: list[ast.Call] = []

    class V(ast.NodeVisitor):
        def visit_ExceptHandler(self, n):        # noqa: N802
            # A documented fallback after the scoped read failed. Not a bug.
            return

        def visit_If(self, n):                   # noqa: N802
            skip = _none_branch_test(n.test)
            self.visit(n.test)
            for name in ("body", "orelse"):
                if name == skip:
                    continue
                for stmt in getattr(n, name):
                    self.visit(stmt)

        def visit_Call(self, n):                 # noqa: N802
            f = n.func
            if (isinstance(f, ast.Name) and f.id == "Settings"
                    and not n.args and not n.keywords):
                found.append(n)
            self.generic_visit(n)

    V().visit(node)
    return found


def _offenders(path: str) -> list[str]:
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out: list[str] = []
    name = os.path.basename(path)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _account_aware(fn):
            continue
        for call in _bare_settings_calls(fn):
            out.append(f"{name}:{call.lineno} in {fn.name}()")
    return out


class TestNoBareSettingsInAccountAwareCode:
    def test_account_aware_functions_never_read_the_legacy_tenant(self):
        """The regression guard for all four shipped instances.

        If this fails, the named function is about to act on the legacy
        tenant instead of its caller's. Pass `account_id=` -- from the
        parameter, or from `op.account_id`.
        """
        bad: list[str] = []
        for mod in ACCOUNT_AWARE_MODULES:
            bad += _offenders(os.path.join(REPO, mod))
        assert not bad, (
            "bare Settings() in account-aware function(s):\n  "
            + "\n  ".join(bad)
            + "\n\nUse Settings(account_id=...). Bare Settings() reads the "
              "legacy env.sh tenant, not the caller's."
        )

    def test_the_check_actually_detects_the_bug_it_claims_to(self):
        """A guard nobody has seen fail is a guard that might match nothing.

        This is the exact shape of the shipped `dwd_status` bug.
        """
        tree = ast.parse(
            "async def dwd_status(op: Operator = Depends(operator)):\n"
            "    s = Settings()\n"
            "    return s\n"
        )
        fn = tree.body[0]
        assert _account_aware(fn)
        assert len(_bare_settings_calls(fn)) == 1

    def test_an_account_id_parameter_also_counts_as_account_aware(self):
        """full_setup's shape: the account arrives as a plain argument."""
        tree = ast.parse(
            "def run(account_id, side):\n"
            "    return Settings()\n"
        )
        assert _account_aware(tree.body[0])
        assert len(_bare_settings_calls(tree.body[0])) == 1

    def test_a_scoped_call_is_not_flagged(self):
        tree = ast.parse(
            "def run(account_id):\n"
            "    return Settings(account_id=account_id)\n"
        )
        assert _bare_settings_calls(tree.body[0]) == []

    def test_a_fallback_inside_except_is_allowed(self):
        """full_setup.py's deliberate degradation must stay legal -- a
        tenant_configs row that cannot be read must not abort a setup that
        has already provisioned a project and granted delegation."""
        tree = ast.parse(
            "def run(account_id):\n"
            "    try:\n"
            "        return Settings(account_id=account_id)\n"
            "    except Exception:\n"
            "        return Settings()\n"
        )
        assert _bare_settings_calls(tree.body[0]) == []

    def test_an_explicit_no_account_branch_is_allowed(self):
        """api_server's `verified_domains` shape, which this guard flagged on
        its first run: the legacy/tunnel caller has no tenant_configs row at
        all, so env.sh really is its source of truth. The branch says so."""
        tree = ast.parse(
            "def read(op):\n"
            "    if op.account_id is not None:\n"
            "        s = Settings(account_id=op.account_id)\n"
            "    else:\n"
            "        s = Settings()\n"
            "    return s\n"
        )
        assert _bare_settings_calls(tree.body[0]) == []

    def test_the_inverted_no_account_branch_is_also_allowed(self):
        tree = ast.parse(
            "def read(account_id):\n"
            "    if account_id is None:\n"
            "        return Settings()\n"
            "    return Settings(account_id=account_id)\n"
        )
        assert _bare_settings_calls(tree.body[0]) == []

    def test_the_exemption_does_not_leak_into_the_account_branch(self):
        """The narrowness that makes the exemption safe: only the branch
        that has established it has no account is excused. The same call in
        the branch that *does* have one is still the bug."""
        tree = ast.parse(
            "def read(op):\n"
            "    if op.account_id is not None:\n"
            "        return Settings()\n"
            "    else:\n"
            "        return Settings()\n"
        )
        assert len(_bare_settings_calls(tree.body[0])) == 1

    def test_a_call_after_the_branch_is_still_policed(self):
        """The exemption covers the no-account *branch*, not everything
        downstream of it -- code that has rejoined the common path is back
        to needing the account it has."""
        tree = ast.parse(
            "def read(account_id):\n"
            "    if account_id is None:\n"
            "        return Settings()\n"
            "    return Settings()\n"
        )
        assert len(_bare_settings_calls(tree.body[0])) == 1

    def test_an_unrelated_if_does_not_excuse_anything(self):
        tree = ast.parse(
            "def read(account_id):\n"
            "    if account_id > 3:\n"
            "        return Settings()\n"
            "    return Settings()\n"
        )
        assert len(_bare_settings_calls(tree.body[0])) == 2

    def test_single_tenant_modules_are_not_policed(self):
        """The CLI and the seeder are single-tenant; bare Settings() is
        correct there and must not be reported as a defect."""
        assert "main.py" not in ACCOUNT_AWARE_MODULES
        assert "seed_sandbox.py" not in ACCOUNT_AWARE_MODULES
