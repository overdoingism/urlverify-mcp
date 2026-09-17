# URLVerify_MCP · Source-Verification MCP Server / 來源驗證 MCP Server

A small investigative agent exposed as an [MCP](https://modelcontextprotocol.io) server: given a download / installer / repo / model URL, it decides whether the source is **official**.
一個以 MCP server 形式提供的小型調查 agent：給定下載、安裝包、倉庫或模型網址，判斷來源是否**官方**。

Verdicts `VERIFIED_TRUE` · `VERIFIED_FALSE` · `UNVERIFIABLE`, each with a confidence score, a reason in the caller's language, and evidence whose quotes are verified against fetched content.
判定為三態，各附信心分數、跟隨呼叫方語言的結論，以及可逐字驗證的證據。

**Configuration reference:** every `config.yaml` field is explained in [document_for_config.md](document_for_config.md).
**設定說明**：所有 `config.yaml` 欄位的解釋見 [document_for_config.md](document_for_config.md)。

## How it works / 運作方式

1. **L0 — deterministic checks** (no LLM): TLS chain / SAN / Organization, DNS, redirect chain, punycode / homoglyph / typosquat / subdomain abuse, Certificate-Transparency first-seen, platform anchors, allow/deny lists, prompt-injection screening of the target page.
   確定性檢查（不經 LLM）：TLS、DNS、重導鏈、同形字/仿冒網域、CT 首見時間、平台錨點、黑白名單、目標頁注入篩檢。
2. **L1 — identity resolution** (LLM + tools): product → developer → aliases → official domains / orgs, backed by tiered independent third-party sources with temporal-stability checks.
   身分解析（LLM＋工具）：產品→開發者→別名→官方網域/組織，需多個分級獨立第三方來源佐證，並檢查時間穩定性。
3. **Rules engine**: verifies every quote, counts independent sources, matches the target against the established identity. The LLM proposes; the rules decide.
   規則引擎：驗證引文、計算獨立來源數、比對目標與官方身分——LLM 提議，規則裁決。

## Quick start / 快速開始

Requirements: **Python ≥ 3.11** (or [uv](https://docs.astral.sh/uv/), preferred) and an **OpenAI-compatible LLM endpoint** (llama-server, LM Studio, vLLM, Ollama, OpenAI…). Web search is optional but recommended: a **SearXNG** instance with its JSON API enabled (`search.formats: [html, json]`). Page fetching is built in; nothing else needs to be installed.

### Linux / macOS

| Step | Command | What it does |
|---|---|---|
| 1. Install | `./setup-venv.sh` | Creates `.venv` with uv (uses `uv.lock`) or falls back to `python -m venv` + pip. Re-run any time; the old `.venv` is backed up. |
| 2. Configure | `cp config.example.yaml config.yaml` | `config.yaml` is git-ignored and machine-specific. Edit at least the three endpoints below. |
| 3. Probe | `uv run urlverify-mcp check-env` | Confirms the LLM answers `/v1/models` and the search backend returns results. Fix these before going further. |
| 4. Run | see *Running* below | Admin UI, MCP server, or a one-shot verification. |

Without uv, replace `uv run urlverify-mcp` with `.venv/bin/urlverify-mcp` in every command.

### Windows

| Step | Command (PowerShell) | What it does |
|---|---|---|
| 1. Install | `.\setup-venv.ps1` | Same as the shell script: uv if present, otherwise venv + pip. A `.venv` copied from Linux is detected and moved aside. |
| 2. Configure | `Copy-Item config.example.yaml config.yaml` | Then edit the endpoints below. |
| 3. Probe | `uv run urlverify-mcp check-env` | Same probe. Without uv: `.venv\Scripts\urlverify-mcp check-env`. |
| 4. Run | see *Running* below | |

Windows notes:
- No SearXNG at all? Set `search.provider: none`: the investigator then relies on the structured sources only (Wikidata, Wikipedia, Wayback, GitHub, Hugging Face, registries). Well-known projects still verify; obscure ones come back `UNVERIFIABLE` more often.
- Keep console output UTF-8 (`chcp 65001`) so non-ASCII reasons render correctly. Logs are plain text (no ANSI colours) on every platform.
- In `config.yaml`, write Windows paths with forward slashes or in single quotes (`'C:\\tools\\logs'`); inside double quotes YAML treats `\` as an escape character.
- A launcher must **not** pass `--transport` unless it means to override `config.yaml`; CLI flags win over the config file.

### The three endpoints in `config.yaml`

```yaml
llm:
  base_url: "http://127.0.0.1:8080/v1"   # any OpenAI-compatible server
  model: "your-model-id"                  # some servers ignore this and serve whatever is loaded

search:
  provider: searxng_http                  # searxng_http (default) | mcp (SearXNG MCP server) | none
  searxng_http:
    base_url: "http://127.0.0.1:8888"     # SearXNG JSON API

fetch:
  provider: builtin                       # builtin (default, no dependencies) | mcp

server:
  transport: stdio                        # stdio | http  →  http://host:port/mcp
  port: 8766
admin:
  port: 8765
```

### Running

| Goal | Command | Notes |
|---|---|---|
| Admin UI | `uv run urlverify-mcp admin` | http://127.0.0.1:8765 — config editor, allow/deny lists, caches, history, manual test. |
| MCP server (stdio) | `uv run urlverify-mcp serve` | For hosts that spawn the process themselves (Claude Desktop, Claude Code, …). Nothing is printed; that is expected. |
| MCP server (HTTP) | `uv run urlverify-mcp serve --transport http` | Streamable HTTP at `http://127.0.0.1:8766/mcp` for URL-based hosts (LibreChat, …). Set `server.transport: http` to make it the default. Browsers show 400/406 on this URL; test with a POST. |

Long verifications and client timeouts: the server sends MCP progress notifications at every step plus a heartbeat
(`server.progress_events`, `server.heartbeat_s`), so clients that honour `resetTimeoutOnProgress` never hit `-32001`
however long a run takes; every wait is bounded and the whole run by `budget.max_total_s`. OpenCode honours progress
since its June 2026 fix (PR #32477); older builds time out regardless of what the server sends.
| One-shot check | `uv run urlverify-mcp verify "LM Studio" https://lmstudio.ai/download "Linux AppImage"` | Prints the full JSON result. Exit code 0 only for `VERIFIED_TRUE`. |

MCP host configuration:

```jsonc
// stdio — the host launches the server
{ "mcpServers": { "urlverify": {
    "command": "uv", "args": ["--directory", "/path/to/urlverify-mcp", "run", "urlverify-mcp", "serve"] } } }

// HTTP — the server is already running with --transport http
{ "mcpServers": { "urlverify": { "url": "http://127.0.0.1:8766/mcp" } } }
```

**Recommended system prompt for the calling agent.** The tool description explains *what* `verify_source` does; *when* an
agent must use it is policy, which belongs in the host's system prompt. Suggested wording:

> Regardless of the source, before downloading any executable, source code, library, or script, you MUST verify its URL using
> URLVerify_MCP. If the verification does not pass and no other usable source is available, you MUST report the issue to the
> user and ask for permission before proceeding. If URLVerify_MCP is unavailable, you MUST report this to the user and obtain
> permission before downloading or installing anything.

Tool exposed: `verify_source(project, url, description, options?)` → `{verdict, confidence, reason, evidence[], checks{}, identity{}, risk_signals[], trace_id}`.

### Full data log & editable prompts

| Feature | Where | Notes |
|---|---|---|
| **Full data log** | `full_log.enabled` in `config.yaml`, or the switch at the top of the admin **Config** tab | Off by default. Records, in order, every MCP request/response, every LLM turn (request messages, response, and the model's reasoning when the backend returns it), every search/fetch exchange, structured API results, L0 results and the rules decision, as one JSON record per line. Files are `full-YYYYMMDDHHMMSS.log` in `full_log.dir`; a new file starts once the current one exceeds `max_bytes` (1 MB). Browse them in the admin **Logs** tab. |
| **Agent prompts** | admin **Prompts** tab → `agent_*` | The investigator's system prompt, the submit_verdict schema text, the fallback JSON-action instructions and the final reason-writing prompt. Edits are saved as overrides in `prompts.dir` and take effect on the next verification. |
| **MCP-facing prompts** | admin **Prompts** tab → `mcp_*` | The server instructions and the three tool descriptions shown to the agent that calls this MCP server. Registered at startup, so restart `serve` after editing. |

Defaults ship in `urlverify_mcp/prompt_defaults/`; *Reset to default* deletes the override. Required placeholders (e.g. `{findings}` in the reason prompt) are validated on save.

### Registry fast path (PyPI / npm)

A package URL (`pypi.org/project/<name>`, `npmjs.com/package/<name>`) asks a narrower question than a website: *is this the
real package, published by the project it claims?* Registries answer the second half themselves: **build provenance** — npm's
Sigstore attestations and PyPI's PEP 740 provenance — is the registry's signed statement of *which repository's CI published
this version*. A name-squatter cannot forge one naming someone else's repository. `VERIFIED_TRUE` (confidence
`package_registry_fast_path.confidence`, `path: registry_fast_path`) requires all of:

- the package exists, its first release is older than `min_age_days`, and it has `min_releases` releases;
- **signed provenance** for the latest version, whose repository owner is a domain-verified GitHub organisation
  (deps.dev's independent verification of the same attestation is recorded when available);
- the registry metadata and that repository's manifest (`pyproject.toml` / `setup.cfg` / `setup.py`, `package.json`) agree
  on the package name (bidirectional link), and the repository is not a fork;
- for scoped npm packages (`@scope/name`) the scope equals the provenance repository's owner — the scope *is* the identity;
- for unscoped names, no far more popular package one edit away (typosquat check; popularity is only the denominator);
- the caller's project name matches the package or the owner.

Packages without provenance (most small or dormant ones) are not trusted on metadata alone: they go through the full
investigation, and without independent evidence come back `UNVERIFIABLE`. That is deliberate — a new or small package
has not earned trust, and the calling agent should ask the user. In the full investigation, provenance also lets a package
inherit the standing of an established GitHub organisation. `options.mode` = `auto` (default) | `quick` | `full`.

### Where things live

```
<project>/
  config.yaml            machine-specific settings (relative paths below resolve against this file's folder)
  state/                 rebuildable caches and settings: cert_cache.json, identity_cache.json, pypi_top.json,
                         admin.auth, prompts/ (edited prompts). Safe to copy to another machine or hand to someone.
  log/                   records of what this installation did: full/ (full data log), history/ (one JSON per
                         verification + index.jsonl), health.json (observed dependency health), server/ and admin/
                         (process output incl. "!! DEPENDENCY" lines, rotated). May be private; delete freely.
```
No database, no files outside the project folder. Both folders are git-ignored.

### Security notes

- **DNS spoofing and TLS interception.** The trusted-certificate requirement already defeats plain DNS poisoning (an
  attacker's server cannot present a valid certificate for the host). Two checks cover what remains: the leaf certificate
  must be publicly logged in Certificate Transparency (`net.ct_check`) — a locally installed interception CA never is — and
  the host is resolved again over DNS-over-HTTPS (`net.doh_cross_check`); when the answers differ, a handshake against the
  DoH address tells GeoDNS apart from a poisoned local resolver. No IP lists are bundled.
- **Non-public targets are refused.** Loopback, private, link-local and `.local`-style hosts, and redirects that land on
  them, fail L0 with `public_address` and never get probed. A verification server should not be usable for LAN reconnaissance.
- **Admin UI**: password-only login (default `admin`, change it in the UI). The hash (PBKDF2-HMAC-SHA256, random salt) and the
  session-signing key live in `admin.auth_file`; delete that file to reset. Sessions last `admin.session_days`. The UI can edit
  the config, open folders and run verifications: keep it on `127.0.0.1` and closed when not in use.
- **HTTP transport**: no authentication unless `server.auth_token` is set, in which case every request must carry
  `Authorization: Bearer <token>` (most MCP hosts accept custom headers per server). Default bind is `127.0.0.1`.
- **Concurrency**: `server.max_concurrent` (default 1) queues extra `verify_source` calls so a local LLM is never hit twice at once.

### Threat model & limitations

What it is good at: look-alike domains (homoglyph, typosquat, brand-in-label, subdomain abuse), non-official orgs on hosting
platforms (forks, community re-uploads, wrong GitHub / Hugging Face owner), non-existent or typosquatted packages, pages that
try to talk to AI agents, plain-HTTP or untrusted-certificate downloads.

What it does **not** do: it never downloads or inspects the file itself (no hash, signature or malware check); it does not
prove a site is *safe*, only that it is the project's own channel; `UNVERIFIABLE` means "not enough independent evidence",
not "dangerous". Results depend on the LLM you point it at and on third-party services (Wikipedia, Wikidata, archive.org,
GitHub, registries) being reachable and rate-limit friendly.

Known weak spots: very new projects and projects without a Wikipedia / Wikidata presence tend to come back `UNVERIFIABLE`
(a design choice: absence of independent evidence is not evidence); dynamic download pages may show a different OS's link
than the one described; tier-3 sources only count when their age can be proven, so a project known only from forums stays
unverifiable; Reddit dating uses the public RSS feed, which lacks the edit timestamp.

### Data flow (what leaves your machine)

Project name and URL go to the search engines behind your SearXNG, to Wikipedia / Wikidata, archive.org, GitHub, Hugging Face,
PyPI / npm, crt.sh, deps.dev, the DoH resolvers in `net.doh_resolvers` (host names only) and (for tier-3 dating) Reddit / HN /
Stack Exchange as needed. Fetched page text and the investigator's
messages go to the LLM endpoint you configured: with a local model nothing else leaves; with a cloud API the provider sees them.
Nothing is sent anywhere else, and no telemetry exists.

### Network footprint

The agent never renders pages: it requests the HTML/JSON document only, so images, CSS, scripts and fonts are never downloaded.
Other measures that keep traffic small and polite: redirect chains are walked with `HEAD` (a `GET` is streamed and closed
immediately if `HEAD` is refused); files under verification are never downloaded (binary content-types are reported, not read);
page bodies are capped at 2 MB and truncated to `budget.fetch_max_chars` before the LLM sees them; per-verification budgets
(`budget.max_searches` / `max_fetches` / `max_api_calls`) bound the number of requests; certificates and resolved identities are
cached (`cache.*`); Wikipedia / Wikidata / GitHub / Hugging Face / registries are queried through their JSON APIs instead of
scraping; Reddit requests are serialized; and `net.user_agent` identifies the tool with a contact URL, which Wikimedia requires.

## Tests / 測試

| Suite | Command | Needs | Time |
|---|---|---|---|
| Unit | `uv run pytest -q` | nothing (offline) | seconds |
| Pipeline (scripted LLM, real network) | included in `uv run pytest -q`; auto-skips when offline | network | ~1 min |
| Live regression | `uv run pytest tests/test_live.py --live -s` | LLM + search + network | 10–15 min |

`uv run urlverify-mcp check-env` (or the *Run check-env* button on the admin Test tab) sends the smallest request each
configured endpoint offers — LLM `/models`, SearXNG `/healthz`, Wikipedia `siteinfo`, Wayback availability API, GitHub
`/rate_limit`, PyPI simple-index `HEAD`, npm `/-/ping` — and is never run automatically. For a service that stays up for days
a probe is only a snapshot, so the real signal is the **observed dependency health** table (admin Test tab, `/api/health`):
every real call records its outcome, failures print a prominent `!! DEPENDENCY …` line on stderr and a
`dependency_failure` record in the full log, and each result lists the dependencies that failed during that run in
`degraded`. The table is a report, never a gate: networks flap, and the next call is always attempted. Results carry `schema_version` (currently 1); a breaking change to the
result shape bumps it.

Windows without uv: `.venv\Scripts\python -m pytest -q` (same flags).

The unit suite covers URL/homoglyph analysis, the rules engine (quote verification, source counting, tier promotion by age, temporal contradictions), injection detection, and the search provider's failure handling. The live suite runs the cases in `tests/fixtures/cases.yaml`; every check family has at least one true and one false case, and any change to prompts or rules should be validated against it.

## Docs & license / 文件與授權

- [AGENTS.md](AGENTS.md) — design charter: interface contract, source tiering, safety red lines (Traditional Chinese). / 設計憲章：介面契約、來源分級、安全紅線（繁體中文）。
- MIT License. See [LICENSE](LICENSE). / 採用 MIT 授權，見 [LICENSE](LICENSE)。
