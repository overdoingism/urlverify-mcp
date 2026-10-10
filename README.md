# URLVerify_MCP

An [MCP](https://modelcontextprotocol.io) server that tells an AI agent whether a download, install command, repository
or model is the project's **official** channel, before the agent fetches or runs it.

一個 [MCP](https://modelcontextprotocol.io) server：在 AI agent 下載或執行任何東西之前，判斷這個下載網址、安裝指令、
程式碼倉庫或模型，是不是該專案的**官方**管道。

- **Input / 輸入**: the project name and exactly what will be used: a URL or one install command
  (`pip install requests`, `winget install --id Docker.DockerDesktop`, `curl … | sh`).
  專案名稱，加上實際要用的東西：一個網址或一條安裝指令。
- **Output / 輸出**: `VERIFIED_TRUE` · `VERIFIED_FALSE` · `UNVERIFIABLE`, a confidence score, and a `next_action` the
  agent follows (`PROCEED` · `INFORM_USER_AND_CONFIRM` · `DO_NOT_PROCEED` · `FIX_INPUT_AND_RETRY`).
  三種判定、信心分數，以及 agent 應遵循的下一步。
- It checks *who publishes* a source, not whether the file is safe: no download, no hash or malware scan.
  它判斷的是「誰發佈的」，不是檔案是否安全：不下載檔案、不驗雜湊、不掃毒。

**Quick start / 快速開始:** [English](docs/quickstart.en.md) · [繁體中文](docs/quickstart.zh-TW.md)

---

## How it works

1. **Deterministic checks** (no LLM): TLS, DNS (with a DNS-over-HTTPS cross-check), redirects, look-alike domains,
   Certificate Transparency, hosting-platform owners, allow / deny lists, prompt-injection screening of the target page.
2. **Fixed lookups** (no LLM): Wikidata / Wikipedia, platform and registry records (GitHub, Hugging Face, PyPI, npm,
   NuGet, Flathub, SourceForge, Homebrew …), and the official site's own pages where needed.
3. **LLM investigation**, only for the identity links still missing after step 2 (aliases, renames, product ↔ company).
   Structured records are cited by fact id; web pages need verbatim quotes.
4. **Rules engine** decides: it verifies every quote, counts *independent* sources and matches the target against the
   established identity. The LLM proposes, the rules decide. When step 2 already decides, the LLM is never started.

## The `verify_source` tool

```
verify_source(project, source, artifact="", description="", version="", options=None) -> YAML
```

| Argument | Meaning |
|---|---|
| `project` | Product name only (`LM Studio`, `requests`). |
| `source` | Exactly what will be used: a URL or ONE install / download command. A bare name is rejected. |
| `artifact` | The form: `Windows x64 installer`, `Python package`, `Docker image`, `install script`. |
| `description` | What it is for. Give `artifact` or `description`, preferably both. |
| `version` | Optional exact version; empty = the registry's default. |
| `options` | Optional: `mode` (`auto` · `fast` = no LLM · `quick` · `full`), `origin` (distribution packages, see below). |

The reply is YAML. `machine_readable` comes first and is the only part to act on; everything after it may quote untrusted
web pages, and verdict words inside it are neutralised.

```yaml
machine_readable:
  schema_version: 2
  verdict: VERIFIED_TRUE        # worst subject wins
  next_action: PROCEED
  confidence: 0.9
  codes: []                     # why it is not TRUE / what blocked it
  notices: []                   # conditions to tell the user about (e.g. OFFICIAL_DOWNLOAD_HOST, WIKIMEDIA_ONLY)
  subjects: [...]               # one per package / URL
summary: ...                    # fixed sentences in the caller's language
explanation: ...                # the LLM's prose
details: ...                    # evidence, checks, identity, notes
```

### What `source` accepts

| Verified | Commands / notes |
|---|---|
| URL | any `https://…` |
| PyPI | `pip`, `python -m pip`, `uv pip/add/tool`, `uvx`, `pipx`, `poetry add`, `pdm add`, `pipenv install` |
| npm | `npm i/install/add`, `yarn add`, `pnpm add`, `bun add`, `npx`, `pnpm dlx`, `bunx` (aliases resolved) |
| NuGet | `dotnet add package`, `dotnet tool install`, `nuget install`, `Install-Package` |
| WinGet | `winget install --id <Id>`: the installer URL from the winget-pkgs manifest is verified |
| git | `git clone`, `gh repo clone` |
| Hugging Face | `hf download`, `huggingface-cli download` |
| Containers | `docker` / `podman` / `nerdctl` `pull` / `run` on Docker Hub and ghcr.io |
| Homebrew | `brew install [--cask]` (official taps) |
| Scoop | `scoop install [bucket/]<app>` (official ScoopInstaller buckets) |
| Flatpak | `flatpak install [flathub] <app-id>`; apps not verified by their developer stay `UNVERIFIABLE` |
| Go | `go install` / `go get <module>@<version>` |
| apt / dnf / pacman | pass the package manager's origin report in `options.origin` (`LC_ALL=C apt-cache policy <pkg>`, `dnf info`, `pacman -Si`) |
| Scripts | `curl … \| sh`, `irm … \| iex` and similar: the script URL is verified, not what it downloads next |

Parsed but not verified yet (`ECOSYSTEM_NOT_YET_VERIFIED`): cargo, gem, composer, choco, conda, zypper, apk, AUR,
snap, ollama, Install-Module, msstore, other container registries. Always rejected: lock / requirement files, local paths,
extra indexes, chained commands (`&&`, `;`). Unknown flags are reported, never ignored.

## Trust rules in brief

- An official domain or account needs **two independent source families** (Wikimedia counts as one; all domains of one
  hosting platform count as one).
- **Self-published projects** (a personal GitHub / Hugging Face account, nothing else) are checked for consistency across
  platforms only: TRUE is capped at 0.75.
- **Mirrors, CDNs and SourceForge**: accepted only when the established official site links that exact file (or the
  SourceForge project, which links back) or redirects to the same file; TRUE capped at 0.8 with `OFFICIAL_DOWNLOAD_HOST`
  (compare the published checksum). SourceForge's automatic mirrors are `SOURCEFORGE_MIRROR`.
- **Old, widely read Wikimedia records** may establish a domain alone (value unchanged since a random point 18–30 months
  ago, ≥ 2000 page views every month); lowest-confidence path, capped at 0.6 with `WIKIMEDIA_ONLY`.
- **Packages** with signed build provenance from an established GitHub organisation take a fast path; packages without
  it need independent evidence like any website. A recently published version carries `RELEASE_COOLDOWN_*`
  (`release_cooldown.hours`, default 72).
- **Known malware**: a package OSV.dev lists as malware in every version is `VERIFIED_FALSE`; if only the version to be
  installed is a listed malicious release of an official package, the verdict stands but `next_action` is
  `DO_NOT_PROCEED`. PyPI quarantine is `DO_NOT_PROCEED` too.
- A URL whose host does not exist is `UNVERIFIABLE` with `TARGET_DOMAIN_DOES_NOT_EXIST` (`FIX_INPUT_AND_RETRY`).
- `UNVERIFIABLE` means "not enough independent evidence", not "dangerous".

Every threshold is configurable; see [document_for_config.md](document_for_config.md).

## Security and privacy

- **Network targets**: private, loopback and link-local addresses (and redirects to them) are refused.
- **TLS interception / DNS poisoning**: the leaf certificate must be logged in Certificate Transparency, and the host is
  resolved again over DNS-over-HTTPS.
- **Admin UI**: password login (default `admin`; change it in the UI). Keep it on `127.0.0.1`.
- **HTTP transport**: set `server.auth_token` to require `Authorization: Bearer <token>`; default bind `127.0.0.1`.
- **What leaves your machine**: project names, URLs and host names go to the public services that are looked up
  (Wikimedia, archive.org, crt.sh, DoH resolvers, GitHub, Hugging Face, package registries, OSV.dev, Flathub, SourceForge,
  Homebrew, your SearXNG, and Reddit / HN / Stack Exchange for dating forum posts). Page text and the investigator's
  messages go to the LLM endpoint you configure. No telemetry.
- **Footprint**: documents only (no images, scripts or files under verification), per-run request budgets, caches for
  certificates and identities, JSON APIs instead of scraping.

## Files

```
config.yaml   machine-specific settings (git-ignored)
state/        caches, admin password hash, edited prompts (rebuildable)
log/          history (one JSON per verification), dependency health, process logs
```

Nothing is written outside the project folder; there is no database.

## Tests

| Suite | Command | Needs |
|---|---|---|
| Unit + replay | `uv run pytest -q` | nothing (a few network tests skip when offline) |
| Live regression | `uv run pytest tests/test_live.py --live -s` | LLM, search, network; tens of minutes |

## Docs

- [docs/quickstart.en.md](docs/quickstart.en.md) · [docs/quickstart.zh-TW.md](docs/quickstart.zh-TW.md): install, configure, connect a host.
- [document_for_config.md](document_for_config.md): every `config.yaml` field.
- [AGENTS.md](AGENTS.md): design charter and decisions (Traditional Chinese).
- [CHANGELOG.md](CHANGELOG.md).

MIT License, see [LICENSE](LICENSE).
