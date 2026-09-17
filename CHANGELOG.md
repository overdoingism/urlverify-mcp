# Changelog

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
