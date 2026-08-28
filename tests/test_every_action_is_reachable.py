"""An ACTIONS entry with no button is a feature that does not exist.

Seven had one. Two of them are whole services the per-user migration
cannot cover by definition:

    shared_drives_inventory / shared_drives_migrate
        a shared drive belongs to no single person
    sso_inventory / sso_migrate
        inbound SAML profiles
    phased_count_only / phased_migrate
        every service in order, reconciled against the tenants directly
    scope
        what migrates, and the exact OAuth scopes this config needs

The only way to run any of them was to SSH in. phased_count_only is the
one that proves the point: it is the fidelity check, and reaching it
during this session meant POSTing to /api/run by hand.

An action counts as reachable if a page names it, or if a wizard step
offers it (STEP_ACTIONS drives that list). This asserts the union covers
everything.
"""
import os

import webui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _frontend() -> str:
    blob = []
    for d, _, fs in os.walk(os.path.join(ROOT, "migration-webui/src")):
        for f in fs:
            if f.endswith((".ts", ".tsx")) and ".test." not in f:
                blob.append(open(os.path.join(d, f), encoding="utf-8",
                                 errors="replace").read())
    return "\n".join(blob)


def _step_actions() -> set:
    out = set()
    for v in webui.STEP_ACTIONS.values():
        out.update(v)
    return out


class TestNothingIsStranded:
    def test_every_action_is_reachable_from_the_ui(self):
        front, steps = _frontend(), _step_actions()
        stranded = [k for k in webui.ACTIONS
                    if k not in steps
                    and f"'{k}'" not in front and f'"{k}"' not in front]
        assert not stranded, (
            "these have an ACTIONS entry and no way to run them from the "
            f"app: {stranded}")

    def test_the_check_is_not_vacuous(self):
        # A frontend that failed to load would make the assertion above
        # pass by accident.
        assert len(_frontend()) > 50_000
        assert len(webui.ACTIONS) > 20


class TestTheServicesPageCarriesTheOrphans:
    def _page(self):
        return open(os.path.join(ROOT, "migration-webui/src/pages/Services.tsx"),
                    encoding="utf-8").read()

    def test_it_offers_shared_drives(self):
        src = self._page()
        assert "shared_drives_inventory" in src and "shared_drives_migrate" in src

    def test_it_offers_sso(self):
        src = self._page()
        assert "sso_inventory" in src and "sso_migrate" in src

    def test_it_offers_the_phased_run(self):
        src = self._page()
        assert "phased_count_only" in src and "phased_migrate" in src

    def test_it_says_why_shared_drives_need_their_own_step(self):
        import re
        src = re.sub(r"\s+", " ", self._page())
        assert "belongs to no single person" in src

    def test_it_warns_that_sso_profiles_land_unassigned(self):
        """A wrong assignment locks users out of the tenant they were just
        migrated into."""
        import re
        src = re.sub(r"\s+", " ", self._page())
        assert "unassigned" in src and "locks users out" in src

    def test_it_is_routed_and_in_the_nav(self):
        app = open(os.path.join(ROOT, "migration-webui/src/App.tsx"),
                   encoding="utf-8").read()
        nav = open(os.path.join(ROOT, "migration-webui/src/components/Layout.tsx"),
                   encoding="utf-8").read()
        assert 'path="/services"' in app
        assert "'/services'" in nav

    def test_a_missing_action_hides_its_card_rather_than_crashing(self):
        # actions[] is empty until the fetch lands, and an older server may
        # not offer all of them.
        src = self._page()
        assert "const has = (k: string) => Boolean(actions[k])" in src


class TestScopeGotItsOwnAction:
    def test_the_scope_page_runs_it(self):
        src = open(os.path.join(ROOT, "migration-webui/src/pages/Scope.tsx"),
                   encoding="utf-8").read()
        assert 'name="scope"' in src
        assert 'name="export_scope"' in src
