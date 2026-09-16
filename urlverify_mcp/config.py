"""Configuration loading: config.yaml (+ env overrides) -> pydantic model."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml")


class LLMConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str = "not-needed"
    model: str = "default"
    supports_tools: bool | Literal["auto"] = "auto"
    temperature: float = 0.1
    max_iterations: int = 24
    timeout_s: int = 300


class MCPSearchConfig(BaseModel):
    url: str = "http://127.0.0.1:3000/mcp"
    search_tool: str = "searxng_web_search"
    fetch_tool: str = "web_url_read"


class SearxngHTTPConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8888"


class SearchConfig(BaseModel):
    provider: Literal["mcp", "searxng_http"] = "mcp"
    call_timeout_s: int = 45              # hard limit for one search / fetch call (MCP call_tool read timeout or HTTP timeout)
    mcp: MCPSearchConfig = MCPSearchConfig()
    searxng_http: SearxngHTTPConfig = SearxngHTTPConfig()


class BudgetConfig(BaseModel):
    max_searches: int = 8
    max_fetches: int = 10
    max_api_calls: int = 20
    fetch_max_chars: int = 12000
    max_total_s: int = 900                # whole-verification deadline; past it the result is UNVERIFIABLE


class IdentityConfig(BaseModel):
    min_sources: int = 2
    allow_tier3: bool = False
    history_days: int = 90
    min_stable_revisions: int = 3
    new_domain_days: int = 180
    github_token: str = ""
    # additive source tier lists (eTLD+1 or full host). Built-in lists live in identity/sources.py.
    extra_tier1: list[str] = Field(default_factory=list)
    extra_tier2: list[str] = Field(default_factory=list)
    extra_tier3: list[str] = Field(default_factory=list)
    # temporal provenance for tier-3 sources (forums / social): a post proven older than this is promoted to tier 2
    tier3_min_age_days: int = 365
    tier3_aged_max_count: int = 1
    aging_sources: dict[str, str] = Field(default_factory=dict)   # domain -> method override (see identity/aging.py)
    domain_age_contradiction_years: int = 3   # only apply "post predates domain" when the domain's first evidence is this recent


class NetConfig(BaseModel):
    timeout_s: int = 15
    user_agent: str = "URLVerify-MCP/0.1 (+https://github.com/overdoingism/urlverify-mcp)"   # Wikimedia UA policy: must carry a contact URL/email


class CacheConfig(BaseModel):
    cert_ttl_hours: int = 168
    identity_ttl_hours: int = 720
    anchor_refresh_days: int = 30


class StorageConfig(BaseModel):
    path: str = "~/.urlverify_mcp/urlverify.sqlite3"

    def resolved(self) -> Path:
        p = Path(os.path.expanduser(self.path))
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


class AllowEntry(BaseModel):
    project: str = "*"
    domain: str


class ListsConfig(BaseModel):
    allowlist: list[AllowEntry] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)


class FullLogConfig(BaseModel):
    enabled: bool = False
    dir: str = "~/.urlverify_mcp/logs"
    max_bytes: int = 1_048_576          # rotate to a new full-YYYYMMDDHHMMSS.log beyond this size


class PromptsConfig(BaseModel):
    dir: str = "~/.urlverify_mcp/prompts"   # user overrides; defaults ship inside the package


class ServerConfig(BaseModel):
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8766          # Streamable HTTP endpoint: http://host:port/mcp
    progress_events: bool = True   # send MCP progress notifications at each pipeline step (clients that honour them reset their timeout)
    heartbeat_s: int = 15          # additionally send a progress heartbeat every N seconds while a verification runs; 0 disables


class AdminConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class Config(BaseModel):
    llm: LLMConfig = LLMConfig()
    search: SearchConfig = SearchConfig()
    budget: BudgetConfig = BudgetConfig()
    identity: IdentityConfig = IdentityConfig()
    net: NetConfig = NetConfig()
    cache: CacheConfig = CacheConfig()
    storage: StorageConfig = StorageConfig()
    lists: ListsConfig = ListsConfig()
    injection_patterns: list[str] = Field(default_factory=lambda: [
        r"\b(ai|llm|language model|assistant|agent|verifier|claude|gpt|chatgpt|copilot)\b[^.\n]{0,80}\b(this (site|website|page|domain) is|we are|trust|official|legitimate|safe|verified)",
        r"\b(ignore|disregard) (all |any )?(previous|prior|above) (instructions|rules|prompts)",
        r"\bto (any|all) (ai|llm|language models?|assistants?|agents?|bots?)\b",
        r"\b(system|developer) (prompt|instruction|override)\b",
        r"\bplease (report|respond|answer|mark|classify)[^.\n]{0,60}\b(verified|official|true|legitimate|safe)\b",
    ])
    full_log: FullLogConfig = FullLogConfig()
    prompts: PromptsConfig = PromptsConfig()
    server: ServerConfig = ServerConfig()
    admin: AdminConfig = AdminConfig()

    # where it was loaded from (None = defaults only)
    source_path: Path | None = Field(default=None, exclude=True)

    def with_overrides(self, options: dict[str, Any] | None) -> "Config":
        """Apply per-call overrides. Only identity.*, budget.*, and lists.* may be overridden by callers."""
        if not options:
            return self
        data = self.model_dump()
        for section in ("identity", "budget"):
            if isinstance(options.get(section), dict):
                data[section].update(options[section])
        # flat convenience keys
        for k in ("min_sources", "allow_tier3", "history_days"):
            if k in options:
                data["identity"][k] = options[k]
        cfg = Config.model_validate(data)
        cfg.source_path = self.source_path
        return cfg


def find_config_path(explicit: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("URLVERIFY_CONFIG")
    if env:
        candidates.append(Path(env))
    for name in DEFAULT_CONFIG_NAMES:
        candidates.append(Path.cwd() / name)
    candidates.append(Path(__file__).resolve().parent.parent / "config.yaml")
    candidates.append(Path(os.path.expanduser("~/.urlverify_mcp/config.yaml")))
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(explicit: str | None = None) -> Config:
    path = find_config_path(explicit)
    if path is None:
        cfg = Config()
    else:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg = Config.model_validate(raw)
        cfg.source_path = path
    # env overrides for the two things people most often need to change
    if os.environ.get("URLVERIFY_LLM_BASE_URL"):
        cfg.llm.base_url = os.environ["URLVERIFY_LLM_BASE_URL"]
    if os.environ.get("URLVERIFY_LLM_API_KEY"):
        cfg.llm.api_key = os.environ["URLVERIFY_LLM_API_KEY"]
    if os.environ.get("URLVERIFY_LLM_MODEL"):
        cfg.llm.model = os.environ["URLVERIFY_LLM_MODEL"]
    return cfg


def save_config(cfg: Config, path: Path | None = None) -> Path:
    target = path or cfg.source_path or Path(os.path.expanduser("~/.urlverify_mcp/config.yaml"))
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(mode="json"), f, allow_unicode=True, sort_keys=False)
    cfg.source_path = target
    return target
