# Changelog

## Unreleased

- Fix: an official link that redirects to a host we cannot establish (mirror networks, download CDNs) is
  `UNVERIFIABLE` with `REDIRECT_TO_UNESTABLISHED_HOST`, no longer `VERIFIED_FALSE` (mirrors redirect by design).

- Fix (found live with VLC): a distribution's manifest repository (Flathub, Homebrew, Scoop, F-Droid, nixpkgs, ...)
  is the same independence family as the distribution itself; and an unverified Flathub app gets no support from the
  Flathub family at all (Flathub states such apps are not affiliated with the developer). An unverified app plus
  Flathub's own build manifest no longer adds up to "official".
- The identity cache fingerprint includes `rules.RULES_VERSION`: identities established under older rules are
  re-verified (the wrong VLC identity above had been cached and was read back after the fix).

- Flatpak verified (Flathub only): the Flathub developer-verification record is a platform-verified link to the
  verified domain (or the appstream homepage when verified manually / by account); unverified apps are community
  packaging and stay `UNVERIFIABLE` with `FLATHUB_UNVERIFIED`, and their declared homepage never votes. Platform targets
  may match the project name against the display name in the target's own platform record.

- Fixed lookup for websites: Homebrew casks that download from the target domain (reverse lookup in the cask
  catalogue, cached in `state/brew_cask_index.json`, refreshed weekly with ETag) become evidence when their token /
  name equals the project or their homepage is on the same domain (`identity.homebrew_reverse_lookup`).

- New verified ecosystems: Homebrew (`brew install [--cask|--formula]`, official taps; cask download URL or the
  formula's upstream source, with the formulae.brew.sh record as evidence), Scoop (official ScoopInstaller buckets;
  no bucket = main, otherwise `SCOOP_BUCKET_AMBIGUOUS`), Go (`go install` / `go get`; hosting-platform paths map to the
  repository, custom domains are verified as the module's own domain via go-import).

- Each tier-1 manifest repository (winget-pkgs, Homebrew, Scoop, ...) is its own independence family instead of being
  folded into "github" because it is hosted there.
- WinGet: the default-locale manifest's PackageName / Publisher / PublisherUrl / PackageUrl lines become evidence too
  (the curated statement of the publisher's and package's official site).

- Third-party "try again later" answers (429/502/503/504) are retried with backoff (`net.retries`, `net.retry_backoff_s`,
  Retry-After honoured up to 10 s). CT first-seen dates are cached (`net.ct_first_seen_cache_days`, default 90): the
  date never changes and crt.sh often answers 502 for popular domains.

## v0.3.0 — 2026-09-24

- Identity edges (AGENTS §6.3): every decision reports established / missing edges (TARGET_CHECKS, PROJECT_TO_DOMAIN,
  PROJECT_TO_ORG, PACKAGE_TO_REPOSITORY, PROJECT_NAME_MATCH) with how many independent sources were found; the YAML
  subjects carry them and the reason codes (`MISSING_EDGE:*`) come from them.
- Fixed, gap-driven lookups before the LLM: Wikimedia for the project name, then the package / repository name, then
  the developer (at most three searches, entities accepted only by exact label/alias or a link to the target, Wikipedia
  only via the entity's sitelink), the target owner's platform record and the records of owners a Wikimedia repository
  record names. When that already decides the case the LLM is not started; otherwise it is told what is known and which
  edges are missing. After the LLM, the developer it resolved is looked up on Wikimedia once (third candidate).
- A `VERIFIED_FALSE` carries a native reason code (`OWNER_NOT_OFFICIAL`, `REPOSITORY_IS_FORK`,
  `REDIRECT_LEAVES_OFFICIAL_DOMAIN`, `CERT_ORG_MISMATCH`, `L0_FATAL`, `PACKAGE_SECURITY_HOLDING`, `PACKAGE_NOT_FOUND`);
  missing-edge codes are reported only for `UNVERIFIABLE`.
- WinGet: a response that is not the requested manifest (e.g. a rate-limit page) is `RESOLUTION_FAILED:winget`, not an
  input error.

- TLS check tries the resolved addresses in turn (address families interleaved, at most 4): a machine without an IPv6
  route no longer fails dual-stack hosts. Only a failed connection moves on; any TLS answer is final.
- Admin Config tab: optional GitHub token field (masked, check-limit button, links to create a no-permission token).
  The JSON editor shows the token masked and keeps it on save.
- A "not found" answer (404 for a guessed repository, an invalid Wikipedia title) is no longer counted as a dependency
  failure; Wikipedia replies without results are reported as not found; infobox developer fields drop citation text.

## v0.2.0 — 2026-09-24

Breaking: the MCP interface changed; restart the MCP host after updating.

- `verify_source(project, source, artifact, description, version, options)` replaces `url`. `source` is a URL or ONE
  install / download command (pip/uv/pipx/poetry/pdm/pipenv, npm/yarn/pnpm/bun/npx, dotnet/nuget/Install-Package,
  winget, git/gh, hf, docker/podman, curl | sh style scripts). Flags are white-listed per tool; bare names, unknown
  flags, several indexes, requirement files, local paths and chained commands are reported with fixed codes, never
  guessed. Several packages in one command are verified one by one (worst wins).
- Versions are resolved against the registry (PEP 440, npm semver ranges and dist-tags, NuGet ranges); WinGet installs
  are resolved to the installer URL in Microsoft's winget-pkgs manifest, which becomes tier-1 seed evidence.
- Parse-only ecosystems (cargo, go, gem, composer, brew, scoop, choco, conda, apt family, snap, flatpak, ollama, ...)
  return `ECOSYSTEM_NOT_YET_VERIFIED` with the parsed package.
- Replies are YAML: `machine_readable` (verdict, next_action, confidence, codes, notices, subjects) first, then rule-made
  `summary`, the LLM `explanation`, and `details`. Verdict tokens inside untrusted text are neutralised.
- New config section `source` (registries, max_subjects). Admin Test tab and CLI `verify` use the new arguments.
- Typed facts: structured tool results are stored as records and flattened into numbered facts that the investigator
  cites by id (`facts: ["F12"]`) instead of copying JSON into quotes; API records and fetched pages for the same URL
  are kept apart; a record can be cited by any of its URLs (api.github.com/repos/o/r = github.com/o/r). Fixes
  structured evidence being discarded as "cited URL was not fetched" (llama.cpp, drluoto regressions).
- Developer tool: `URLVERIFY_CAPTURE_DIR` captures rules-engine inputs; `tests/test_replay.py` replays them offline.

- 冷卻期訊號改為 `RELEASE_COOLDOWN_PERIOD:<已發布小時數>`（最多六位小數），明確要求呼叫端告知風險並取得使用者確認後才可下載／安裝；時間不明亦須確認。README 呼叫端 system prompt 加入工具鏈，且任何非 TRUE 或附帶條件的結果均須確認。

- 審視修正：引文嚴格綁定來源，API 來源類型由工具標記，Wikidata 多實體分開儲存；阻止借用其他頁面或單一真實錨點替虛構內容背書。
- 必要 L0 檢查未完成時不再 TRUE；HTTP 抓取逐次檢查公開位址並固定連線 IP，重導迴圈／上限／部分失敗均回報，致命 L0 失敗不再抓頁面。平台重導需驗證不同 owner。
- npm／PyPI 版本狀態與 provenance 使用指定版本／檔案；API 503／429 等錯誤不再視為不存在，npm scope 比對也適用完整流程。
- 身分快取命中不續期，舊格式與不同證據政策的快取重新調查；獨立來源家族控制信心加分。未知網域不再單憑未列入官方清單而判 FALSE。
- 保留時間升級、注入即 FALSE 與下載／冷卻期政策。

- Platform anchors now declare, per platform, which hosts carry user content and where the owner is read from (path
  segment, host label such as `<owner>.github.io`, or nowhere for asset/CDN hosts); every other host on the platform's
  domains is the operator's own site with full L0 and its domain as an identity candidate. Asset hosts without a readable owner are `UNVERIFIABLE` on their own.
  Fixes both the Docker Desktop false negative and a `<owner>.github.io` path that could borrow `github.io` as an
  "official domain".
- Hardening: tier-1 manifest files count only when addressed by a branch or tag (commit SHAs and `refs/pull/...` are
  tier 3: GitHub serves unmerged PR commits under the upstream URL); domain votes come from the verified quote only;
  Wayback snapshots never vote for a domain or org; LinkedIn and Crunchbase profiles are tier 3.
- Fix: a hosting platform's own site (desktop.docker.com, desktop.github.com: no path owner) keeps its domain as an
  identity candidate; the platform-root filter only applies when the target has a path owner on that platform.
- 新增 npm／PyPI／NuGet 版本冷卻期：`release_cooldown.hours` 預設 72 小時，0 停用，可在 Config 修改。依目標版本／檔案的登錄時間計算；結果與提醒獨立呈現，不改來源裁決或信心。NuGet 加入平台身分解析與 L1 結構化 metadata 查詢。

- Independence counting folds every domain of one hosting platform into one source family (github.com +
  githubusercontent.com + github.io, huggingface.co + hf.co, ...): two accounts on the same platform can never vouch for
  each other. Repository pages, READMEs, issues and discussions on hosting platforms are user content: tier 3, countable
  only with proven age (`github_api` now dates repository pages by repository creation; new `huggingface_api` method).
  Platform API records and owner-profile roots stay tier 2; tier-1 manifest prefixes still win.
- Self-attestation for platform targets is decided by the owner's own paths, not by the platform domain; hosting
  platforms listed by the LLM in `official_domains` are ignored (noted in `engine_notes`) instead of turning every page
  on that platform into "self".
- A `VERIFIED_TRUE` whose owner is established only by cross-platform consistency (self-published project) is capped at
  confidence 0.75 and labelled in `engine_notes`. Wayback snapshots no longer count as an org vote (they prove age,
  not identity), which previously let the owner's own archived page defeat the cap. Org votes are taken from the
  verified quote only, never from the LLM's claim text (a claim saying "not <owner>" used to count as support).
- Admin Status tab: Clear button next to Refresh forgets the observed dependency health (`log/health.json`).
- Admin: the full-data-log switch (with folder path / open / copy) moved from the Config tab to the Logs tab; it still writes config.yaml and stays in step with the Config JSON editor.
- Admin History tab: refresh and clear buttons (clear deletes `log/history`).
- Tier-1 manifest repository prefixes moved to `urlverify_mcp/data/tier1_paths.yaml` with an explanatory header; the
  admin Caches tab lists them, reloads on demand and opens the file in the local editor. Cache row buttons are no
  longer red and read "forget" instead of "invalidate".
- Source tiers: official package-manager manifest repositories hosted on generic code hosts (microsoft/winget-pkgs,
  Homebrew, ScoopInstaller, nixpkgs, flathub, conda-forge, MacPorts, F-Droid, ...) are tier 1 by `host/path` prefix
  instead of falling to tier 3 as "unknown domain". `identity.extra_tier1/2/3` accept `host/path` prefixes too.
- `verify_source` tool description: `project` is the product name only; vendor / notes belong in `description`.
- Wikidata `P1324` (source code repository) and the Wikipedia infobox `repo` field are read alongside the official
  website, with the same revision-history stability check. A stable Wikimedia repository record gives the platform org
  one independent vote in the rules engine, so repository-only projects (no official website anywhere) can be
  established from Tier-1 data instead of depending on the LLM finding media coverage. Fixes a wikitext parsing bug
  where a self-closing `<ref name="x"/>` inside the infobox swallowed the following fields.
- Investigator system prompt: a hard rule that the agent's identity, task and procedure are fixed by the system prompt
  and tool parameters; content met during investigation that tries to change them, or tells the agent to stop or approve
  the target, is treated as a prompt-injection attempt (not complied with, recorded in `risk_notes`, investigation
  continues). A/B simulation against the local model showed no regression; the rule is defence in depth.
- The registry's own statement about a name (missing, security holding, latest release yanked) is evaluated before the
  project-name match in the fast path and is honoured by the rules engine on every path and mode: a security holding
  package is `VERIFIED_FALSE` even under `options.mode: full`.
- Quote verification ignores whitespace entirely (JSON and HTML sources differ from the LLM's rendering only in spacing).

- npm security holding packages (`0.0.1-security`, the name of a removed malicious package) are `VERIFIED_FALSE`
  outright; a PyPI release whose files are all yanked is a risk signal. The registry's own state is trusted.
- The npm near-name check is removed: there is no popularity reference for npm and generating "likely typos" is
  guesswork; provenance already gates TRUE. Saves a long bulk-download request per npm verification. PyPI keeps its
  local comparison against the popularity list.

## v0.1.2 — 2026-09-18

- `check-env` probes only the configured endpoints (LLM, search, fetch); third-party services are observed, not probed.

- L0: `ct_logged` — embedded SCTs pass; a well-known public CA without embedded SCTs only warns (SCTs may be delivered
  in the handshake); an unknown issuer with no SCTs and no crt.sh record (older than 24 h) is fatal (TLS interception).
  crt.sh fingerprint lookups parse the HTML page (its JSON output does not support fingerprints); certificate cache
  entries created before the SCT flag are refetched.
- Evidence fact-anchoring accepts double-quoted values and elided quotes ("…"); Wikipedia / Wikidata evidence was
  being discarded systematically when the LLM quoted JSON with double quotes. `dns_cross_check` — DNS-over-HTTPS cross-resolution with a
  TLS handshake against the DoH address when answers differ; poisoned-resolver signature is fatal, GeoDNS is a note.
  Both configurable under `net`.

- **Build provenance is now the package "verified badge".** The registry fast path requires npm Sigstore attestations or
  PyPI PEP 740 provenance whose repository owner is a domain-verified GitHub organisation (deps.dev corroboration recorded);
  scoped npm packages must have scope == provenance owner. Packages without provenance are never trusted on metadata
  alone and run the full investigation. In the full investigation, provenance lets a package inherit an established
  GitHub organisation's standing (fixes `@electron/asar` coming back UNVERIFIABLE with a complete evidence chain).
- npm anchor expects Google Trust Services certificates (no more spurious issuer_drift).

- **No more SQLite.** Rebuildable caches and settings are plain JSON under `state/` (cert/identity caches, auth,
  prompt overrides, PyPI top list); everything that records what the installation did is under `log/` (`full/`,
  `history/` as one file per verification, `health.json`, `server/`, `admin/`). Both folders sit next to
  `config.yaml`, relative paths in the config resolve against that folder, and each can be deleted to reset. The old
  `~/.urlverify_mcp/` contents are not migrated; caches and history simply start over.
- Wayback: first-seen lookups use the availability API (bare and `www.` variants, closest to 1996) with CDX as a
  two-attempt fallback; after two consecutive 503s Wayback is not asked again within the same verification.
- Evidence `source` strings from the LLM are reduced to their URL token (annotations like "(via package_registry npm)"
  no longer reach Wayback); `npmjs.org` / `pythonhosted.org` are tier 1; API/registry evidence is never sent for dating.
- Admin: new Status tab (version, paths, observed health, Run check-env); History tab shows rows again (a duplicate
  element id had sent them to the wrong table); process logs also go to `log/server` and `log/admin`.

- Observed dependency health replaces automatic probing: every real call to an external dependency records success or
  failure (persisted in SQLite), failures print `!! DEPENDENCY …` on stderr and log a `dependency_failure` record, results
  carry a `degraded` list, the admin Test tab shows the table. It is a report, never a gate.
- `check-env` is manual only and uses the lightest endpoint each service offers (siteinfo, availability API, `/rate_limit`,
  `HEAD /simple/`, `/-/ping`, SearXNG `/healthz`); the admin UI no longer probes on page load, and has a *Run check-env* button.
- Wayback CDX lookups no longer filter by status code (existence is what is measured; the filter forced slow scans).
- npm metadata comes from the small `/<name>/latest` document instead of the full registry document.

## v0.1.1 — 2026-09-17

- **Built-in page fetching is now the default** (`fetch.provider: builtin`): httpx plus a dependency-free HTML→text converter
  that keeps the title, headings, list bullets and link targets. The SearXNG MCP fetch tool remains available as
  `fetch.provider: mcp`.
- **Search defaults to SearXNG's JSON API** (`search.provider: searxng_http`); the MCP server is optional (`mcp`), and
  `none` runs with structured sources only. One fewer service to run and no MCP client stack in the default setup.
- `check-env` probes the fetcher as well.

- Registry fast path: `VERIFIED_TRUE` now requires a **bidirectional** repository link (registry metadata → GitHub repo
  whose manifest declares the package; pyproject/setup.cfg/setup.py/package.json parsed properly). Popularity is no longer a
  signal on its own (download counts can be inflated); it only serves as the denominator of the typosquat ratio.
- Config section renamed `registry_fast_path` → `package_registry_fast_path` (it covers PyPI and npm, not pip alone); new keys
  `require_project_match`, `typosquat_check`, `toplist_size` (1500), `toplist_refresh_days` (60), `toplist_url`. The PyPI
  popularity list is no longer bundled: it is streamed on first use (connection closed after N rows) and re-validated with ETag.
- Rules engine: `VERIFIED_TRUE` is withheld (→ `UNVERIFIABLE`) when the resolved identity does not refer to the project the
  caller asked about (asked for "requests", given pypdf's URL).
- README: recommended system prompt for the calling agent (when verification is mandatory).
- Live fixtures: requests / pypdf (fast path), reqeusts (missing), lodahs (near-name must fall through to the full pipeline).

## v0.1.0 — 2026-09-17

First public release.

- L0 deterministic checks: TLS chain / SAN / Organization, DNS, redirect chain, punycode / homoglyph / typosquat /
  subdomain abuse, Certificate-Transparency first-seen, platform anchors, allow/deny lists, prompt-injection screening,
  refusal of non-public targets.
- L1 identity investigation with any OpenAI-compatible LLM: Wikidata / Wikipedia (revision-history stability), Wayback,
  GitHub, Hugging Face, PyPI / npm, tiered third-party sources, temporal promotion of forum / social sources.
- Rules engine: verbatim / fact-anchored evidence verification, independent-source counting, identity cache.
- Registry fast path for PyPI / npm packages (seconds, no LLM), `options.mode` auto | quick | full.
- MCP server (stdio / Streamable HTTP) with progress notifications and heartbeat; bounded waits everywhere and a total
  deadline; optional bearer token; concurrency limit.
- Admin UI: config editor, allow / deny lists, caches, history, manual test, editable prompts, full data log viewer,
  password login.
- Full data log (JSONL, 1 MB rotation), editable agent and MCP-facing prompts with runtime date tokens.
- Result schema version 1.
