"""The Chat app form is behind a tab nobody clicked.

configure_chat_app opens

    console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat

which lands on the API's SERVICE DETAILS overview -- "Status: Enabled",
with tabs for Metrics, Quotas & System Limits, Credentials and
Configuration. The app name field is not rendered until Configuration is
opened, so the driver searched an overview page for a form that was not
there and returned:

    could not find the app name field -- console may have changed

That message sent every reader looking for a console redesign instead of a
missing click, and Chat was therefore never configured on any tenant this
tool has set up. Downstream, every spaces.create() returned 404, the
seeder recorded "0 spaces" as a note, and a migration skipped 199 DMs as
SKIPPED_NOT_A_SPACE while reporting success.

Taken from this function's own saved diagnostics: the page it gave up on
was captured in full and contains the tab list and no form.
"""
import gcloud_browser_auth as g


class _Loc:
    def __init__(self, n=0, visible=True, on_click=None):
        self._n, self._v, self._cb = n, visible, on_click

    def count(self):
        return self._n

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._v

    def click(self):
        if self._cb:
            self._cb()


class _Page:
    """Renders the overview until Configuration is clicked."""

    def __init__(self, has_tab=True, form_after=True):
        self.configured = False
        self.clicks = []
        self._has_tab, self._form_after = has_tab, form_after

    def locator(self, sel):
        if sel in g._CHAT_NAME_SEL:
            return _Loc(1 if (self.configured and self._form_after) else 0)
        if "Configuration" in sel:
            if not self._has_tab:
                return _Loc(0)
            return _Loc(1, on_click=lambda: (self.clicks.append(sel),
                                             setattr(self, "configured", True)))
        return _Loc(0)

    def wait_for_timeout(self, ms):
        pass


class TestItOpensTheTab:
    def test_it_clicks_configuration_then_finds_the_form(self):
        page = _Page()
        assert g._open_chat_configuration_tab(page) is True
        assert page.clicks, "the tab was never clicked"

    def test_a_form_already_open_needs_no_click(self):
        # A reload can land straight on it.
        page = _Page()
        page.configured = True
        assert g._open_chat_configuration_tab(page) is True
        assert not page.clicks

    def test_no_tab_at_all_is_reported_not_retried_forever(self):
        page = _Page(has_tab=False)
        assert g._open_chat_configuration_tab(page) is False

    def test_a_tab_that_reveals_nothing_is_a_failure(self):
        # Clicking succeeded but the form never rendered -- that is not
        # success, and returning True would push the confusion one step down.
        page = _Page(form_after=False)
        assert g._open_chat_configuration_tab(page) is False

    def test_it_tries_several_shapes(self):
        """Tab, link or left-nav item depending on rollout and viewport. A
        single selector is how this stopped working in the first place."""
        import inspect
        src = inspect.getsource(g._open_chat_configuration_tab)
        assert src.count('Configuration") ') + src.count('Configuration")') >= 3


class TestTheFormFillerUsesIt:
    def test_it_opens_the_tab_before_looking_for_the_field(self):
        import inspect
        src = inspect.getsource(g._fill_chat_app_form)
        assert "_open_chat_configuration_tab" in src
        # The field lookup is _chat_field now (label-first, for a Material
        # form); the ordering it guards is unchanged.
        assert src.index("_open_chat_configuration_tab") < src.index("name_box =")

    def test_the_failure_names_what_was_not_found(self):
        """It used to blame a missing Configuration tab. A saved page from a
        live run showed the tab was there all along, with the form behind an
        add-on checkbox -- so an operator trusting that message went and
        confirmed the API was enabled, which it was, and learned nothing.

        The message must name the thing actually absent (the app name field)
        and point at the evidence, not guess at a cause."""
        import inspect
        src = inspect.getsource(g._fill_chat_app_form)
        assert "app name field" in src
        assert "/tmp" in src, "must point at the saved screenshot and page text"

    def test_banners_are_dismissed_before_anything_is_clicked(self):
        """A screenshot settled several rounds of guessing: the console
        pins a cookie consent bar to the BOTTOM of the window, and Save is
        at the foot of the form. Every click was landing on that bar --
        which is why Playwright timed out on an actionable-looking button,
        why force=True appeared to work and changed nothing, and why no
        modal was ever found. There was no modal."""
        import inspect
        src = inspect.getsource(g._open_chat_configuration_tab)
        assert "_dismiss_console_banners" in src
        assert src.index("_dismiss_console_banners") < src.index(
            "_clear_workspace_addon_checkbox")

    def test_the_add_on_checkbox_is_cleared(self):
        """A Workspace add-on is not a Chat app: with the box ticked the
        form fills, Save clicks without error, and spaces.create still
        cannot resolve an app. It is irreversible, and deliberate on
        projects this tool creates and deletes per tenant."""
        import inspect
        src = inspect.getsource(g._open_chat_configuration_tab)
        # Superseded: a Workspace add-on is not a Chat app as far as
        # spaces.create is concerned, so the box has to be cleared whether
        # or not the fields happen to render behind it. What that earlier
        # reasoning actually observed was the cookie bar blocking clicks.
        assert "_clear_workspace_addon_checkbox" in src

    def test_clearing_it_is_announced_as_irreversible(self):
        """The console says the checkbox cannot be re-ticked. Automation
        that walks through a one-way door should say so in the transcript."""
        import inspect
        src = inspect.getsource(g._clear_workspace_addon_checkbox)
        assert "IRREVERSIBLE" in src


class TestTheConsoleOpenerIsNotHardcodedToDWD:
    """It reported the console unreachable while sitting on it.

    _open_dwd_console waited for the literal string "Add new" -- which
    belongs to the Domain-Wide Delegation page. A caller landing anywhere
    else waited the full timeout ON THE CORRECT PAGE and then reported
    failure, with DWD's manual instructions attached.

    Confirmed live against the Data Migration console: sign-in succeeded,
    the URL was right, and it still said "timed out waiting for the
    console."
    """

    def test_the_readiness_string_is_a_parameter(self):
        import inspect

        import dwd_helper
        sig = inspect.signature(dwd_helper._open_dwd_console)
        assert "ready_text" in sig.parameters

    def test_dwd_callers_are_unchanged(self):
        import inspect

        import dwd_helper
        sig = inspect.signature(dwd_helper._open_dwd_console)
        assert sig.parameters["ready_text"].default == "Add new"
        assert sig.parameters["url"].default == dwd_helper.DWD_URL

    def test_the_timeout_message_names_what_it_waited_for(self):
        """"timed out waiting for the console" gives the reader nothing.
        The page it was on and the string it wanted are the whole diagnosis."""
        import inspect

        import dwd_helper
        src = inspect.getsource(dwd_helper._open_dwd_console)
        assert "timed out waiting for {ready_text!r} on {page.url}" in src
