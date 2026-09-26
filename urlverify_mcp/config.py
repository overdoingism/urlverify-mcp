"""Configuration loading: config.yaml (+ env overrides) -> pydantic model."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml")


class LLMConfig(BaseModel):
    enabled: bool = True                           # false = no-LLM mode: L0 + fixed lookups + rules only (AGENTS §6.3)
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


class FetchConfig(BaseModel):
    provider: Literal["builtin", "mcp"] = "builtin"   # builtin: httpx + dependency-free HTML->text; mcp: SearXNG MCP's fetch tool


class SearchConfig(BaseModel):
    provider: Literal["searxng_http", "mcp", "none"] = "searxng_http"   # none: no web search, structured APIs only
    call_timeout_s: int = 45              # hard limit for one search / fetch call (MCP call_tool read timeout or HTTP timeout)
    mcp: MCPSearchConfig = MCPSearchConfig()
    searxng_http: SearxngHTTPConfig = SearxngHTTPConfig()


class BudgetConfig(BaseModel):
    max_searches: int = 8
    max_fetches: int = 10
    max_api_calls: int = 20
    fetch_max_chars: int = 12000
    max_total_s: int = 900                # whole-verification deadline; past it the result is UNVERIFIABLE


class WikimediaSoloConfig(BaseModel):
    """An old, watched Wikimedia record may establish an official domain on its own (AGENTS §4.1). Lowest-confidence path."""
    enabled: bool = True
    min_monthly_views: int = 2000             # every one of the last 24 months (human traffic, en.wikipedia article)
    sample_window_months: list[int] = Field(default_factory=lambda: [18, 30])   # random past revision taken from this window
    max_confidence: float = 0.6               # TRUE reached through this path is capped here (WIKIMEDIA_ONLY)


class IdentityConfig(BaseModel):
    min_sources: int = 2
    allow_tier3: bool = False
    history_days: int = 90
    min_stable_revisions: int = 3
    new_domain_days: int = 180
    github_token: str = ""
    homebrew_reverse_lookup: bool = True       # websites: look up Homebrew casks that download from the target domain
    homebrew_index_refresh_days: int = 7       # the ~2 MB cask catalogue is re-checked (ETag) at most this often
    wikimedia_solo: WikimediaSoloConfig = WikimediaSoloConfig()
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
    ct_check: bool = True                      # leaf must show Certificate Transparency compliance (embedded SCTs; see known_public_cas)
    known_public_cas: list[str] = Field(default_factory=lambda: [
        "Let's Encrypt", "ISRG", "Google Trust Services", "DigiCert", "Sectigo", "Comodo", "USERTrust", "GlobalSign", "Amazon",
        "Cloudflare", "GoDaddy", "Starfield", "Entrust", "IdenTrust", "Microsoft", "Apple", "Buypass", "ZeroSSL", "SSL.com",
        "Actalis", "Certum", "HARICA", "SwissSign", "Telia", "QuoVadis", "Baltimore", "Thawte", "GeoTrust", "RapidSSL",
        "Trustwave", "SECOM", "TWCA", "Chunghwa", "Certigna", "D-TRUST", "T-Systems", "e-commerce monitoring", "Izenpe", "WoSign"])
    doh_cross_check: bool = True               # resolve the host again over DNS-over-HTTPS and compare with the system resolver
    doh_resolvers: list[str] = Field(default_factory=lambda: ["https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"])
    user_agent: str = "URLVerify-MCP/0.1 (+https://github.com/overdoingism/urlverify-mcp)"   # Wikimedia UA policy: must carry a contact URL/email
    retries: int = 2                           # retries for 429 / 502 / 503 / 504 from third-party services (crt.sh, Wikimedia, archive.org ...)
    retry_backoff_s: float = 3.0               # first retry wait; doubles each time; a Retry-After header is honoured up to 10 s
    ct_first_seen_cache_days: int = 90         # CT first-seen dates never change: reuse a found date this long (a "no certificate" answer for 1 day)


class CacheConfig(BaseModel):
    cert_ttl_hours: int = 168
    identity_ttl_hours: int = 720
    anchor_refresh_days: int = 30


_BASE_DIR: Path = Path.cwd()          # directory of the loaded config.yaml; relative paths resolve against it


def resolve_path(p: str) -> Path:
    q = Path(os.path.expanduser(p))
    return q if q.is_absolute() else (_BASE_DIR / q)


class StorageConfig(BaseModel):
    dir: str = "state"                 # plain JSON files: caches, history, health, auth, prompt overrides

    def resolved(self) -> Path:
        d = resolve_path(self.dir)
        d.mkdir(parents=True, exist_ok=True)
        return d


class LogConfig(BaseModel):
    dir: str = "log"                   # everything log-like lives here; delete the whole folder any time
    process_max_bytes: int = 1_048_576 # rotation for log/server/*.log and log/admin/*.log
    process_backups: int = 5

    def resolved(self) -> Path:
        d = resolve_path(self.dir)
        d.mkdir(parents=True, exist_ok=True)
        return d


class AllowEntry(BaseModel):
    project: str = "*"
    domain: str


class ListsConfig(BaseModel):
    allowlist: list[AllowEntry] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)


class PromptsConfig(BaseModel):
    dir: str = "state/prompts"        # user overrides; defaults ship inside the package


class ServerConfig(BaseModel):
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8766          # Streamable HTTP endpoint: http://host:port/mcp
    max_concurrent: int = 1        # verify_source calls running at once; extra callers queue (protects a local LLM)
    auth_token: str = ""           # optional shared secret for the HTTP transport: clients must send "Authorization: Bearer <token>"
    progress_events: bool = True   # send MCP progress notifications at each pipeline step (clients that honour them reset their timeout)
    heartbeat_s: int = 15          # additionally send a progress heartbeat every N seconds while a verification runs; 0 disables


class AdminConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    auth_file: str = "state/admin.auth"   # PBKDF2 hash + salt + session key; delete it to reset the password to "admin"
    session_days: int = 7


class PackageRegistryFastPathConfig(BaseModel):
    """PyPI / npm shortcut: structured registry checks first, LLM only when inconclusive."""
    enabled: bool = True
    mode: Literal["auto", "quick", "full"] = "auto"   # auto: fast path, fall back to full; quick: fast path only; full: skip fast path
    min_age_days: int = 365        # package must have existed this long
    min_releases: int = 3
    confidence: float = 0.8
    require_project_match: bool = True   # the caller's project name must match the package or repository name
    typosquat_check: bool = True         # compare against popular near-names; false = rely on the repository link only
    toplist_size: int = 1500             # rows kept from the PyPI popularity list (~60 bytes each); streamed, rest not downloaded
    toplist_refresh_days: int = 60       # conditional (ETag) re-fetch at most this often; first download on first PyPI target
    toplist_url: str = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"


class ReleaseCooldownConfig(BaseModel):
    hours: float = Field(default=72, ge=0, allow_inf_nan=False)


class SourceConfig(BaseModel):
    """How the `source` argument is interpreted (AGENTS.md §13). Registries: the default used when the command does not
    name one. A non-public registry here is reported as REGISTRY_UNSUPPORTED (mirrors cannot be verified), never
    silently replaced by the public one."""
    registries: dict[str, str] = {"pypi": "https://pypi.org/simple", "npm": "https://registry.npmjs.org",
                                  "nuget": "https://api.nuget.org/v3/index.json"}
    max_subjects: int = 8
    # apt: extra official archive mirrors (host, or host/path prefix) besides *.debian.org / *.ubuntu.com, e.g.
    # "free.nchc.org.tw/ubuntu". APT verifies the distribution's signatures on any mirror; this only says the mirror
    # serves your distribution rather than a third-party repository.
    distro_archives: list[str] = []


class Config(BaseModel):
    llm: LLMConfig = LLMConfig()
    search: SearchConfig = SearchConfig()
    fetch: FetchConfig = FetchConfig()
    budget: BudgetConfig = BudgetConfig()
    identity: IdentityConfig = IdentityConfig()
    net: NetConfig = NetConfig()
    cache: CacheConfig = CacheConfig()
    storage: StorageConfig = StorageConfig()
    log: LogConfig = LogConfig()
    lists: ListsConfig = ListsConfig()
    injection_patterns: list[str] = Field(default_factory=lambda: [
        r"\b(ai|llm|language model|assistant|agent|verifier|claude|gpt|chatgpt|copilot)\b[^.\n]{0,80}\b(this (site|website|page|domain) is|we are|trust|official|legitimate|safe|verified)",
        r"\b(ignore|disregard) (all |any )?(previous|prior|above) (instructions|rules|prompts)",
        r"\bto (any|all) (ai|llm|language models?|assistants?|agents?|bots?)\b",
        r"\b(system|developer) (prompt|instruction|override)\b",
        r"\bplease (report|respond|answer|mark|classify)[^.\n]{0,60}\b(verified|official|true|legitimate|safe)\b",
    ])
    source: SourceConfig = SourceConfig()
    package_registry_fast_path: PackageRegistryFastPathConfig = PackageRegistryFastPathConfig()
    release_cooldown: ReleaseCooldownConfig = ReleaseCooldownConfig()
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
        if options.get("mode") in ("quick", "full", "auto"):
            data["package_registry_fast_path"]["mode"] = options["mode"]
        if options.get("mode") == "fast":
            data["llm"]["enabled"] = False      # this call only: stop after the fixed lookups (a caller may only reduce, never add)
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
    global _BASE_DIR
    path = find_config_path(explicit)
    _BASE_DIR = path.parent.resolve() if path else Path.cwd()
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
    target = path or cfg.source_path or (_BASE_DIR / "config.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(mode="json"), f, allow_unicode=True, sort_keys=False)
    cfg.source_path = target
    return target
