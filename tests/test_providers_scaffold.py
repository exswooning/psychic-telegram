"""OneDrive and Zoho WorkDrive are scaffolding, and a scaffold must never pass for working code.

Three things are pinned: only Google is marked implemented; every scaffold operation refuses by
name instead of returning something plausible; and the interface asks for what drive_engine.py
really calls, so it cannot drift into an invention nothing needs.
"""
import inspect
import os

import pytest

import providers as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPERATIONS = [n for n, f in inspect.getmembers(P.DriveProvider, inspect.isfunction) if not n.startswith("_")]


class TestOnlyGoogleIsImplemented:
    def test_exactly_google(self):
        assert [p.id for p in P.implemented()] == ["google"]

    @pytest.mark.parametrize("pid", ["onedrive", "zoho_workdrive"])
    def test_the_others_say_so(self, pid):
        assert P.get(pid).implemented is False

    def test_a_scaffold_cannot_be_marked_implemented_without_leaving_the_scaffold_list(self):
        """Flipping `implemented` is the whole promise. It must come with the class no longer being
        a stub, so both lists have to change in one commit."""
        for pid, cls in P.DRIVE_PROVIDERS.items():
            stubbed = all(getattr(cls, n) is getattr(P.DriveProvider, n) for n in OPERATIONS)
            assert stubbed == (not P.get(pid).implemented), (
                f"{pid}: implemented={P.get(pid).implemented} but its operations are "
                f"{'all still stubs' if stubbed else 'overridden'}")

    def test_an_unknown_provider_is_named_not_guessed(self):
        with pytest.raises(ValueError, match="known: google, onedrive, zoho_workdrive"):
            P.get("dropbox")


class TestAScaffoldRefusesByName:
    @pytest.mark.parametrize("pid", ["onedrive", "zoho_workdrive"])
    @pytest.mark.parametrize("op", OPERATIONS)
    def test_every_operation_refuses(self, pid, op):
        prov = P.DRIVE_PROVIDERS[pid]()
        params = [p for p in inspect.signature(getattr(prov, op)).parameters]
        with pytest.raises(P.NotImplementedProvider) as exc:
            result = getattr(prov, op)(*["x"] * len(params))
            list(result)                # a generator refuses on first use
        assert P.get(pid).label in str(exc.value) and op in str(exc.value) and "Google Workspace" in str(exc.value)

    def test_the_refusal_is_a_NotImplementedError_so_nothing_catches_it_as_a_data_problem(self):
        assert issubclass(P.NotImplementedProvider, NotImplementedError)


class TestTheInterfaceIsWhatTheDriveEngineCalls:
    ENGINE_CALLS = {
        "list_children": "files().list", "get_item": "files().get", "download": "get_media",
        "list_grants": "permissions().list", "list_comments": "comments().list",
        "create_folder": "files().create", "upload": "files().create", "set_modified": "files().update",
        "create_grant": "permissions().create", "create_comment": "comments().create",
    }

    def test_every_operation_has_a_counterpart_in_the_engine(self):
        src = open(os.path.join(ROOT, "drive_engine.py"), encoding="utf-8").read().replace("\n", "").replace(" ", "")
        for op, needle in self.ENGINE_CALLS.items():
            assert needle.replace(" ", "") in src, f"{op}: the engine never calls {needle}"

    def test_and_nothing_is_offered_that_is_not_in_that_list(self):
        assert sorted(OPERATIONS) == sorted(self.ENGINE_CALLS)


class TestTheRecordsAreUsable:
    @pytest.mark.parametrize("pid", ["onedrive", "zoho_workdrive"])
    def test_a_scaffold_says_how_a_tenant_would_grant_access_and_where_the_api_is(self, pid):
        p = P.get(pid)
        assert p.auth and p.api.startswith("https://") and p.notes

    def test_the_scaffolds_admit_their_endpoints_have_never_been_run(self):
        assert "VERIFY" in P.__doc__ and "never been run" in P.__doc__

    def test_nothing_imports_it_yet_so_no_engine_behaviour_can_have_changed(self):
        for name in ("drive_engine.py", "gmail_engine.py", "main.py", "api_server.py"):
            assert "import providers" not in open(os.path.join(ROOT, name), encoding="utf-8").read(), name
