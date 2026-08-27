"""Running Now called a live migration "--account-id".

fleet_agent took "the token after main.py" as the job name. That token is a
FLAG whenever one is passed, and api_server.py always passes one:

    main.py --account-id 7 migrate --services drive,gmail

So every migration launched from the SaaS UI was reported to the fleet as
a job named "--account-id", and that is what the page displayed beside its
pid. webui.py had its own copy of the same scan with its own command list,
which is how the two could disagree at all.
"""
import fleet_agent
import webui


class TestTheSubcommandIsFound:
    def test_a_flag_before_the_command_is_skipped(self):
        assert fleet_agent.main_command(
            "/usr/bin/python3 main.py --account-id 7 migrate "
            "--services drive,gmail") == "migrate"

    def test_a_valueless_flag_is_skipped_too(self):
        # --dry-run takes no value, so "skip the flag and the next token"
        # would have eaten the command itself.
        assert fleet_agent.main_command(
            "python main.py --dry-run delta --days 2") == "delta"

    def test_a_bare_command_still_works(self):
        assert fleet_agent.main_command("python main.py migrate") == "migrate"

    def test_a_flag_value_is_never_mistaken_for_a_command(self):
        # --keys-dir keys/7 -- neither token is a command.
        assert fleet_agent.main_command(
            "python main.py --account-id 7 --keys-dir keys/7") is None

    def test_another_script_is_not_a_main_command(self):
        assert fleet_agent.main_command(
            "python seed_sandbox.py --confirm-domain x") is None

    def test_a_path_invocation_is_recognised(self):
        assert fleet_agent.main_command(
            "python /root/migration/main.py migrate") == "migrate"


class TestBothScannersAgree:
    def test_webui_uses_the_shared_rule(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "webui.py"), encoding="utf-8").read()
        assert "fleet_agent.main_command(args)" in src

    def test_there_is_one_command_list(self):
        assert webui._EXT_MAIN_CMDS is fleet_agent.MAIN_COMMANDS

    def test_migrate_and_delta_are_in_it(self):
        # The two that matter for "what is running right now".
        assert {"migrate", "delta"} <= set(fleet_agent.MAIN_COMMANDS)
