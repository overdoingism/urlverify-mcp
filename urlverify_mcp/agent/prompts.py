"""Prompt accessors. Texts live in prompt_defaults/*.md and may be overridden via the admin UI (see promptstore)."""
from __future__ import annotations

from ..promptstore import get_store


def system_prompt() -> str:
    return get_store().render("agent_system")


def fallback_action_instructions() -> str:
    return get_store().render("agent_fallback_actions")


def submission_schema_text() -> str:
    return get_store().render("agent_submission_schema")


def reason_prompt() -> str:
    return get_store().render("agent_reason")
