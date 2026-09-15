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

```bash
# 1. dependencies — or: .\setup-venv.ps1 (Windows) · ./setup-venv.sh (Linux/macOS)
uv sync --extra dev            # or: python -m venv .venv && pip install -e ".[dev]"

# 2. configuration — point at your LLM + search endpoints / 設定你的 LLM 與搜尋端點
cp config.example.yaml config.yaml    # Windows: copy / Copy-Item

# 3. probe, then run / 探測後啟動
uv run urlverify-mcp check-env
uv run urlverify-mcp admin            # admin UI at http://127.0.0.1:8765
uv run urlverify-mcp serve            # MCP over stdio (or --transport http)
uv run urlverify-mcp verify "LM Studio" https://lmstudio.ai/download "Linux AppImage"
```

## Tests / 測試

```bash
uv run pytest                          # offline unit tests (+ network pipeline test, auto-skips when offline)
uv run pytest tests/test_live.py --live -s   # live regression (needs LLM + search + network)
```

## Docs & license / 文件與授權

- [AGENTS.md](AGENTS.md) — design charter: interface contract, source tiering, safety red lines (Traditional Chinese). / 設計憲章：介面契約、來源分級、安全紅線（繁體中文）。
- MIT License. See [LICENSE](LICENSE). / 採用 MIT 授權，見 [LICENSE](LICENSE)。
