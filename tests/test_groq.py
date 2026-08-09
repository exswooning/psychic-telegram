"""
tests/test_groq.py
==================
The Groq "active log" panel: a place to store a Groq API key (case-preserved
-- it is an opaque secret, not a domain to lowercase) and a narrow endpoint
that sends the migration log tail + headline metrics to Groq and renders the
summary back.

Two contracts matter here:

  * the key must round-trip to env.sh verbatim. Groq keys are case-sensitive;
    `write_config` lowercases everything by design, so the key path uses
    `write_config_raw` instead;
  * the analysis endpoint must never execute anything the client sent -- it
    is a fixed POST body to Groq, and anything the browser types is only
    text passed to the LLM prompt.
"""

from __future__ import annotations

import json

import pytest

import webui


class TestGroqKey:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webui, "ENV_PATH", str(tmp_path / "env.sh"))
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

    def test_key_is_saved_case_preserved(self):
        """The key is an opaque secret. Lowercasing it -- as write_config does
        to every domain-style field -- would make it invalid at the API."""
        err = webui.save_groq_key("gsk_MiXeDcAsE_TokEn")
        assert err == ""
        text = open(webui.ENV_PATH).read()
        assert "GROQ_API_KEY=gsk_MiXeDcAsE_TokEn" in text

    def test_key_round_trips_verbatim(self):
        webui.save_groq_key("gsk_AbC123")
        assert webui.groq_api_key() == "gsk_AbC123"

    def test_whitespace_is_stripped_not_the_key(self):
        webui.save_groq_key("  gsk_Stripped  ")
        assert webui.groq_api_key() == "gsk_Stripped"

    def test_empty_key_is_rejected(self):
        err = webui.save_groq_key("   ")
        assert "required" in err

    def test_saving_preserves_unrelated_env_entries(self):
        webui.write_config_raw({"SOURCE_DOMAIN": "c.example.com",
                                "USER_WORKERS": "6"})
        webui.save_groq_key("gsk_x")
        text = open(webui.ENV_PATH).read()
        assert "SOURCE_DOMAIN=c.example.com" in text
        assert "USER_WORKERS=6" in text
        assert "GROQ_API_KEY=gsk_x" in text


class TestGroqAnalyze:
    def test_error_is_a_clean_message_not_a_500(self, monkeypatch):
        """A network or HTTP failure must come back as a diagnostic string the
        panel can render, never as an uncaught exception."""
        def boom(url, **kw):
            raise ConnectionError("name resolution failed")

        monkeypatch.setattr(webui.urllib.request, "urlopen", boom)
        text, err = webui._groq_analyze_log("x", "summarise", "gsk_1")
        assert text == ""
        assert "could not reach Groq" in err

    def test_http_error_detail_is_passed_through(self, monkeypatch):
        from urllib.error import HTTPError

        body = b'{"error": {"message": "invalid key"}}'

        def fake_error(url, **kw):
            err = HTTPError("u", 401, "Unauthorized", {}, None)
            err.read = lambda: body
            raise err

        monkeypatch.setattr(webui.urllib.request, "urlopen", fake_error)
        text, err = webui._groq_analyze_log("x", "summarise", "gsk_1")
        assert "401" in err and "invalid key" in err

    def test_response_content_is_returned(self, monkeypatch):
        payload = json.dumps(
            {"choices": [{"message": {"content": "**Status:** healthy"}}]}
        ).encode()

        class FakeResp:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(webui.urllib.request, "urlopen",
                            lambda url, **kw: FakeResp())
        text, err = webui._groq_analyze_log("log tail", "summarise", "gsk_1")
        assert err == ""
        assert "healthy" in text

    def test_prompt_and_tail_are_what_get_sent(self, monkeypatch):
        """The client's prompt and the log tail must both reach Groq -- the
        panel is useless if either is dropped silently."""
        sent = {}

        class FakeResp:
            def read(self):
                return b'{"choices": [{"message": {"content": "ok"}}]}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=90):
            sent["body"] = json.loads(req.data)
            sent["auth"] = req.headers.get("Authorization")
            return FakeResp()

        monkeypatch.setattr(webui.urllib.request, "urlopen", fake_urlopen)
        webui._groq_analyze_log("tail-line-1\ntail-line-2",
                                "focus on Drive errors", "gsk_secret")
        body = sent["body"]
        user = body["messages"][1]["content"]
        assert "focus on Drive errors" in user
        assert "tail-line-1" in user
        assert sent["auth"] == "Bearer gsk_secret"
        assert body["model"] == webui.GROQ_MODEL


class TestGroqPayloads:
    def test_run_summary_survives_missing_db(self, monkeypatch):
        """The dashboard numbers are a bonus to the prompt, never a
        requirement -- a fresh checkout with no migration.db yet must not make
        the panel fail."""
        monkeypatch.setattr(webui, "snapshot_payload",
                            lambda: {"error": "no database yet", "snapshot": None})
        out = webui._groq_run_summary()
        assert isinstance(out, str)
