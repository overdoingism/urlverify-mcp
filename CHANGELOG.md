# Changelog

## Unreleased

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
