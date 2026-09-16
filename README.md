# URLVerify_MCP · Source-Verification MCP Server / 來源驗證 MCP Server

A small investigative agent exposed as an [MCP](https://modelcontextprotocol.io) server: given a download / installer / repo / model URL, it decides whether the source is **official**.
一個以 MCP server 形式提供的小型調查 agent：給定下載、安裝包、倉庫或模型網址，判斷來源是否**官方**。

Verdicts `VERIFIED_TRUE` · `VERIFIED_FALSE` · `UNVERIFIABLE`, each with a confidence score, a reason in the caller's language, and evidence whose quotes are verified against fetched content.
判定為三態，各附信心分數、跟隨呼叫方語言的結論，以及可逐字驗證的證據。

## How it works / 運作方式

1. **L0 — deterministic checks** (no LLM): TLS chain / SAN / Organization, DNS, redirect chain, punycode / homoglyph / typosquat / subdomain abuse, Certificate-Transparency first-seen, platform anchors, allow/deny lists, prompt-injection screening of the target page.
   確定性檢查（不經 LLM）：TLS、DNS、重導鏈、同形字/仿冒網域、CT 首見時間、平台錨點、黑白名單、目標頁注入篩檢。
2. **L1 — identity resolution** (LLM + tools): product → developer → aliases → official domains / orgs, backed by tiered independent third-party sources with temporal-stability checks.
   身分解析（LLM＋工具）：產品→開發者→別名→官方網域/組織，需多個分級獨立第三方來源佐證，並檢查時間穩定性。
3. **Rules engine**: verifies every quote, counts independent sources, matches the target against the established identity. The LLM proposes; the rules decide.
   規則引擎：驗證引文、計算獨立來源數、比對目標與官方身分——LLM 提議，規則裁決。

## Quick start / 快速開始

Requirements: **Python ≥ 3.11** (or [uv](https://docs.astral.sh/uv/), preferred), an **OpenAI-compatible LLM endpoint** (llama-server, LM Studio, vLLM, Ollama, OpenAI…), and a **SearXNG** instance reachable either through the [mcp-searxng](https://github.com/ihor-sokoliuk/mcp-searxng) MCP server or its plain JSON API.

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
- No Docker? Skip the MCP search server and point the HTTP fallback at a local SearXNG: `search.provider: searxng_http`, `search.searxng_http.base_url: http://127.0.0.1:8888` (SearXNG must have `search.formats: [html, json]`).
- Keep console output UTF-8 (`chcp 65001`) so non-ASCII reasons render correctly. Logs are plain text (no ANSI colours) on every platform.
- In `config.yaml`, write Windows paths with forward slashes or in single quotes (`'C:\\tools\\logs'`); inside double quotes YAML treats `\` as an escape character.
- A launcher must **not** pass `--transport` unless it means to override `config.yaml`; CLI flags win over the config file.

### The three endpoints in `config.yaml`

```yaml
llm:
  base_url: "http://127.0.0.1:8080/v1"   # any OpenAI-compatible server
  model: "your-model-id"                  # some servers ignore this and serve whatever is loaded

search:
  provider: mcp                           # mcp (default) | searxng_http (fallback)
  mcp:
    url: "http://127.0.0.1:3000/mcp"      # mcp-searxng, Streamable HTTP
  searxng_http:
    base_url: "http://127.0.0.1:8888"     # SearXNG JSON API

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
| One-shot check | `uv run urlverify-mcp verify "LM Studio" https://lmstudio.ai/download "Linux AppImage"` | Prints the full JSON result. Exit code 0 only for `VERIFIED_TRUE`. |

MCP host configuration:

```jsonc
// stdio — the host launches the server
{ "mcpServers": { "urlverify": {
    "command": "uv", "args": ["--directory", "/path/to/urlverify-mcp", "run", "urlverify-mcp", "serve"] } } }

// HTTP — the server is already running with --transport http
{ "mcpServers": { "urlverify": { "url": "http://127.0.0.1:8766/mcp" } } }
```

Tool exposed: `verify_source(project, url, description, options?)` → `{verdict, confidence, reason, evidence[], checks{}, identity{}, risk_signals[], trace_id}`.

### Full data log & editable prompts

| Feature | Where | Notes |
|---|---|---|
| **Full data log** | `full_log.enabled` in `config.yaml`, or the switch at the top of the admin **Config** tab | Off by default. Records, in order, every MCP request/response, every LLM turn (request messages, response, and the model's reasoning when the backend returns it), every search/fetch exchange, structured API results, L0 results and the rules decision, as one JSON record per line. Files are `full-YYYYMMDDHHMMSS.log` in `full_log.dir`; a new file starts once the current one exceeds `max_bytes` (1 MB). Browse them in the admin **Logs** tab. |
| **Agent prompts** | admin **Prompts** tab → `agent_*` | The investigator's system prompt, the submit_verdict schema text, the fallback JSON-action instructions and the final reason-writing prompt. Edits are saved as overrides in `prompts.dir` and take effect on the next verification. |
| **MCP-facing prompts** | admin **Prompts** tab → `mcp_*` | The server instructions and the three tool descriptions shown to the agent that calls this MCP server. Registered at startup, so restart `serve` after editing. |

Defaults ship in `urlverify_mcp/prompt_defaults/`; *Reset to default* deletes the override. Required placeholders (e.g. `{findings}` in the reason prompt) are validated on save.

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

Windows without uv: `.venv\Scripts\python -m pytest -q` (same flags).

The unit suite covers URL/homoglyph analysis, the rules engine (quote verification, source counting, tier promotion by age, temporal contradictions), injection detection, and the search provider's failure handling. The live suite runs the cases in `tests/fixtures/cases.yaml`; every check family has at least one true and one false case, and any change to prompts or rules should be validated against it.

## Docs & license / 文件與授權

- [AGENTS.md](AGENTS.md) — design charter: interface contract, source tiering, safety red lines (Traditional Chinese). / 設計憲章：介面契約、來源分級、安全紅線（繁體中文）。
- MIT License. See [LICENSE](LICENSE). / 採用 MIT 授權，見 [LICENSE](LICENSE)。
