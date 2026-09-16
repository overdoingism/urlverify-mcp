# Configuration reference — `config.yaml`

Every field URLVerify_MCP reads, with type, default and meaning. The file is machine-specific and git-ignored;
start from `config.example.yaml`. Lookup order: `-c/--config`, `$URLVERIFY_CONFIG`, `./config.yaml`,
`<package dir>/../config.yaml`, `~/.urlverify_mcp/config.yaml`. Missing fields take the defaults below.
Most edits made in the admin UI apply to a running MCP server on its next call (see *Live vs restart*).

Environment overrides: `URLVERIFY_LLM_BASE_URL`, `URLVERIFY_LLM_API_KEY`, `URLVERIFY_LLM_MODEL`.

---

## `llm` — the investigator's own model (any OpenAI-compatible endpoint)

| Field | Type / default | Meaning |
|---|---|---|
| `base_url` | str · `http://127.0.0.1:8080/v1` | Chat-completions base URL. llama-server, LM Studio, vLLM, Ollama, OpenAI… |
| `api_key` | str · `not-needed` | Sent as bearer token. Local servers ignore it but the field must not be empty. |
| `model` | str · `default` | Model id sent in requests. llama-server ignores it; LM Studio / OpenAI require a real id. |
| `supports_tools` | `true` / `false` / `auto` · `auto` | Native tool calling. `auto` tries tools once and falls back to JSON-action mode if the backend rejects them. Set `false` for models known to emit broken tool calls. |
| `temperature` | float · `0.1` | Sampling temperature for investigation turns. Keep low; the rules engine needs consistent, verifiable output. |
| `max_iterations` | int · `24` | Maximum LLM turns per verification before the agent is forced to submit. Lowering it reduces review depth. |
| `timeout_s` | int · `300` | Per-request timeout for one LLM call. Sized for a 27B model on consumer hardware; raise it if single turns still time out. |

## `search` — how the agent searches and fetches pages

| Field | Type / default | Meaning |
|---|---|---|
| `provider` | `mcp` / `searxng_http` · `mcp` | `mcp` talks to an existing SearXNG MCP server (Streamable HTTP). `searxng_http` calls SearXNG's JSON API directly (no Docker needed; SearXNG must enable `search.formats: [html, json]`). |
| `call_timeout_s` | int · `45` | Hard limit for one search or fetch call, whichever provider. Bounds the wait if SearXNG hangs; the call then counts as "search unavailable" and the agent continues with structured APIs. |
| `mcp.url` | str · `http://127.0.0.1:3000/mcp` | Streamable HTTP endpoint of the SearXNG MCP server. |
| `mcp.search_tool` | str · `searxng_web_search` | Tool name used for web search on that server. |
| `mcp.fetch_tool` | str · `web_url_read` | Tool name used to fetch a page as text. |
| `searxng_http.base_url` | str · `http://127.0.0.1:8888` | SearXNG base URL for the HTTP provider. |

## `budget` — per-verification limits

| Field | Type / default | Meaning |
|---|---|---|
| `max_searches` | int · `8` | Web searches the agent may run. |
| `max_fetches` | int · `10` | Page fetches the agent may run (the target page fetched for injection screening does not count). |
| `max_api_calls` | int · `20` | Structured lookups (Wikidata, Wikipedia, Wayback, GitHub, Hugging Face, PyPI/npm). |
| `fetch_max_chars` | int · `12000` | Characters of a fetched page shown to the LLM (bodies are additionally capped at 2 MB on the wire). |
| `max_total_s` | int · `900` | Deadline for the whole verification. When exceeded the result is `UNVERIFIABLE` with the last stage in the reason. This is what makes the progress heartbeat safe: the heartbeat can never outlive this deadline. |

Callers may override `min_sources`, `allow_tier3`, `history_days` and the `identity` / `budget` sections per call through the tool's `options` argument; nothing else is overridable per call.

## `identity` — how "official" is established (L1)

| Field | Type / default | Meaning |
|---|---|---|
| `min_sources` | int · `2` | Independent tier-1/2 sources required to establish an official domain or org. Wikipedia + Wikidata count as one family. |
| `allow_tier3` | bool · `false` | Count forum / social / blog sources without age proof. Normally leave off and rely on aging (below). |
| `history_days` | int · `90` | Window for Wikipedia / Wikidata revision-history stability checks. |
| `min_stable_revisions` | int · `3` | Recent revisions that must agree on the official website for the value to count as stable. A value that changed within the window is demoted to tier 2 and cannot stand alone. |
| `new_domain_days` | int · `180` | A target domain whose first certificate (CT) is younger than this is flagged as a risk signal. |
| `github_token` | str · `""` | Optional GitHub token; raises the unauthenticated API limit (60/h). |
| `extra_tier1` / `extra_tier2` / `extra_tier3` | list[str] · `[]` | Additional source domains (eTLD+1 or full host) per tier, merged with the built-in lists in `identity/sources.py`. Config entries win over built-ins, so a built-in tier-2 site can be demoted here. Tier 1 can only be extended by the user, never by the LLM. |
| `tier3_min_age_days` | int · `365` | A tier-3 post proven older than this is promoted to tier 2. |
| `tier3_aged_max_count` | int · `1` | How many promoted tier-3 sources may count toward `min_sources` per verification. |
| `domain_age_contradiction_years` | int · `3` | "Post predates the domain" is only applied when the target domain's earliest evidence (CT or Wayback) is younger than this; CT coverage before 2018 is incomplete. |
| `aging_sources` | map · `{}` | Domain → dating method override: `reddit_api`, `hn_api`, `stackexchange_api`, `snowflake` (X post id), `discourse`, `github_api`, `jsonld` (weak, needs Wayback), `wayback`, `none` (login-walled, never promoted). Unlisted domains use `wayback`. |

## `net` — outbound HTTP

| Field | Type / default | Meaning |
|---|---|---|
| `timeout_s` | int · `15` | Timeout for L0 checks (TLS, DNS, redirects, CT) and the search MCP handshake. Structured APIs use `max(timeout_s, 30)`. |
| `user_agent` | str · `URLVerify-MCP/0.1 (+https://github.com/overdoingism/urlverify-mcp)` | Sent on every request. **Keep a contact URL or email**: Wikipedia's API returns 403 to user agents without one. |

## `cache`

| Field | Type / default | Meaning |
|---|---|---|
| `cert_ttl_hours` | int · `168` | How long a host's certificate facts are reused (never past the certificate's own expiry). |
| `identity_ttl_hours` | int · `720` | How long an independently established identity graph (official domains / orgs) is reused. Cached identities are trusted until they expire; invalidate them in the admin UI if a project moves. |
| `anchor_refresh_days` | int · `30` | Refresh interval for platform anchor data. |

## `storage`

| Field | Type / default | Meaning |
|---|---|---|
| `path` | str · `~/.urlverify_mcp/urlverify.sqlite3` | SQLite file holding caches, verification history and the identity graph. |

## `lists`

| Field | Type / default | Meaning |
|---|---|---|
| `allowlist` | list of `{project, domain}` · `[]` | Domains treated as official for a project (`project: "*"` = any). Bypasses L1 for that domain, not L0. |
| `denylist` | list[str] · `[]` | Hosts or eTLD+1 that are always `VERIFIED_FALSE`. |

## `injection_patterns`

List of case-insensitive regexes. A match in the **target page** means "text addressed to AI agents / verifiers" and yields `VERIFIED_FALSE`. Ordinary "this is the official site" wording is deliberately not matched (it carries zero weight instead).

## `registry_fast_path` — PyPI / npm shortcut

| Field | Type / default | Meaning |
|---|---|---|
| `enabled` | bool · `true` | Try the structured registry check before the LLM investigation for `pypi.org/project/<name>` and `npmjs.com/package/<name>` targets. |
| `mode` | `auto` / `quick` / `full` · `auto` | `auto`: fast path, then the full pipeline if inconclusive. `quick`: fast path only (inconclusive → `UNVERIFIABLE`). `full`: skip the fast path. Callers may override per call with `options.mode`. |
| `min_age_days` | int · `365` | The package's first release must be at least this old. |
| `min_releases` | int · `3` | Minimum number of releases. |
| `toplist_refresh_days` | int · `30` | The PyPI monthly top-5000 list is re-fetched at most this often (cached in `~/.urlverify_mcp/pypi_top.json`; a bundled snapshot is the fallback). |
| `confidence` | float · `0.8` | Confidence assigned to a fast-path `VERIFIED_TRUE`. |

Any signal that is unknown (API down, scoped npm package, no linked repository on an unpopular package) or suspicious
(a far more popular package one edit away) makes the fast path inconclusive and hands the case to the full pipeline with
the suspicion attached as a risk signal. The fast path can therefore only speed things up, never decide wrongly.

## `full_log` — full data log

| Field | Type / default | Meaning |
|---|---|---|
| `enabled` | bool · `false` | Record, in order, every MCP request/response, LLM turn (messages, response, reasoning), search exchange, structured API result, L0 result, aging result and rules decision as JSONL. Toggle in the admin Config tab; applies immediately. |
| `dir` | str · `~/.urlverify_mcp/logs` | Log directory. Each process writes its own `full-YYYYMMDDHHMMSS.log`. |
| `max_bytes` | int · `1048576` | Start a new file once the current one exceeds this size. |

## `prompts`

| Field | Type / default | Meaning |
|---|---|---|
| `dir` | str · `~/.urlverify_mcp/prompts` | Where edited prompts are stored (admin Prompts tab). Defaults live in `urlverify_mcp/prompt_defaults/`. `agent_*` prompts apply on the next verification; `mcp_*` texts are registered at server start. Agent prompts may use the optional tokens `{current_date}`, `{current_datetime}`, `{timezone}` (filled at run time, UTC); other braces are left untouched. |

## `server` — the MCP server (`urlverify-mcp serve`)

| Field | Type / default | Meaning |
|---|---|---|
| `transport` | `stdio` / `http` · `stdio` | `stdio` for hosts that launch the process; `http` exposes Streamable HTTP at `http://host:port/mcp`. CLI flags `--transport/--host/--port` override. |
| `host` / `port` | str · `127.0.0.1` / int · `8766` | Bind address for HTTP transport. No authentication: keep it on localhost or a trusted LAN. |
| `max_concurrent` | int · `1` | `verify_source` calls allowed to run at once; further callers wait (they see a "queued" progress message). Raise it for cloud LLMs or big hardware. |
| `auth_token` | str · `""` | Optional shared secret for the HTTP transport. When set, every request to `/mcp` must carry `Authorization: Bearer <token>`. Empty = no authentication (fine on 127.0.0.1). |
| `progress_events` | bool · `true` | Send an MCP progress notification at each pipeline step (L0, each LLM turn, each tool call, aging, rules, done). Clients that honour `resetTimeoutOnProgress` restart their request timer on each one. No-op when the client sent no progress token. |
| `heartbeat_s` | int · `15` | While a verification runs, also send a progress heartbeat every N seconds so a single long LLM turn cannot trip a short client timeout. `0` disables. Safe because every wait is bounded (`llm.timeout_s`, `search.call_timeout_s`, `net.timeout_s`) and the whole run by `budget.max_total_s`. |

## `admin` — the management UI (`urlverify-mcp admin`)

| Field | Type / default | Meaning |
|---|---|---|
| `host` / `port` | str · `127.0.0.1` / int · `8765` | Bind address. The UI can edit this file, open the log folder and run verifications: keep it local. |
| `auth_file` | str · `~/.urlverify_mcp/admin.auth` | JSON file with the PBKDF2-HMAC-SHA256 password hash, its random salt and the session-signing key. Created on first use with the default password `admin`; **delete it to reset the password** (all sessions are invalidated). |
| `session_days` | int · `7` | Lifetime of the login cookie. |

---

## Live vs restart

| Change | Takes effect |
|---|---|
| Anything under `llm`, `search`, `budget`, `identity`, `net`, `cache`, `lists`, `injection_patterns`, `full_log`, `registry_fast_path` | Next `verify_source` call (the server re-reads the file per call) |
| `agent_*` prompts | Next verification |
| `mcp_*` prompts, `server.*` (incl. `max_concurrent`, `auth_token`), `admin.*`, `storage.path`, `prompts.dir` | Restart the affected process |

## Timeout map (why nothing can hang forever)

```
client timeout  ──reset by──▶ progress events + heartbeat (server.progress_events / heartbeat_s)
                                   │ bounded by
verification    ──────────────▶ budget.max_total_s (900)
  ├─ each LLM call ────────────▶ llm.timeout_s (300)
  ├─ each search / fetch ──────▶ search.call_timeout_s (45)
  ├─ L0 checks, MCP handshake ─▶ net.timeout_s (15)
  └─ structured APIs, aging ───▶ max(net.timeout_s, 30), Wayback ≤ 5 retries
```
