"""A handler that raises must say so, not close the socket.

Live: the wipe endpoint hit an UnboundLocalError, the exception escaped into
socketserver, which closes the connection without writing anything. Caddy
returned a bare 502 with no body, and the dialog showed

    SyntaxError: Failed to execute 'json' on 'Response':
    Unexpected end of JSON input

which names the symptom and nothing else -- no route, no exception, no clue
that a Python error had happened at all.
"""
import inspect

import webui


class TestEveryHandlerIsGuarded:
    def test_get_and_post_both_go_through_the_guard(self):
        for name in ("do_GET", "do_POST"):
            src = inspect.getsource(getattr(webui.Handler, name))
            assert "_guard" in src, f"{name} can still escape into socketserver"

    def test_the_real_work_still_exists_underneath(self):
        assert hasattr(webui.Handler, "_do_GET")
        assert hasattr(webui.Handler, "_do_POST")

    def test_the_guard_answers_in_json(self):
        src = inspect.getsource(webui.Handler._guard)
        assert "self._json(" in src
        assert '"ok": False' in src

    def test_it_names_the_exception_and_the_route(self):
        """"something went wrong" would be the same non-answer the browser
        already gives."""
        src = inspect.getsource(webui.Handler._guard)
        assert "type(exc).__name__" in src
        assert '"where"' in src

    def test_the_traceback_still_reaches_the_log(self):
        """Containing a crash must not hide it from whoever has to fix it."""
        assert "traceback.print_exc()" in inspect.getsource(webui.Handler._guard)

    def test_a_client_that_hung_up_is_not_reported_to(self):
        """There is nothing to write to, and the noise would bury real
        failures."""
        src = inspect.getsource(webui.Handler._guard)
        assert "BrokenPipeError" in src and "ConnectionResetError" in src

    def test_a_failure_to_report_does_not_become_a_second_crash(self):
        """The response may already have begun."""
        src = inspect.getsource(webui.Handler._guard)
        i = src.index("except Exception:")
        assert "pass" in src[i:i + 120]


class TestTheBugThatFoundIt:
    def test_the_removal_handler_binds_settings_itself(self):
        """Another branch of do_POST imports Settings inside its own `if`,
        which makes it a LOCAL of the whole method -- so reaching it from a
        branch that did not run raises UnboundLocalError."""
        src = inspect.getsource(webui.Handler._do_POST)
        i = src.index('if self.path == "/api/remove_tenant_setup":')
        j = src.index('if self.path == "/api/reset_target":', i)
        assert "from config import Settings as _Settings" in src[i:j]
