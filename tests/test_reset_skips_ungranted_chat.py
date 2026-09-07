"""A pre-check that knows the work cannot succeed should stop it.

reset_target mints a token for chat.delete to decide whether Chat spaces can
be removed. When that fails it printed "Chat spaces will survive this reset"
and then ran the Chat phase for every user anyway -- each one calling
spaces.list, getting 403, and burning SCOPE_RETRY_BUDGET (11 attempts) before
moving on.

Measured on a live 200-user reset: about 1.45 of those blocks a minute, ~138
minutes of the run spent retrying a scope nobody had granted.
"""
import inspect

import reset_target


class TestTheChatPhaseIsDroppedNotAnnounced:
    def test_the_ungranted_branch_removes_chat_from_services(self):
        src = inspect.getsource(reset_target.main)
        i = src.index("chat.delete is not granted")
        window = src[max(0, i - 600):i]
        assert 'services = tuple(x for x in services if x != "chat")' in window, (
            "the pre-check still only prints; every user will retry a 403 "
            "eleven times")

    def test_the_granted_branch_still_enables_deletion(self):
        """The other half must keep working: a grant made months ago with the
        flag never following is the case this pre-check exists for."""
        src = inspect.getsource(reset_target.main)
        assert "settings.chat_allow_delete = True" in src
        assert 'os.environ["CHAT_ALLOW_DELETE"] = "true"' in src

    def test_chat_is_still_a_valid_service_to_ask_for(self):
        """Dropping it on a failed pre-check must not remove the ability to
        request it when the scope IS granted."""
        assert "chat" in reset_target.ALL_SERVICES
