"""Editable prompts. Defaults ship in urlverify_mcp/prompt_defaults/*.md; user overrides live in
config.prompts.dir (default ~/.urlverify_mcp/prompts). Overrides are re-read on every access (mtime-checked),
so agent prompts take effect on the next verification without a restart. MCP-facing texts (instructions and
tool descriptions) are registered when the server starts and therefore need a restart."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULTS_DIR = Path(__file__).parent / "prompt_defaults"


OPTIONAL_TOKENS = ("current_date", "current_datetime", "timezone")
_TOKEN_RE = re.compile(r"\{(" + "|".join(OPTIONAL_TOKENS) + r")\}")


def runtime_values() -> dict[str, str]:
    """Values for the optional {current_date} / {current_datetime} / {timezone} tokens (UTC, matching age_days maths)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return {"current_date": now.strftime("%Y-%m-%d"), "current_datetime": now.strftime("%Y-%m-%d %H:%M UTC"), "timezone": "UTC"}


def render_tokens(text: str, values: dict[str, str] | None = None) -> str:
    """Replace only the known optional tokens; every other brace (JSON examples, required placeholders) is left intact.
    Never use str.format here: the prompts contain JSON."""
    vals = values or runtime_values()
    return _TOKEN_RE.sub(lambda m: vals[m.group(1)], text)




@dataclass(frozen=True)
class PromptMeta:
    name: str
    title: str
    audience: str          # "agent" (URLVerify's own LLM) | "mcp" (the outer agent calling the MCP server)
    live: bool             # True = picked up on next verification; False = needs server restart
    placeholders: tuple[str, ...] = ()   # {names} that must survive an edit


PROMPTS: dict[str, PromptMeta] = {
    "agent_system": PromptMeta("agent_system", "Investigator system prompt", "agent", True),
    "agent_fallback_actions": PromptMeta("agent_fallback_actions", "Fallback JSON-action instructions (models without tool calling)", "agent", True, ("tool_list",)),
    "agent_submission_schema": PromptMeta("agent_submission_schema", "submit_verdict schema text", "agent", True),
    "agent_reason": PromptMeta("agent_reason", "Final reason-writing prompt", "agent", True,
                               ("project", "url", "description", "verdict", "confidence", "findings", "narrative")),
    "mcp_instructions": PromptMeta("mcp_instructions", "MCP server instructions (shown to the calling agent)", "mcp", False),
    "mcp_tool_verify_source": PromptMeta("mcp_tool_verify_source", "Tool description: verify_source", "mcp", False),
    "mcp_tool_get_verification": PromptMeta("mcp_tool_get_verification", "Tool description: get_verification", "mcp", False),
    "mcp_tool_list_known_identities": PromptMeta("mcp_tool_list_known_identities", "Tool description: list_known_identities", "mcp", False),
}


class PromptStore:
    def __init__(self, override_dir: str | os.PathLike | None = None):
        self.override_dir = Path(os.path.expanduser(str(override_dir or "~/.urlverify_mcp/prompts")))
        self._cache: dict[str, tuple[float, str]] = {}

    # ---- paths
    def default_path(self, name: str) -> Path:
        return DEFAULTS_DIR / f"{name}.md"

    def override_path(self, name: str) -> Path:
        return self.override_dir / f"{name}.md"

    # ---- read
    def default(self, name: str) -> str:
        return self.default_path(name).read_text(encoding="utf-8")

    def get(self, name: str) -> str:
        if name not in PROMPTS:
            raise KeyError(name)
        p = self.override_path(name)
        if p.is_file():
            mtime = p.stat().st_mtime
            cached = self._cache.get(name)
            if cached and cached[0] == mtime:
                return cached[1]
            text = p.read_text(encoding="utf-8")
            self._cache[name] = (mtime, text)
            return text
        self._cache.pop(name, None)
        return self.default(name)

    def render(self, name: str) -> str:
        """Effective text with the optional runtime tokens filled in. Required placeholders (e.g. {findings}) stay."""
        return render_tokens(self.get(name))

    def is_overridden(self, name: str) -> bool:
        return self.override_path(name).is_file()

    # ---- write
    def validate(self, name: str, text: str) -> list[str]:
        meta = PROMPTS[name]
        problems = []
        if not text.strip():
            problems.append("prompt is empty")
        for ph in meta.placeholders:
            if not re.search(r"\{" + re.escape(ph) + r"(?::[^{}]*)?\}", text):   # {name} or {name:.2f}
                problems.append(f"missing required placeholder {{{ph}}}")
        return problems

    def set(self, name: str, text: str) -> Path:
        problems = self.validate(name, text)
        if problems:
            raise ValueError("; ".join(problems))
        self.override_dir.mkdir(parents=True, exist_ok=True)
        p = self.override_path(name)
        p.write_text(text, encoding="utf-8")
        self._cache.pop(name, None)
        return p

    def reset(self, name: str) -> None:
        p = self.override_path(name)
        if p.is_file():
            p.unlink()
        self._cache.pop(name, None)

    def listing(self) -> list[dict]:
        out = []
        for name, meta in PROMPTS.items():
            out.append({"name": name, "title": meta.title, "audience": meta.audience, "live": meta.live,
                        "placeholders": list(meta.placeholders), "optional_tokens": list(OPTIONAL_TOKENS),
                        "overridden": self.is_overridden(name),
                        "override_path": str(self.override_path(name))})
        return out


_store: PromptStore | None = None


def get_store(override_dir: str | None = None) -> PromptStore:
    global _store
    if _store is None or (override_dir and Path(os.path.expanduser(override_dir)) != _store.override_dir):
        _store = PromptStore(override_dir)
    return _store
