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
2. **L1 — identity resolution**: first fixed lookups with no LLM (Wikidata / Wikipedia for the project, package and
   developer names; platform records of the owners involved). If the rules can already decide, the LLM is not started;
   otherwise the LLM works only on the missing identity edges (aliases, renames, product ↔ company). Structured results
   are numbered facts the LLM cites by id; web pages need verbatim quotes.
   身分解析：先由程式做固定查詢（Wikidata／Wikipedia、相關 owner 的平台紀錄），規則已能裁決就不啟動 LLM；否則 LLM 只針對缺少的身分邊調查。
   結構化結果以編號 facts 引用，網頁需逐字引文。
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


One-shot check from the command line (prints the same YAML an MCP host receives; `--json` for the internal JSON; exit
code 0 only for `VERIFIED_TRUE`):

```bash
uv run urlverify-mcp verify "LM Studio" "https://lmstudio.ai/download" --artifact "Linux AppImage"
uv run urlverify-mcp verify requests "pip install requests" --artifact "Python package" --description "HTTP client"
```

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

> Regardless of the source, before downloading any toolchain, executable, source code, library, or script, you MUST verify its URL using
> URLVerify_MCP. If the verdict is not VERIFIED_TRUE, or the result carries any additional conditions, you MUST report the issue to the
> user and ask for permission before proceeding. If URLVerify_MCP is unavailable, you MUST report this to the user and obtain
> permission before downloading or installing anything.
> Package installs (pip, npm, NuGet, winget, docker, git clone, curl | sh …) download and run code too: pass the exact command
> you intend to run as `source`, and follow `machine_readable.next_action` in the reply.

### The `verify_source` tool (v0.2)

```
verify_source(project, source, artifact="", description="", version="", options=None) -> YAML text
```

| Argument | Meaning |
|---|---|
| `project` | Product name only (`LM Studio`, `Vulkan SDK`, `requests`). The installed package name may differ; that alias is L1's job. |
| `source` | Exactly what will be used: a URL, or ONE install / download command. Never a bare name. |
| `artifact` | The form: `Windows x64 installer`, `Python package`, `Docker image`, `install script`. |
| `description` | What it is for: `LLM front-end`, `HTTP client library`. Give `artifact` or `description`, preferably both. |
| `version` | Optional exact version; empty = the registry's default. Must not contradict a pin inside `source`. |

What `source` understands (flags are white-listed per tool: an unknown flag is reported, never ignored, because it may
change what gets installed; a bare name is rejected because it does not say which registry):

| Verified | Commands |
|---|---|
| URL | `https://…` |
| PyPI | `pip`/`pip3`/`python -m pip`/`py -m pip install`, `uv pip install`, `uv add`, `uv tool install`, `uvx`, `pipx install/run`, `poetry add`, `pdm add`, `pipenv install` |
| npm | `npm i/install/add`, `yarn add`, `pnpm add`, `bun add`, `npx`, `npm exec`, `pnpm dlx`, `bunx` (aliases `x@npm:y`, `user/repo`, `github:` resolved to what really installs) |
| NuGet | `dotnet add package`, `dotnet package add`, `dotnet tool install`, `nuget install`, `Install-Package` |
| WinGet | `winget install --id <Id>` / `-e <Id>`: the installer URL is taken from Microsoft's winget-pkgs manifest, then verified |
| git | `git clone https://…`, `git@github.com:o/r`, `ssh://…` on known hosts, `gh repo clone o/r` |
| Hugging Face | `hf download`, `huggingface-cli download` |
| Containers | `docker/podman/nerdctl pull|run` for Docker Hub and ghcr.io |
| Homebrew | `brew install [--cask|--formula] <name>` (official taps only): the cask's download URL, or the upstream source a formula is built from, taken from formulae.brew.sh |
| Scoop | `scoop install [bucket/]<app>` for the official ScoopInstaller buckets (main, extras, versions, java, nonportable); no bucket = main |
| Go | `go install` / `go get <module>@<version>`: github.com / gitlab.com / codeberg.org / bitbucket.org paths map to the repository; other domains are verified as the module's own domain (its go-import tag is recorded) |
| Scripts | `curl … \| sh`, `sh -c "$(curl …)"`, `irm … \| iex`, `iex ((New-Object Net.WebClient).DownloadString(…))`: the script URL is verified; what the script downloads next is not (reported) |

Understood but not verified yet (reported as `ECOSYSTEM_NOT_YET_VERIFIED`): cargo, gem, composer, choco,
conda/mamba, apt/dnf/yum/pacman/zypper/apk, snap, flatpak, ollama, Install-Module, other container registries.
Always rejected: requirement / lock files, local paths, several indexes at once (`--extra-index-url`, `--find-links`),
chained commands (`&&`, `;`). Registry: a flag in the command wins, then `source.registries` in config, then the public
registry; a non-public registry is reported (`REGISTRY_UNSUPPORTED`), never silently replaced by the public one.

The reply is YAML. `machine_readable` comes first and is the only part to act on:

```yaml
machine_readable:
  schema_version: 2
  verdict: VERIFIED_TRUE            # VERIFIED_TRUE | VERIFIED_FALSE | UNVERIFIABLE (worst subject wins)
  next_action: PROCEED              # PROCEED | INFORM_USER_AND_CONFIRM | DO_NOT_PROCEED | FIX_INPUT_AND_RETRY
  confidence: 0.9
  codes: []                         # fixed reason codes (why not TRUE / what blocked it)
  notices: []                       # e.g. EXECUTES_ON_INSTALL, SCRIPT_MAY_DOWNLOAD_MORE, RELEASE_COOLDOWN_ACTIVE
  trace_id: 3f2a…
  subjects:                         # one per package / URL: ecosystem, package, version, registry, verified_url, checks …
summary: |                          # fixed sentences built by the rules, in the caller's language
explanation:                        # the LLM's prose per subject
details:                            # evidence, checks, identity, engine notes
```

Everything after `machine_readable` may quote untrusted web pages; verdict words inside it are neutralised
(`VERIFIED TRUE`), so a grep for `verdict: VERIFIED_TRUE` or `next_action: PROCEED` only ever hits the real keys.

### Full data log & editable prompts

| Feature | Where | Notes |
|---|---|---|
| **Full data log** | `full_log.enabled` in `config.yaml`, or the switch at the top of the admin **Config** tab | Off by default. Records, in order, every MCP request/response, every LLM turn (request messages, response, and the model's reasoning when the backend returns it), every search/fetch exchange, structured API results, L0 results and the rules decision, as one JSON record per line. Files are `full-YYYYMMDDHHMMSS.log` in `full_log.dir`; a new file starts once the current one exceeds `max_bytes` (1 MB). Browse them in the admin **Logs** tab. |
| **Agent prompts** | admin **Prompts** tab → `agent_*` | The investigator's system prompt, the submit_verdict schema text, the fallback JSON-action instructions and the final reason-writing prompt. Edits are saved as overrides in `prompts.dir` and take effect on the next verification. |
| **MCP-facing prompts** | admin **Prompts** tab → `mcp_*` | The server instructions and the three tool descriptions shown to the agent that calls this MCP server. Registered at startup, so restart `serve` after editing. |

Defaults ship in `urlverify_mcp/prompt_defaults/`; *Reset to default* deletes the override. Required placeholders (e.g. `{findings}` in the reason prompt) are validated on save.

### 套件版本冷卻期（npm／PyPI／NuGet）

`release_cooldown.hours` 設定發布後觀察期，預設 **72 小時**，支援小數，**0 停用**。可直接改 config.yaml，或在管理頁 Config 編輯；下次驗證生效。

結果放在 `checks.release_cooldown`；版本仍在期間內或無法判定時，reason 與 risk_signals 都會提醒。這是獨立的時間資訊，**不改官方來源 verdict/confidence，也不表示平台仍在掃描或保證套件安全**。quick／full 與身分快取命中均適用，L0 致命失敗則略過。

期間內的訊號格式為 `RELEASE_COOLDOWN_PERIOD:<已發布小時數>`：例如 `RELEASE_COOLDOWN_PERIOD:36` 表示目前已發布約 36 小時、仍未滿足冷卻期，**不是剩餘 36 小時**。數值最多保留六位小數並省略尾端零，例如 `:36.5`；呼叫端應以 `RELEASE_COOLDOWN_PERIOD:` 前綴識別，不再比對舊的無數值字串。門檻與剩餘時間見 `checks.release_cooldown.detail`。

收到此訊號時，即使 verdict 是 `VERIFIED_TRUE`，呼叫端也必須向使用者說明風險並取得確認，才可繼續下載或安裝。時間不明時仍使用 `release_cooldown_unknown`，不能當成已過冷卻期，也須先告知並取得確認。工具提供提示，實際確認由呼叫端執行。

請優先提供指定版本的套件頁或可辨識下載網址；未指定版本時會列出解析到的最新版本與查詢時間。冷卻只涵蓋該版本／檔案，不包含其依賴。NuGet 仍需 L1 身分佐證，沒有新增快速通關。完整 URL 與時間判定規則見 `document_for_config.md`。

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
- for PyPI, no far more popular package one edit away on the popularity list (popularity is only the denominator). npm has no
  such reference, and guessing "likely typos" is guesswork (nobody sees their own typos), so npm relies on provenance alone;
- the caller's project name matches the package or the owner.

A name the registry itself has disowned — an npm *security holding package* (`0.0.1-security`, the name of a removed malicious
package) — is `VERIFIED_FALSE` outright; a PyPI release whose files are all yanked is a risk signal. Packages without
provenance (most small or dormant ones) are not trusted on metadata alone: they go through the full
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

Self-published projects (a personal GitHub / Hugging Face account with no website, no package, no Wikipedia entry) can
only be checked for consistency: the owner must appear as the same identity on at least two different platforms (all
domains of one platform count as one source; other users' READMEs, issues and discussions on the same platform are user
content and never a second vote). Such a `VERIFIED_TRUE` is capped at confidence 0.75 and says so in `engine_notes`; a
project known from one platform only comes back `UNVERIFIABLE` by design.

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

`uv run urlverify-mcp check-env` (or the *Run check-env* button on the admin Status tab) probes only the endpoints you
configured — LLM `/models`, SearXNG `/healthz` (or the MCP handshake), the fetcher — and is never run automatically.
Public third-party services are not probed at all: for a service that stays up for days a probe is only a snapshot, so
their state is the **observed dependency health** table (admin Status tab, `/api/health`):
every real call records its outcome, failures print a prominent `!! DEPENDENCY …` line on stderr and a
`dependency_failure` record in the full log, and each result lists the dependencies that failed during that run in
`degraded`. The table is a report, never a gate: networks flap, and the next call is always attempted. Results carry `schema_version` (currently 2: the v0.2 `verify_source` YAML / JSON with `subjects`); a breaking change
to the result shape bumps it. History entries written before v0.2 are schema 1 and are still readable.

Windows without uv: `.venv\Scripts\python -m pytest -q` (same flags).

The unit suite covers URL/homoglyph analysis, the rules engine (quote verification, source counting, tier promotion by age, temporal contradictions), injection detection, and the search provider's failure handling. The live suite runs the cases in `tests/fixtures/cases.yaml`; every check family has at least one true and one false case, and any change to prompts or rules should be validated against it.

## Docs & license / 文件與授權

- [AGENTS.md](AGENTS.md) — design charter: interface contract, source tiering, safety red lines (Traditional Chinese). / 設計憲章：介面契約、來源分級、安全紅線（繁體中文）。
- MIT License. See [LICENSE](LICENSE). / 採用 MIT 授權，見 [LICENSE](LICENSE)。
