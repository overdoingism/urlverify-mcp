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
