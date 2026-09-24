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
| `enabled` | bool · `true` | `false` = **no-LLM mode**: L0, the fixed lookups (Wikimedia, platform records, manifests, registries) and the rules only. Cases the fixed lookups can decide get their normal verdict; the rest come back `UNVERIFIABLE` with the missing edges and the notice `NO_LLM_MODE`. No LLM endpoint is needed. A single call can ask for the same with `options.mode: "fast"`. |
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
| `provider` | `searxng_http` / `mcp` / `none` · `searxng_http` | `searxng_http` calls SearXNG's JSON API directly (SearXNG must enable `search.formats: [html, json]`). `mcp` talks to a SearXNG MCP server (Streamable HTTP) instead. `none` disables web search: the investigator uses only the structured sources; well-known projects still verify, obscure ones fall to `UNVERIFIABLE` more often. |
| `call_timeout_s` | int · `45` | Hard limit for one search or fetch call, whichever provider. Bounds the wait if SearXNG hangs; the call then counts as "search unavailable" and the agent continues with structured APIs. |
| `mcp.url` | str · `http://127.0.0.1:3000/mcp` | Streamable HTTP endpoint of the SearXNG MCP server (only with `provider: mcp` or `fetch.provider: mcp`). |
| `mcp.search_tool` | str · `searxng_web_search` | Tool name used for web search on that server. |
| `mcp.fetch_tool` | str · `web_url_read` | Tool name used to fetch a page as text. |
| `searxng_http.base_url` | str · `http://127.0.0.1:8888` | SearXNG base URL for the HTTP provider. |

## `release_cooldown` — 套件版本冷卻期

| 欄位 | 型別／預設 | 說明 |
|---|---|---|
| `hours` | 非負有限數值 · `72` | npm／PyPI／NuGet 的發布後觀察期，單位小時，可用小數。`0` 停用且不發出冷卻期 API 查詢。管理頁 Config 修改後，下次驗證生效。此設定不接受呼叫端 options 覆寫。 |

結果放在 `checks.release_cooldown.detail`：`state` 為 `disabled`、`active`、`elapsed`、`unknown`；可判定時含 `registry`、`package`、`version`、`source`、`published_at`、`checked_at`、`threshold_hours`、`age_hours`、`remaining_hours` 與 `resolution`。期間內與不明狀態會附加 `risk_signals` 及 reason 提醒，不改來源 verdict/confidence，不代表掃描中／掃描通過，也不涵蓋間接依賴。L0 致命失敗時不執行；quick、full 與身分快取命中均適用。

`risk_signals` 的期間內訊號為 `RELEASE_COOLDOWN_PERIOD:<age_hours>`，例如 `RELEASE_COOLDOWN_PERIOD:36` 表示已發布約 36 小時，仍未滿足門檻；數字不是剩餘時間。最多六位小數、移除尾端零（如 `:36.5`），與 `detail.age_hours` 相同精度；精確狀態應讀取 `detail.state`，不可用顯示數字自行推定已過期。呼叫端以 `RELEASE_COOLDOWN_PERIOD:` 前綴識別；此格式取代舊的無數值訊號。

期間內與時間不明的 reason 均明確要求：繼續下載／安裝前必須向使用者說明風險並取得確認，來源驗證通過不代表內容安全。時間不明仍使用 `release_cooldown_unknown`；停用或已過期不產生冷卻期提示。工具不代替呼叫端執行使用者確認。

支援 npm 套件頁（含 scoped、`/v/<version>`）、registry metadata 與 tarball URL；PyPI 套件／版本頁、JSON API、可辨識的 wheel／sdist URL；NuGet 套件／版本頁、v2 package 與 v3 flat-container 下載 URL。未指定版本時，npm 解析 latest 標籤、PyPI 解析 API 最新版，NuGet 選版本排序最高的 stable 候選（若 unlisted 則回 unknown，請指定版本）；皆記錄解析後版本。無法辨識的登錄平台 URL 回 unknown；其他網站不套用。

npm 使用 `time[version]`，不用 metadata 的 `modified`。PyPI 下載檔須與 metadata URL 完全相符；版本頁使用該版本所有檔案中最新上傳時間，以涵蓋後補 wheel。NuGet 使用 registration leaf 的 `published`；1900 年占位值、未來／無時區日期、缺失資料與網路失敗均回 unknown。NuGet CDN 檔名不能可靠拆出 ID 時亦回 unknown，請改用帶版本的套件頁。

冷卻期只提供觀察資訊，不替代既有套件首發年齡 `package_registry_fast_path.min_age_days`。NuGet 必走 L1；新增冷卻查詢不會產生獨立身分票數或新增快速通關。

## `fetch` — how pages are turned into text

| Field | Type / default | Meaning |
|---|---|---|
| `provider` | `builtin` / `mcp` · `builtin` | `builtin`: httpx GET with a dependency-free HTML→text converter that keeps the title, headings, list bullets and link targets (`text (url)`), drops scripts/styles, never downloads binaries, caps bodies at 2 MB. `mcp`: the SearXNG MCP server's `web_url_read` (HTML→markdown, PDF text); if it fails the built-in fetcher is tried for the target page. |

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
| `min_stable_revisions` | int · `3` | Recent revisions that must agree on the official website (and, separately, the official source repository) for the value to count as stable. A value that changed within the window is demoted to tier 2 and cannot stand alone. |
| `new_domain_days` | int · `180` | A target domain whose first certificate (CT) is younger than this is flagged as a risk signal. |
| `homebrew_reverse_lookup` | bool · `true` | For website targets, a fixed lookup (no LLM) finds Homebrew casks whose download URL is on the target domain and whose token / name equals the project or whose homepage is on the same domain; the cask record becomes evidence (brew.sh family). Uses the cask catalogue below. |
| `homebrew_index_refresh_days` | int · `7` | Homebrew's cask catalogue (formulae.brew.sh/api/cask.json, ~2 MB compressed) is downloaded on first use, reduced to a small domain index in `state/brew_cask_index.json`, and re-checked with ETag at most this often (an unchanged catalogue costs one 304). |
| `github_token` | str · `""` | Optional. Without it GitHub allows 60 API requests per hour per IP; GitHub-heavy verifications then run out and come back `UNVERIFIABLE` (never a wrong `TRUE`). A token with **no permissions** raises the limit to 5000/hour: create a fine-grained token at https://github.com/settings/personal-access-tokens/new (Repository access: *Public repositories*, no permissions, any expiry) or a classic one with no scopes at https://github.com/settings/tokens/new. Set it in the admin Config tab (stored in config.yaml, masked in the UI, sent only to api.github.com) or here. |
| `extra_tier1` / `extra_tier2` / `extra_tier3` | list[str] · `[]` | Additional sources per tier, merged with the built-in lists in `identity/sources.py`. An entry is a domain (eTLD+1 or full host) or, when it contains a path, a `host/path` prefix such as `github.com/myorg/manifests/` (case-insensitive; a scheme or `www.` is ignored). Path prefixes are checked before domains, so a manifest repository on `github.com` can be tier 1 while `github.com` itself stays tier 2. The built-in tier-1 prefixes ship in `urlverify_mcp/data/tier1_paths.yaml` (shown in the admin Caches tab, editable with a local editor, re-read when it changes). Config entries win over built-ins, so a built-in tier-2 site can be demoted here. Tier 1 can only be extended by the user, never by the LLM. |
| `tier3_min_age_days` | int · `365` | A tier-3 post proven older than this is promoted to tier 2. |
| `tier3_aged_max_count` | int · `1` | How many promoted tier-3 sources may count toward `min_sources` per verification. |
| `domain_age_contradiction_years` | int · `3` | "Post predates the domain" is only applied when the target domain's earliest evidence (CT or Wayback) is younger than this; CT coverage before 2018 is incomplete. |
| `aging_sources` | map · `{}` | Domain → dating method override: `reddit_api`, `hn_api`, `stackexchange_api`, `snowflake` (X post id), `discourse`, `github_api` (issue / PR / discussion creation, or repository creation for repo pages, READMEs and raw files), `huggingface_api` (model / dataset / space creation), `jsonld` (weak, needs Wayback), `wayback`, `none` (login-walled, never promoted). Unlisted domains use `wayback`. |

## `net` — outbound HTTP

| Field | Type / default | Meaning |
|---|---|---|
| `timeout_s` | int · `15` | Timeout for L0 checks (TLS, DNS, redirects, CT) and the search MCP handshake. Structured APIs use `max(timeout_s, 30)`. |
| `ct_check` | bool · `true` | Certificate Transparency compliance of the leaf. Embedded SCTs → pass. No SCTs but a well-known public CA (`known_public_cas`) → warn only (SCTs may travel in the TLS handshake, which Python cannot see). No SCTs, unknown issuer, and unknown to crt.sh (older than 24 h) → `VERIFIED_FALSE`: the signature of a locally installed interception CA. |
| `known_public_cas` | list · major public CAs | Issuer names treated as public CAs for the rule above. Add your own if a legitimate CA you use is missing. |
| `doh_cross_check` | bool · `true` | Resolve the host again over DNS-over-HTTPS and compare with the system resolver. Differing answers are not an error by themselves (GeoDNS); when they differ, a TLS handshake against the DoH address decides: system path invalid + DoH path valid → `VERIFIED_FALSE` (local DNS spoofing suspected). Sends the host name to the resolvers; disable if that matters. |
| `doh_resolvers` | list · Cloudflare, Google | DoH JSON endpoints tried in order. |
| `user_agent` | str · `URLVerify-MCP/0.1 (+https://github.com/overdoingism/urlverify-mcp)` | Sent on every request. **Keep a contact URL or email**: Wikipedia's API returns 403 to user agents without one. |
| `retries` | int · `2` | Retries for "try again later" answers (429, 502, 503, 504) from third-party services: crt.sh, Wikidata, Wikipedia, archive.org, GitHub. Network errors and other statuses are not retried. |
| `retry_backoff_s` | float · `3.0` | Wait before the first retry; doubles each time. A `Retry-After` header is honoured up to 10 s. |
| `ct_first_seen_cache_days` | int · `90` | Certificate-Transparency first-seen dates never change, so a found date is kept in `state/kv.json` and crt.sh (a volunteer service that often answers 502 for popular domains) is asked again only after this many days. A "no certificate yet" answer is kept for 1 day. `0` disables the cache. |

## `cache`

| Field | Type / default | Meaning |
|---|---|---|
| `cert_ttl_hours` | int · `168` | How long a host's certificate facts are reused (never past the certificate's own expiry). |
| `identity_ttl_hours` | int · `720` | How long an independently established identity graph (official domains / orgs) is reused. Cached identities are trusted until they expire; invalidate them in the admin UI if a project moves. |
| `anchor_refresh_days` | int · `30` | Refresh interval for platform anchor data. |

## `storage`

| Field | Type / default | Meaning |
|---|---|---|
| `dir` | str · `state` | Directory of plain JSON files: `cert_cache.json`, `identity_cache.json`, `anchor_cache.json`, `pypi_top.json`, `admin.auth`, `prompts/` — rebuildable caches and settings only. Relative paths resolve against the folder of `config.yaml`. Delete a file to reset that part. |

## `log`

| Field | Type / default | Meaning |
|---|---|---|
| `dir` | str · `log` | Root of everything log-like: `full/` (full data log), `history/` (one JSON per verification + `index.jsonl`), `health.json` (observed dependency health), `server/server.log`, `admin/admin.log`. Records of what this installation did, possibly private: safe to delete at any time. |
| `process_max_bytes` / `process_backups` | int · `1048576` / `5` | Rotation of the server / admin process logs. |

## `lists`

| Field | Type / default | Meaning |
|---|---|---|
| `allowlist` | list of `{project, domain}` · `[]` | Domains treated as official for a project (`project: "*"` = any). Bypasses L1 for that domain, not L0. |
| `denylist` | list[str] · `[]` | Hosts or eTLD+1 that are always `VERIFIED_FALSE`. |

## `injection_patterns`

List of case-insensitive regexes. A match in the **target page** means "text addressed to AI agents / verifiers" and yields `VERIFIED_FALSE`. Ordinary "this is the official site" wording is deliberately not matched (it carries zero weight instead).

## `source` — how `verify_source(source=…)` is interpreted

| Field | Type · default | Meaning |
|---|---|---|
| `registries` | map · pypi `https://pypi.org/simple`, npm `https://registry.npmjs.org`, nuget `https://api.nuget.org/v3/index.json` | Registry used when the command names none. A flag in the command (`-i`, `--registry`, `-s`/`-Source`) or `PIP_INDEX_URL` / `NPM_CONFIG_REGISTRY` in front of it wins. A non-public registry (mirror, private feed) is reported as `REGISTRY_UNSUPPORTED:<host>` and not verified; it is never replaced by the public registry. |
| `max_subjects` | int · `8` | Most packages accepted in one command (`pip install a b c …`); more gives `TOO_MANY_SUBJECTS`. Each package is verified in turn, each with its own `budget.max_total_s`. |

## `package_registry_fast_path` — PyPI / npm shortcut

| Field | Type / default | Meaning |
|---|---|---|
| `enabled` | bool · `true` | Try the structured registry check before the LLM investigation for `pypi.org/project/<name>` and `npmjs.com/package/<name>` targets. `false` = always run the full investigation. |
| `mode` | `auto` / `quick` / `full` · `auto` | `auto`: fast path, then the full pipeline if inconclusive. `quick`: fast path only (inconclusive → `UNVERIFIABLE`). `full`: skip the fast path. Callers may override per call with `options.mode`. |
| `min_age_days` | int · `365` | The package's first release must be at least this old. |
| `min_releases` | int · `3` | Minimum number of releases. |
| `confidence` | float · `0.8` | Confidence assigned to a fast-path `VERIFIED_TRUE`. |
| `require_project_match` | bool · `true` | The caller's `project` must match the package name (normalised, substring either way). A mismatch (asked for "requests", given `reqests-utils`) sends the case to the full pipeline with a `project_package_mismatch` risk signal. |
| `typosquat_check` | bool · `true` | PyPI only: compare the name against far more popular near-names on the popularity list. npm has no popularity reference and is not compared (provenance gates TRUE there). |
| `toplist_size` | int · `1500` | Rows kept from the PyPI popularity list, ~60 bytes each (1500 ≈ 90 KB). The file is sorted by downloads and streamed: the connection is closed after N rows, the rest is never downloaded. Nothing is bundled with the package. |
| `toplist_refresh_days` | int · `60` | The cached list (`state/pypi_top.json`) is re-validated at most this often, with `If-None-Match`; an unchanged list costs a 304 and no body. The first download happens on the first PyPI target, never at install or start-up. |
| `toplist_url` | str · hugovk top-pypi-packages | Source of the list (JSON rows `{download_count, project}` sorted descending). |

`VERIFIED_TRUE` needs existence + age + release count + project-name match + **signed build provenance** (npm attestation /
PyPI PEP 740) whose repository owner is a domain-verified GitHub organisation + a bidirectional repository link + (unscoped
npm / PyPI) a clean typosquat check or (scoped npm) scope == provenance owner. Popularity is only the denominator of the
typosquat ratio. Packages without provenance are never trusted on metadata alone; they run the full investigation. Any
unknown or suspicious signal makes the fast path inconclusive and hands the case to the full pipeline with the suspicion
attached as a risk signal. The fast path can therefore only speed things up, never decide wrongly.

## `full_log` — full data log

| Field | Type / default | Meaning |
|---|---|---|
| `enabled` | bool · `false` | Record, in order, every MCP request/response, LLM turn (messages, response, reasoning), search exchange, structured API result, L0 result, aging result and rules decision as JSONL. Toggle in the admin Config tab; applies immediately. |
| `dir` | str · `log/full` | Full-log directory (relative to the config folder). Each process writes its own `full-YYYYMMDDHHMMSS.log`. |
| `max_bytes` | int · `1048576` | Start a new file once the current one exceeds this size. |

## `prompts`

| Field | Type / default | Meaning |
|---|---|---|
| `dir` | str · `state/prompts` | Where edited prompts are stored (admin Prompts tab). Defaults live in `urlverify_mcp/prompt_defaults/`. `agent_*` prompts apply on the next verification; `mcp_*` texts are registered at server start. Agent prompts may use the optional tokens `{current_date}`, `{current_datetime}`, `{timezone}` (filled at run time, UTC); other braces are left untouched. |

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
| `auth_file` | str · `state/admin.auth` | JSON file with the PBKDF2-HMAC-SHA256 password hash, its random salt and the session-signing key. Created on first use with the default password `admin`; **delete it to reset the password** (all sessions are invalidated). |
| `session_days` | int · `7` | Lifetime of the login cookie. |

---

## Live vs restart

| Change | Takes effect |
|---|---|
| Anything under `llm`, `search`, `fetch`, `budget`, `identity`, `net`, `cache`, `lists`, `injection_patterns`, `full_log`, `package_registry_fast_path` | Next `verify_source` call (the server re-reads the file per call) |
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

## 審視修正後的執行行為（2026-09-19）

- `cache.identity_ttl_hours` 是原始成立時間起算的有效期限；命中不會續期。`identity` 證據政策改變後重新調查；舊格式快取不再採用。
- 本機網頁／公開 API 抓取檢查所有 DNS 位址並固定連線 IP；這類連線不採用環境變數 HTTP 代理，避免代理繞過位址檢查。設定的 LLM／搜尋服務端點維持原有連線方式。
- `fetch.provider: mcp` 先在本機檢查 URL 與重導鏈；外部抓取服務仍須自行限制非公開位址及重導。本機預檢無法保證另一台服務的 DNS 視圖。
- npm／PyPI 指定版本或檔案時，provenance 與版本狀態也使用該目標。PyPI 未指定檔案的版本頁，代表檔案 provenance 不能解讀為該版本所有檔案均已驗證。
- 冷卻期維持提示、不影響 verdict／confidence；原網址不是登錄網址而重導終點是登錄網址時，也會查詢終點的冷卻期。
