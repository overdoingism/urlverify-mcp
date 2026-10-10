# 快速開始

[English](quickstart.en.md) · [回到 README](../README.md)

## 1. 需求

- **Python ≥ 3.11**，或使用 [uv](https://docs.astral.sh/uv/)（建議）。
- 一個 **OpenAI 相容的 LLM 端點**（llama-server、LM Studio、vLLM、Ollama、OpenAI…）。沒有也能用，見第 5 步的 no-LLM 模式。
- 選用但建議：一個開啟 JSON API 的 **SearXNG**（`search.formats: [html, json]`）。

## 2. 安裝

```bash
./setup-venv.sh            # Linux / macOS
.\setup-venv.ps1           # Windows PowerShell
```

有 uv 時依 `uv.lock` 建立 `.venv`，沒有 uv 時改用 `python -m venv` + pip。可重複執行，舊的 `.venv` 會留作備份。
沒有 uv 的話，下文的 `uv run urlverify-mcp` 換成 `.venv/bin/urlverify-mcp`（Windows：`.venv\Scripts\urlverify-mcp`）。

## 3. 設定

```bash
uv run urlverify-mcp init-config      # 依 config.example.yaml 產生 config.yaml
```

編輯 `config.yaml` 裡的端點：

```yaml
llm:
  base_url: "http://127.0.0.1:8080/v1"   # 任何 OpenAI 相容的服務
  model: "your-model-id"

search:
  provider: searxng_http                  # searxng_http | mcp | none
  searxng_http:
    base_url: "http://127.0.0.1:8888"
```

其他欄位的說明見 [document_for_config.md](../document_for_config.md)（英文）。Windows 上路徑請用正斜線或單引號，
並以 `chcp 65001` 讓主控台使用 UTF-8。

## 4. 檢查端點

```bash
uv run urlverify-mcp check-env
```

確認 LLM 與搜尋服務都有回應，有問題先排除再往下。

## 5. 執行

| 用途 | 指令 |
|---|---|
| 管理介面（設定、可編輯的 prompts、歷史紀錄、手動測試） | `uv run urlverify-mcp admin` → http://127.0.0.1:8765（密碼 `admin`，請改掉） |
| MCP server（stdio） | `uv run urlverify-mcp serve`（不會印任何東西，由 host 與它溝通） |
| MCP server（HTTP） | `uv run urlverify-mcp serve --transport http` → `http://127.0.0.1:8766/mcp` |
| 在命令列驗證一次 | `uv run urlverify-mcp verify "LM Studio" "https://lmstudio.ai/download" --artifact "Linux AppImage"` |

`verify` 印出的 YAML 與 MCP host 收到的相同（`--json` 改印內部 JSON；只有 `VERIFIED_TRUE` 時結束碼為 0）。
`--mode fast` 不啟動 LLM：幾秒就有結果，但需要調查的案例會回 `UNVERIFIABLE` 並附 `NO_LLM_MODE`。
要預設不用 LLM，設定 `llm.enabled: false`。

## 6. 接上 MCP host

```jsonc
// stdio：由 host 啟動 server
{ "mcpServers": { "urlverify": {
    "command": "uv", "args": ["--directory", "/path/to/urlverify-mcp", "run", "urlverify-mcp", "serve"] } } }

// HTTP：server 已用 --transport http 啟動
{ "mcpServers": { "urlverify": { "url": "http://127.0.0.1:8766/mcp" } } }
```

有 LLM 參與的驗證可能要好幾分鐘。server 會送出 MCP 進度通知與心跳，會依進度重設逾時的 host 就不會逾時。

## 7. 告訴 agent 什麼時候要用

工具說明只講 `verify_source` 做什麼；「什麼時候必須用」屬於 host 的 system prompt。建議放入下面這段（英文原文，供 agent 閱讀）：

> Regardless of the source, before downloading any toolchain, executable, source code, library, or script, you MUST verify its URL using
> URLVerify_MCP. If the verdict is not VERIFIED_TRUE, or the result carries any additional conditions, you MUST report the issue to the
> user and ask for permission before proceeding. If URLVerify_MCP is unavailable, you MUST report this to the user and obtain
> permission before downloading or installing anything.
> Package installs (pip, npm, NuGet, winget, docker, git clone, curl | sh …) download and run code too: pass the exact command
> you intend to run as `source`, and follow `machine_readable.next_action` in the reply.

大意：下載任何工具鏈、執行檔、原始碼、函式庫或腳本之前，都必須先用 URLVerify_MCP 驗證；結果不是 `VERIFIED_TRUE` 或附帶條件時，
必須告知使用者並取得同意；URLVerify_MCP 無法使用時也一樣。套件安裝指令同樣要驗證，把實際要執行的指令當作 `source` 傳入，
並依回覆中的 `machine_readable.next_action` 行事。
