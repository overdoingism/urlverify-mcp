import pytest

from urlverify_mcp.promptstore import PROMPTS, PromptStore


def test_defaults_exist_and_placeholders_present():
    s = PromptStore("/nonexistent/override/dir")
    for name, meta in PROMPTS.items():
        text = s.get(name)
        assert text.strip(), name
        assert not s.validate(name, text), (name, s.validate(name, text))


def test_override_roundtrip_and_reset(tmp_path):
    s = PromptStore(tmp_path)
    assert not s.is_overridden("agent_system")
    s.set("agent_system", "You are a very careful verifier.\n")
    assert s.is_overridden("agent_system")
    assert s.get("agent_system") == "You are a very careful verifier.\n"
    assert s.default("agent_system") != s.get("agent_system")
    # editing the file directly is picked up (mtime check)
    (tmp_path / "agent_system.md").write_text("edited on disk\n", encoding="utf-8")
    import os, time
    os.utime(tmp_path / "agent_system.md", (time.time() + 5, time.time() + 5))
    assert s.get("agent_system") == "edited on disk\n"
    s.reset("agent_system")
    assert not s.is_overridden("agent_system") and s.get("agent_system") == s.default("agent_system")


def test_validation_rejects_missing_placeholders_and_empty(tmp_path):
    s = PromptStore(tmp_path)
    with pytest.raises(ValueError):
        s.set("agent_reason", "no placeholders here")
    with pytest.raises(ValueError):
        s.set("agent_system", "   ")
    with pytest.raises(KeyError):
        s.get("nope")


def test_agent_prompt_accessors_use_store(tmp_path, monkeypatch):
    import urlverify_mcp.promptstore as ps
    from urlverify_mcp.agent import prompts
    monkeypatch.setattr(ps, "_store", None)
    ps.get_store(str(tmp_path)).set("agent_system", "CUSTOM SYSTEM\n")
    assert prompts.system_prompt() == "CUSTOM SYSTEM\n"
    assert "{tool_list}" in prompts.fallback_action_instructions()


def test_optional_tokens_render_and_leave_json_intact(tmp_path):
    from urlverify_mcp.promptstore import render_tokens
    text = 'Date {current_date} tz {timezone}; schema {"a": {"b": 1}}; keep {findings} and {confidence:.2f}'
    out = render_tokens(text, {"current_date": "2026-09-16", "current_datetime": "x", "timezone": "UTC"})
    assert out == 'Date 2026-09-16 tz UTC; schema {"a": {"b": 1}}; keep {findings} and {confidence:.2f}'
    s = PromptStore(tmp_path)
    rendered = s.render("agent_system")
    assert "{current_date}" not in rendered and "Current date: 20" in rendered
    # reason prompt still formats after rendering (required placeholders survive)
    r = s.render("agent_reason").format(project="p", url="u", description="d", verdict="V", confidence=0.5, findings="f", narrative="n")
    assert "Current date: 20" in r and "{" not in r.split("Current date")[0]
    # tokens are optional: removing them from an override must not fail validation
    s.set("agent_system", "no tokens at all\n")
    assert s.render("agent_system") == "no tokens at all\n"
