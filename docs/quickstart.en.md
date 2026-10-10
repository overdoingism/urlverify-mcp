# Quick start

[繁體中文](quickstart.zh-TW.md) · [Back to README](../README.md)

## 1. Requirements

- **Python ≥ 3.11**, or [uv](https://docs.astral.sh/uv/) (preferred).
- An **OpenAI-compatible LLM endpoint** (llama-server, LM Studio, vLLM, Ollama, OpenAI …). Optional: without one, run
  in no-LLM mode (step 5).
- Optional but recommended: a **SearXNG** instance with the JSON API enabled (`search.formats: [html, json]`).

## 2. Install

```bash
./setup-venv.sh            # Linux / macOS
.\setup-venv.ps1           # Windows PowerShell
```

Creates `.venv` with uv (from `uv.lock`), or with `python -m venv` + pip when uv is missing. Safe to re-run; the old
`.venv` is kept as a backup. Without uv, replace `uv run urlverify-mcp` below with `.venv/bin/urlverify-mcp`
(Windows: `.venv\Scripts\urlverify-mcp`).

## 3. Configure

```bash
uv run urlverify-mcp init-config      # writes config.yaml from config.example.yaml
```

Edit the endpoints in `config.yaml`:

```yaml
llm:
  base_url: "http://127.0.0.1:8080/v1"   # any OpenAI-compatible server
  model: "your-model-id"

search:
  provider: searxng_http                  # searxng_http | mcp | none
  searxng_http:
    base_url: "http://127.0.0.1:8888"
```

Every other field is explained in [document_for_config.md](../document_for_config.md). On Windows, write paths with
forward slashes or in single quotes, and use `chcp 65001` for UTF-8 console output.

## 4. Check the endpoints

```bash
uv run urlverify-mcp check-env
```

Confirms that the LLM and the search backend answer. Fix these before going on.

## 5. Run

| Goal | Command |
|---|---|
| Admin UI (settings, editable prompts, history, manual test) | `uv run urlverify-mcp admin` → http://127.0.0.1:8765 (password `admin`, change it) |
| MCP server, stdio | `uv run urlverify-mcp serve` (prints nothing; the host talks to it) |
| MCP server, HTTP | `uv run urlverify-mcp serve --transport http` → `http://127.0.0.1:8766/mcp` |
| One check from the shell | `uv run urlverify-mcp verify "LM Studio" "https://lmstudio.ai/download" --artifact "Linux AppImage"` |

`verify` prints the same YAML an MCP host receives (`--json` for the internal JSON; exit code 0 only for
`VERIFIED_TRUE`). `--mode fast` runs without the LLM: seconds instead of minutes, but cases that need an investigation
come back `UNVERIFIABLE` with `NO_LLM_MODE`. To make that the default, set `llm.enabled: false`.

## 6. Connect your MCP host

```jsonc
// stdio: the host starts the server
{ "mcpServers": { "urlverify": {
    "command": "uv", "args": ["--directory", "/path/to/urlverify-mcp", "run", "urlverify-mcp", "serve"] } } }

// HTTP: the server is already running with --transport http
{ "mcpServers": { "urlverify": { "url": "http://127.0.0.1:8766/mcp" } } }
```

A verification with the LLM can take several minutes. The server sends MCP progress notifications and a heartbeat, so
hosts that reset their timeout on progress do not time out.

## 7. Tell the agent when to use it

The tool description says *what* `verify_source` does; *when* to use it belongs in your host's system prompt:

> Regardless of the source, before downloading any toolchain, executable, source code, library, or script, you MUST verify its URL using
> URLVerify_MCP. If the verdict is not VERIFIED_TRUE, or the result carries any additional conditions, you MUST report the issue to the
> user and ask for permission before proceeding. If URLVerify_MCP is unavailable, you MUST report this to the user and obtain
> permission before downloading or installing anything.
> Package installs (pip, npm, NuGet, winget, docker, git clone, curl | sh …) download and run code too: pass the exact command
> you intend to run as `source`, and follow `machine_readable.next_action` in the reply.
