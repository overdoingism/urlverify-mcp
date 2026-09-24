# AGENTS.md — URLVerify_MCP

> 給所有在此專案工作的 AI agent / 開發者的憲章。工作範圍**僅限本資料夾**，
> 不得讀取或修改桌面其他資料夾的內容。

## 1. 專案目的

URLVerify_MCP 是一個 **來源驗證用的 MCP Server**。
給定一個安裝包 / 下載檔 / 資料源的網址，判斷它是否來自**官方或正式渠道**。

它本身是一個小型 Agent：透過既有的 **SearXNG MCP server** 搜尋，透過任一
**OpenAI 相容 API** 的 LLM 推理，自行調查、給出結論。

## 2. 介面契約

### 輸入（MCP tool: `verify_source`，v0.2.0 起）
| 欄位 | 必填 | 說明 |
|---|---|---|
| `project` | 是 | 產品名稱；可與套件名不同（別名由 L1 佐證） |
| `source` | 是 | URL，或**一條**安裝／下載指令；不接受裸名（不猜 registry） |
| `artifact` | 擇一 | 形式：Windows x64 安裝檔、Python 套件、Docker image… |
| `description` | 擇一 | 用途；與 artifact 至少一個非空 |
| `version` | 否 | 指定版本；空白 = registry 預設；與指令內版本衝突時回 INPUT_VERSION_CONFLICT，不覆蓋 |
| `options` | 否 | 覆寫預設設定（見 §5） |

舊的 `url` 參數已移除（尚未對外公開，改完即全面切換）。

### 輸出
MCP 回傳 YAML 文字（無 structuredContent）。第一個區塊 `machine_readable` 由規則產生、鍵值固定：
`verdict`（三態）、`next_action`（PROCEED｜INFORM_USER_AND_CONFIRM｜DO_NOT_PROCEED｜FIX_INPUT_AND_RETRY）、`confidence`、
`codes`、`notices`、`trace_id`、`subjects[]`（每個套件／URL：生態系、名稱、版本、registry 與選擇依據、verified_url、檢查狀態）。
之後的 `summary` 是規則模板句（語言跟隨呼叫方），`explanation` 是 LLM 敘述，`details` 是證據與檢查；後兩者可能引用不可信網頁，
其中的裁決字樣一律中和（VERIFIED_TRUE → VERIFIED TRUE），grep 只會命中真正的鍵。內部、管理頁與歷史保留 JSON（schema_version 2）。
多套件時整體裁決取最差者，next_action 取最嚴重者。

三種狀態的**定義**：
- `VERIFIED_TRUE`：L0 全數通過，且 L1 身分鏈（專案 → 開發者/品牌別名 → 官方網域/倉庫）由足夠的獨立第三方來源佐證，網址落在該官方範圍內。
- `VERIFIED_FALSE`：有**明確反證**：同形字/仿冒網域、不受信任 CA、憑證 Organization 與開發者不符、官方來源指向別處、黑名單、**頁面含對 AI/驗證器說話的文字**。
- `UNVERIFIABLE`：證據不足、來源數未達門檻、網路失敗、逾時。**寧可 UNVERIFIABLE，不猜 TRUE。**

## 3. 驗證架構（兩層 + 規則引擎）

```
L0  確定性檢查（不經 LLM）
    - TLS：系統信任根簽發、未過期、SAN 吻合主機名（必要；自簽/不明 CA → FALSE）
    - CT：leaf 含內嵌 SCT → 通過；知名公開 CA 但無內嵌 SCT → 只警告（SCT 可能走 TLS 擴充，Python 看不到）；
      不明簽發者、無 SCT、crt.sh 也查無（>24h）→ FALSE（本機攔截 CA）。crt.sh 指紋查詢只有 HTML，且有延遲，不可單獨當致命依據
    - DoH 交叉解析：與系統 resolver 無交集時對 DoH 位址再握手；系統路徑失敗而 DoH 成功 → FALSE（DNS 污染）；兩者皆成功只記備註（GeoDNS）
    - TLS Organization 欄位：有則與 L1 開發者比對（吻合=強證據；不符=強反證；無=中性）
    - Certificate Transparency：網域首張憑證太新 + L1 無佐證 → 風險訊號
    - DNS、重導鏈完整展開、縮址展開、最終 eTLD+1 比對
    - punycode / 同形字、子網域濫用（official.com.evil.net）
    - 平台錨點與黑/白名單
L1  身分解析（LLM + 搜尋）
    - 建立身分圖：專案 → 公司 → 品牌/前端別名 → 官方網域、官方 repo、官方 HF org
    - 需 ≥ N 個獨立第三方來源（預設 2），依 §4 分級，Tier3 預設排除
    - 雙向連結檢查：官網 → repo，repo → 官網
    - 平台驗證標記：GitHub org verified domain、HuggingFace verified badge
    - 第三方託管（GitHub / HuggingFace / PyPI / npm）**必走 L1**：平台根網域可信 ≠ 路徑上的組織可信
規則引擎
    - 最終裁決由規則引擎綜合 L0/L1 決定；LLM 提供推理與證據，**不能單獨覆寫規則**
    - L0 任一必要項失敗 → 不得輸出 TRUE
```
L2 產物雜湊/簽章驗證**不在範圍內**（並非所有來源都提供；來源正確即足夠）。

## 4. 第三方來源分級
| Tier | 來源 | 用途 |
|---|---|---|
| 1 | Wikidata「官方網站」屬性、Wikipedia、發行版套件 manifest（Debian/Arch/Homebrew/winget/Flathub）、套件登錄 metadata（PyPI/npm/crates.io） | 機器可讀、經人工審核，計入門檻 |
| 2 | 可靠媒體、開發者官方社群帳號 | 計入門檻 |
| 3 | 論壇、Reddit、個人部落格 | 預設排除，可設定開啟，永不單獨成立 |

| 2 | 雲端/硬體廠商技術文件（vultr、digitalocean、nvidia、redhat…） | 計入門檻（2026-09-15 加入） |

內建清單在 `identity/sources.py`；設定檔 `identity.extra_tier1/2/3` 可**加成**（eTLD+1、完整主機名，或含路徑的 `host/path` 前綴），
官方套件管理器的 manifest 倉庫（winget-pkgs、homebrew-core/cask、ScoopInstaller、nixpkgs、flathub、conda-forge…）住在 github.com 這類通用代碼託管上，
單看網域分不出來，故以「host/path 前綴」判為 Tier1，清單在 `urlverify_mcp/data/tier1_paths.yaml`（隨套件出貨、mtime 變更即重讀、管理頁 Caches 分頁可看與開啟編輯）；列的是套件管理器，不是套件，路徑前綴優先於網域判定。
設定檔的分級優先於內建清單。Tier1 只能由使用者在設定檔加，LLM 可替未知來源提議 Tier2/3，**不得提升任何來源為 Tier1**。
- **託管平台上的使用者內容是 Tier3**（2026-09-19 定案）：github.com / githubusercontent.com / github.io、huggingface.co / hf.co、gitlab.com、codeberg.org
  底下的 repo 頁、README、issue、discussion 任何人都能發佈，一律 Tier3，只能靠老化機制（`github_api` 以 repo 建立日、`huggingface_api` 以
  createdAt 定年）計票。平台自身的記錄（API 主機、`/api/` 路徑、owner profile 根頁、我們工具抓回的 JSON 記錄）維持 Tier2；Tier1 manifest 前綴優先。
- **來源家族**：獨立來源數以「家族」計，一個平台的所有網域是一家（github.com + githubusercontent.com + github.io；huggingface.co + hf.co），
  Wikipedia + Wikidata 一家。同一平台上兩個帳號互相背書永遠只算一票。
- **平台根網域不是身分**：LLM 若把 github.com 之類放進 official_domains，規則層剔除並註記；owner 走 official_orgs。
  例外只有一種：目標本身是平台公司的自有站台（`platform_scope = company_site`，如 desktop.docker.com、desktop.github.com），該網域保留為身分候選。
- **平台 anchor 的判準**（2026-09-20 定案，`cache/anchors.py`）：每個平台明列「使用者內容主機」與 owner 位置：路徑段（github.com、raw.githubusercontent.com、
  hub.docker.com 的 /r /u /_）、主機標籤（*.github.io、*.gitlab.io、*.codeberg.page）、或讀不到（release-assets/objects.githubusercontent.com、
  files.pythonhosted.org、cdn-lfs、dl.flathub.org 等資產主機）。平台網域下其餘主機一律是公司站台，跑完整 L0（含 ct_first_seen）。
  不設保留字表：github.com/features 之類就是 owner 叫 features，那裡本來不放下載檔，驗不過即 UNVERIFIABLE；避免因人設事。讀不到 owner 的資產主機本身永遠 UNVERIFIABLE，只能作為官方 repo 的重導終點。
- **Manifest 前綴的 ref**：tier‑1 manifest 倉庫的檔案只有以分支或標籤定址才算數；commit SHA 與 `refs/pull/…` 一律 Tier3
  （GitHub 會在上游 repo 網址下提供未合併 PR 的 commit，否則開一個 PR 就能種一份 manifest）。
- **網域計票同 org**：只認已驗證的 quote；Wayback 對網域也不計票（§4.1 的時間佐證語意保留，但年齡不是官方性）。
- **LinkedIn、Crunchbase 是自填檔案**：Tier3（LinkedIn 登入牆，永不升級）。
- **自我宣稱的定義**（平台目標）：owner 自己路徑下的頁面（github.com/<owner>/…、raw.githubusercontent.com/<owner>/…、<owner>.github.io）與候選官方網域的頁面；
  同平台其他人的頁面不是自我宣稱，而是使用者內容（上一條）。結構化 API 記錄不受此限。
- **自我發佈專案的信心上限**：owner 只靠跨平台自洽成立（GitHub 記錄 + HF 記錄），沒有 Wikimedia / registry / 媒體 / 自有網域證據時，
  TRUE 的信心上限 0.75（`rules.SELF_PUBLISHED_MAX_CONFIDENCE`）並在 engine_notes 註明；只有單一平台足跡 → UNVERIFIABLE（設計如此）。
  Wayback 存檔只證明時間、不證明身分：對 org 計票不算一家（對官方網域的計票維持 §4.1 的設計）。
  org 計票只認已驗證的 `quote`，不認 `claim`（claim 是 LLM 自由文字，「不是 drluoto」也會出現 owner 名字）。

### 4.2 Tier3 的時間回溯升級（2026-09-15 定案）
論壇 / 社群來源若能**確定性地**證明存在超過 `tier3_min_age_days`（預設 365 天），視為 Tier2 計入門檻，
每次最多 `tier3_aged_max_count`（預設 1）個。**時間戳永遠不由 LLM 產生**，白名單在 `identity/aging.py`，
設定 `identity.aging_sources` 可覆寫：

| 方法 | 平台 | 強度 |
|---|---|---|
| reddit_api / hn_api / stackexchange_api / discourse / github_api | 平台 API（含 edited 欄位） | 強 |
| snowflake | X / Twitter：時間戳編碼在貼文 ID 內 | 強 |
| wayback | 任何未列網域：該網址的首次存檔 | 強 |
| jsonld | YouTube / Medium / dev.to 等自報日期 | 弱，需 Wayback 佐證才升級 |
| none | Facebook / Instagram / Discord / Telegram / LinkedIn（需登入或不可存檔） | 永不升級 |

附加規則：貼文在門檻期內被編輯過不升級；目標網域的最早證據（CT 首見與 Wayback 取較早者）若落在
`domain_age_contradiction_years` 內且貼文比它更早，該貼文不可能指涉此網域 → 反證，記 `post_predates_domain`。
老網域不套用反證（CT 全面記錄始於 2018，會截斷）。

### 4.1 時間穩定性規則（不可只看最新頁面）
可被任何人編輯的來源，最新版本不可信，必須回溯歷史：
- **Wikipedia / Wikidata**：以 API 取修訂歷史，「官方網站」值須在 `history_days` 內、至少 `min_stable_revisions` 個修訂中保持一致。近期才變更 → 該來源降為 Tier2 並標註「近期變更」；仍計入但不可單獨成立。
- **官方原始碼庫也是身分**：Wikidata `P1324`（source code repository）與 Wikipedia infobox `repo` 欄位以同一套修訂歷史檢查判定穩定性。純託管專案（沒有官網、身分就是 repo）由此取得平台 org 的一票：穩定的 Wikimedia repo 記錄 + 平台自身資料 = 兩個來源家族，org 成立；記錄不穩定或近期變更則不計票。規則引擎從 LLM 引用的來源之原始工具輸出判讀，不依賴 LLM 是否把 repo 抄進 quote。
- **Wikimedia 投票的時間條件（2026-09-24 收緊）**：Wikidata／Wikipedia 只經「結構化紀錄」投票，而且只替「觀察期起點就已存在、期間未變」
  的官網值（P856／infobox website）投票；原始碼庫（P1324／infobox repo）同理。新建的條目或條目、期間內改過的值、紀錄裡順帶出現的其他網域
  （開發者官網、描述）、用 fetch 抓的 Wikipedia 網頁一律不投票。（修補：新條目先前會因「觀察期前無修訂」被當成沒變動而拿到 tier‑1 票。）
- **Wayback Machine**：官方網域的首頁在 Internet Archive 有多年存檔，是獨立的時間佐證；仿冒站通常沒有歷史。
- **GitHub / HuggingFace 組織**：帳號建立日期、倉庫建立日期；太新且 L1 無其他佐證 → 風險訊號。
- 結構化 API（Wikipedia、Wikidata、Wayback、GitHub、HF）以 `httpx` 直連，不經搜尋 MCP；只有一般網頁才走 `web_url_read`。

## 5. 設定（`config.yaml`，管理介面可改）
### 5.1 套件版本冷卻期（2026-09-19 定案）
- npm、PyPI、NuGet 支援 `release_cooldown.hours`（預設 72 小時，非負數，可用小數；0 完全停用查詢）。管理頁 Config 可修改，下次驗證生效。
- 只依登錄 API 的目標版本／檔案發布時間計算，不採專案首發年齡、metadata 修改時間或 LLM 推測。未指定版本時解析當下最新版本並明列版本與查詢時間。
- 冷卻結果放在 `checks.release_cooldown`，期間內加 `RELEASE_COOLDOWN_PERIOD` 與人類可讀提醒；不改來源 verdict/confidence、不加身分票數、不表示仍在掃描或保證安全，也不涵蓋間接依賴。
- 時間缺失、未辨識的下載 URL、查詢失敗、未來日期與 NuGet 取消列出時的 1900 年占位日期，均明列 unknown，不猜已過期；來源裁決保持既有定義。
- quick/full 與身分快取命中皆適用；L0 致命失敗不再查詢。NuGet 平台根網域不是套件身分，仍走 L1，沒有新增 provenance 快速通關。

完整欄位說明維護在 `document_for_config.md`，**新增或改動任何設定欄位時必須同步更新該文件**。以下只列設計要點：
- `llm.base_url / api_key / model / supports_tools`（任何 OpenAI 相容 API；不假設特定後端）
- `search.provider`：`searxng_http`（預設，直接打 SearXNG JSON API）、`mcp`（接 SearXNG MCP server，選用）、`none`（不搜尋，只靠結構化 API）
- `fetch.provider`：`builtin`（預設，httpx + 無相依的 HTML 轉文字，保留標題/清單/連結）或 `mcp`（v0.1.1 起；減少維護面）
- `search.mcp.url`（預設 `http://127.0.0.1:3000/mcp`；容器未映射到本機時改填該主機位址，見 §11）
- `budget.max_searches`（預設 8）、`budget.max_fetches`（預設 10）：SearXNG MCP 有 20 req/min 限流
- `identity.history_days`（預設 90）、`identity.min_stable_revisions`（預設 3）：見 §4.1
- `identity.min_sources`（預設 2）、`identity.allow_tier3`（預設 false）
- `allowlist / denylist`
- `cache.cert_ttl_hours`、`cache.identity_ttl_hours`、`cache.anchor_refresh_days`
- `net.timeout_s`
- `full_log.enabled / dir / max_bytes`：完整資料日誌（預設關閉；MCP 進出、LLM 每輪含 reasoning、搜尋往返、L0、裁決；
  `full-YYYYMMDDHHMMSS.log`，超過 1MB 換檔；管理介面 Config 分頁可即時切換）
- `prompts.dir`：prompt 覆寫檔目錄。預設在 `prompt_defaults/*.md`；`agent_*` 下次驗證即生效，`mcp_*` 需重啟 server
- `search.call_timeout_s`（單次搜尋/抓取上限）、`budget.max_total_s`（整次驗證總時限，到期回 UNVERIFIABLE）
- `server.progress_events / heartbeat_s`：MCP progress 通知與心跳，讓支援 resetTimeoutOnProgress 的 client 不會 -32001；
  心跳之所以安全是因為所有等待皆有上限且受總時限約束（2026-09-16 定案）
- `server.transport / host / port`（MCP server 本體，預設 stdio；http 時端點為 `http://host:port/mcp`，預設 8766；CLI 旗標優先於設定檔）
- `admin.host / port`（管理介面，預設 8765）
- 輸出語言：**跟隨呼叫方輸入語言**，無需設定

## 6. 快取（三種，生命週期不同）
| 快取 | 內容 | 失效 |
|---|---|---|
| 平台錨點 | 內建種子：github.com、huggingface.co、pypi.org、npmjs.com 等根網域與預期憑證發行者 | 隨版本更新 + 背景刷新 |
| 憑證 | 每主機的指紋、發行者、Organization、到期日 | 憑證到期或 TTL，取先到者 |
| 身分圖 | 專案 → 公司 → 別名 → 官方網域/倉庫，附證據 URL | TTL；管理介面可手動作廢 |
儲存於 `state/` 的 JSON 檔（cert_cache.json、identity_cache.json）；命中結果要在輸出 `cache_hits` 標示。

## 6.2 Typed facts（v0.2.0 起，提案三第一步）
- 結構化工具（wikidata_lookup、wikipedia_history、github_info、huggingface_info、package_registry、wayback_first_seen）的回傳由程式存成
  **紀錄**（R#），並攤平成帶編號的 **facts**（`F12 github.repo.full_name = ggml-org/llama.cpp`）。LLM 看到的是 facts 清單，
  證據以 `facts: ["F12"]` 引用；規則只確認 ID 存在，來源、種類與引文全由紀錄改寫，LLM 寫的 source／quote 不採用。
- 網頁（fetch_url）仍要逐字 quote。同一 URL 的 API 紀錄與網頁分開存，永不互相覆蓋；頁面引文只比對頁面，紀錄只比對紀錄。
- 紀錄可用任何一個網址引用（api.github.com/repos/o/r ≡ github.com/o/r、registry.npmjs.org/x ≡ npmjs.com/package/x…，見 `evidence.norm_url`）。
- 仍接受舊式「source + quote」的結構化證據（以事實錨定比對），但新提示要求用 fact ID。
- 其餘部分見 §6.3。

## 6.3 身分邊與缺口驅動的固定補查（提案三後半段，2026-09-24 定案）
目標：LLM 只做機器判斷不了的語意工作；能機械完成的查詢由程式按缺口固定執行，減少 LLM 自由發揮。

**邊（edges）**：規則引擎每次裁決後，同時輸出已成立與缺少的邊（`established_edges`／`missing_edges`），缺口附「已有幾家／需要幾家」。
| 邊 | 適用目標 | 成立條件（沿用既有規則，不新增門檻） |
|---|---|---|
| `TARGET_CHECKS` | 全部 | L0 必要檢查完成且無致命失敗 |
| `PROJECT_TO_DOMAIN:<domain>` | 一般網站 | 目標網域由 ≥ min_sources 個獨立家族成立 |
| `PROJECT_TO_ORG:<platform>:<owner>` | 託管平台、套件 | 路徑 owner 成立為官方 org／套件 |
| `PACKAGE_TO_REPOSITORY` | PyPI、npm | 簽章 provenance 指出建置 repo |
| `PROJECT_NAME_MATCH` | 全部 | 證據提到呼叫方給的專案名稱 |

**流程**：
1. L0（不變）。
2. **固定預查**（不經 LLM）：依目標類型決定缺哪些邊，程式直接執行對應查詢：
   - 缺 `PROJECT_TO_DOMAIN`／`PROJECT_TO_ORG` → Wikimedia 固定查詢，候選名稱依序最多 3 個：呼叫方 project → 套件／repo 名稱 →
     第一個被接受的 Wikidata 條目所記的開發者（P178）。條目接受條件是確定性的：標籤或別名與候選名稱正規化後相同，或其官網／原始碼庫
     指向目標。Wikipedia 只經該條目的 enwiki 連結取得，不猜標題。查不到記 `WIKIMEDIA_NO_MATCH`，不重試換標題。
     接受分級（2026-09-24）：官網／原始碼庫指向目標的條目直接採用；其次是**標籤**相同，再其次才是**別名**相同（只在沒有任何標籤相同時才看別名：
     「7-Zip」是程式的標籤，同時是 7z 格式條目的別名）；同一級只有一個才採用；**同一級多個同名條目一個都不採用**，
     紀錄保留（可用 fact ID 引用），候選清單放進給 LLM 的說明，由 LLM 判定哪一個是本專案並引用（`WIKIMEDIA_AMBIGUOUS`）。
     LLM 選擇本身不產生票，票仍依 §4.1 的時間條件由紀錄內容決定。
   - 平台目標缺 `PROJECT_TO_ORG` → 取 owner（與 repo）的平台紀錄。
   - 套件目標缺 `PACKAGE_TO_REPOSITORY` → registry metadata 與 provenance（既有）。
   預查得到的紀錄轉為以 fact 引用的確定性證據；候選網域／org 由紀錄推出（官網、原始碼庫 owner、目標本身）。
3. 以預查證據跑規則引擎。**已能裁決（TRUE／FALSE）就不啟動 LLM**；票數規則與 LLM 引用時完全相同，所以不增加長尾風險。
4. 仍有缺口才啟動 LLM，並告知：已有哪些紀錄與 facts（不必重查）、缺哪些邊、各缺幾家。LLM 負責別名、改名、收購、產品與公司關係、
   以及網頁是否真的支持某條邊；它的證據與預查證據合併後再裁決。
5. 輸出：`machine_readable.subjects[]` 帶 `established_edges`、`missing_edges`；原因代碼由缺口直接產生（取代 v0.2 由備註字串推導的做法）。

- **身分快取與規則版本**：`rules.RULES_VERSION` 屬於快取指紋的一部分。規則改變「會確立什麼」時必須遞增，舊規則下確立的身分因而失效並重新驗證
  （2026-09-24：VLC 在修正前被錯誤確立並寫入快取，修正後仍從快取讀回，因此加入此機制）。

## 6.4 鏡像與下載 CDN（官方委託的下載主機，2026-09-24 定案）
目標網址在鏡像／CDN 上時，只在官方「親自」指向**這個檔案**時放行（`OFFICIAL_DELEGATION` 邊，TRUE 信心上限 0.8，附 `OFFICIAL_DOWNLOAD_HOST`：請比對官方檢查碼）：
- **完全相同的檔案連結**：從已確立官方網域抓回的**頁面**（工具抓的，非 LLM 轉述）中，有 `<a href>` 指向與目標**逐字相同**的檔案網址。
  只提到主機、或連到同主機的其他檔案都不算——以擋「官網順帶連到第三方元件」的誤判；專案名稱比對邊仍須成立。
- **同名檔案轉址**：官方網址轉址到其他主機時，需同時滿足 (1) 轉址前後檔名相同、(2) 轉址鏈任一跳的 query 或 path 都沒有夾帶另一個網址
  （防開放轉址 `/out?url=https://evil`）。不滿足者維持 UNVERIFIABLE（`REDIRECT_TO_UNESTABLISHED_HOST`），不判 FALSE（鏡像本來就會轉址）。
- 只列鏡像主機的官方鏡像清單不讓整個主機過關。我們不下載檔案、不驗雜湊；雜湊比對留給使用者。
- 固定預查會主動抓已確立官網的首頁與其同網域 download 頁尋找上述連結（頁面只存為 page，不引用、不給 LLM）。
- **SourceForge（窄版，2026-09-24 定案）**：SourceForge 當作鏡像網路處理，**不是**像 Wikimedia 那樣的投票來源。
  - 固定預查取 SF REST 專案紀錄（`sourceforge.net/rest/p/<slug>`：名稱、external_homepage、建立日期、開發者），存為 `sourceforge` 紀錄；
    引用只為了名稱比對（7-Zip ↔ `sevenzip`），**SF 家族的任何紀錄或頁面都不投票**（專案管理員自填，或 SF 自己的鏡像）。
  - SF 專案成立為官方（`PROJECT_TO_ORG:sourceforge:<slug>`）只有兩條路，都以「官網已**獨立**確立」為前提、不會反過來自指：
    (1) 官網頁面有**逐字相同**的檔案連結（同上）；(2) **雙向連結**：SF 紀錄的 homepage 指向已確立官網，**且**該官網抓回的頁面連到
    `sourceforge.net/projects/<slug>`（或 `/p/<slug>`、`downloads.sourceforge.net/project/<slug>`）。TRUE 信心上限 0.8，附 `OFFICIAL_DOWNLOAD_HOST`。
  - SF 自動鏡像（無 REST 紀錄，專案頁寫著「exact mirror of the X project … SourceForge is not affiliated with X」）→ `SOURCEFORGE_MIRROR`，
    UNVERIFIABLE；只有官網逐字連到該檔案時例外。查無專案 → `SOURCEFORGE_PROJECT_NOT_FOUND`。
  - 所以官網只有 Wikimedia 一家（Audacity、KeePass）時 SF 幫不上忙，這是預期行為。

## 6.1 規則寫死 vs. LLM 自主：責任分工
原則：**密碼學與結構性事實、安全不變量歸規則；語意推理歸 LLM。LLM 提議，規則驗證。**
最低目標模型：Qwen3.8 27B 等級（能力足以做實體解析與證據判讀）。

| 歸規則（LLM 不得覆寫） | 歸 LLM（自由發揮） |
|---|---|
| TLS 信任鏈、到期、SAN 吻合 | 決定搜什麼、搜幾輪、下一步查哪裡 |
| 同形字、punycode、子網域濫用、重導終點 eTLD+1 | 實體解析：產品 ↔ 公司 ↔ 品牌/前端別名 ↔ 收購/改名（如 LM Studio / Element Labs / Bionic） |
| 對 AI 說話的文字 → FALSE | 判斷某來源**是否真的支持**該主張（同時提到兩個名字 ≠ 支持） |
| 來源計數與 Tier 門檻（算術）；來源家族折疊；平台使用者內容 = Tier3 | 為未知來源提議 Tier2/3 |
| 平台根網域從 official_domains 剔除；自我宣稱以 owner 路徑判定 | 決定哪些帳號屬於同一個開發者（規則只驗其一致性） |
| Tier1 白名單固定 | 憑證 Organization 與開發者名稱的模糊比對（"Element Labs, Inc." vs "Element Labs"），須附連結證據 |
| L0 失敗 → 不得 TRUE |
| 解析出的產品身分與呼叫方 `project` 明顯不同 → 不得 TRUE（降為 UNVERIFIABLE） | 撰寫 reason（呼叫方語言）與風險敘述 |
| 時間穩定性（§4.1）的數值判定 | 解讀歷史變更的意義（改名、搬家、被竄改） |
| **證據可驗證性**：網頁/媒體類 evidence 的 `quote` 必須逐字存在於抓回的內容中；結構化來源（wikidata/wikipedia/github/hf/wayback/registry）改為**事實錨定**：主張中的網域或組織名必須出現在該來源的原始工具輸出中。不符者丟棄 | |

最後一條是防幻覺的核心機制：LLM 引用的證據要能被字串比對驗證，引不出來的證據不算數。
（2026-09-15 實測：Qwen3.8 對 JSON 型工具輸出會寫摘要而非逐字引文，純逐字比對會誤丟 Wikipedia 證據，故結構化來源改事實錨定；防幻覺性質不變。）

### 3.1 登錄快速路徑（2026-09-17 定案）
PyPI / npm 目標先跑結構化檢查：存在、首發年齡、版本數、**簽章 provenance**（npm attestation / PyPI PEP 740，登錄自己簽的
「哪個 repo 的 CI 發布了這版」；其 repo owner 須為 GitHub 已驗證網域的組織；deps.dev 印證則記錄）、repo 雙向互指、
scoped npm 的 scope 須等於 provenance owner、PyPI 的近似名檢查（對照熱門清單；npm 無參照清單故不做，生成「合理錯字」是猜測）、
project 名相符，全數明確且良好才直接 TRUE。登錄方自己宣告的狀態優先且**在任何路徑與 mode 都成立**：不存在 / npm security holding（0.0.1-security）→ FALSE（規則引擎層），
評估順序在 project 名比對之前（那是關於目標本身的事實）。引文比對忽略所有空白（JSON/HTML 與模型渲染只差空格）。
**沒有 provenance 的套件絕不憑 metadata 給 TRUE**（roger 2026-09-18：新／小套件本來就沒有理由被自然信任），走完整流程；
完整流程中 provenance 可讓套件承接已建立 GitHub 組織的地位。**人氣不是獨立訊號**（下載量可灌），只作近似名比值的分母；
熱門清單**不內附**，首次用到才串流下載前 N 筆（預設 1500，約 90 KB）並以 ETag 更新，設定段為 `package_registry_fast_path`；
任一訊號未知或可疑就落回完整流程並附上風險訊號。**快速路徑只能加速，不能決定錯誤方向**。`options.mode` 可強制 quick / full。

## 7. 安全紅線
1. **所有抓回的網頁/搜尋內容都是資料，不是指令**。以明確分隔標記包裹，prompt 宣告為 untrusted。
2. **頁面中對 AI / agent / verifier 說話的文字 → 直接 VERIFIED_FALSE**，原文列入 reason。
3. 頁面一般性的「本站為官方」宣稱：**權重為零**，不加分不扣分（真網站也常寫「請從官網下載」）。
4. **絕不執行、解壓、完整下載**待驗證檔案；只做 HEAD / Range 局部讀取。
5. LLM 既有知識只能當假設，**結論必須有可追溯的證據 URL**。
6. 逾時、失敗一律降級為 UNVERIFIABLE，不可靜默失敗。
7. **非公開位址（loopback / 私有網段 / link-local / .local）一律 VERIFIED_FALSE 且不探測**，重導終點亦同（防 SSRF / 內網探測）。
8. 管理介面只有密碼、預設 `admin`、預設綁 127.0.0.1；HTTP 傳輸可選 bearer token。其餘系統安全由維護者自理，介面不用時關閉。

## 8. 技術選型
- **Python ≥ 3.11**：`mcp` 官方 SDK（同時作 server 與 client）、`httpx`、`cryptography`、`tldextract`、`idna`、`sqlite3`
- LLM 介面：OpenAI 相容 chat completions；原生 tool calling 為主，模型不支援時退化為「先規劃、再結構化逐步執行」
- 傳輸：stdio（預設）+ Streamable HTTP
- 管理介面：FastAPI + 單頁 HTML：設定、白/黑名單、三種快取檢視與作廢、歷史紀錄、手動測試
- 可攜：`uv` / `pipx`，單一 `config.yaml`，Windows / macOS / Linux
- **Prompt 一律英文**；輸出語言跟隨呼叫方

- 儲存：**純 JSON 檔，不用資料庫**（roger 的可攜原則，2026-09-17）。`state/` 只放可重建的快取與設定；`log/` 放所有紀錄
  （full/、history/、health.json、server/、admin/），可能含隱私，可整個刪。相對路徑以 config.yaml 所在資料夾為基準。

## 9. 目錄結構（預定）
```
urlverify_mcp/
  server.py          # MCP 入口
  agent/             # 調查迴圈與英文 prompt
  checks/            # L0：tls.py, dns.py, redirects.py, homoglyph.py, ct.py
  identity/          # L1：graph.py, sources.py（分級）, platforms.py（GitHub/HF/registry）
  providers/         # llm/（openai_compat）, search/（mcp_client, searxng_http）
  rules.py           # 裁決規則引擎
  cache/             # anchors.py（種子）, store.py
  admin/
tests/fixtures/      # 已知真/偽案例迴歸集，每個檢查至少一真一偽
config.example.yaml
```

## 9.1 審視後補正的實作不變量（2026-09-19）
- 引文只比對被引用的確切來源 URL；不得借用同主機其他頁面、搜尋摘要或其他 API 實體的內容。
  結構化來源由工具層記錄類型，不接受 LLM 自行宣告。非逐字摘要必須所有事實錨點吻合，計票只使用已核實錨點。
- `scheme`、`public_address`、`dns`、`tls`、`redirects` 必須完成且通過；錯誤、跳過、缺少結果均不得 TRUE。
  DNS／API 暫時失敗是未知，不當成仿冒或套件不存在；已證實的非公開位址仍是 FALSE。
- 每次本機對非信任 URL 的 HTTP 連線均檢查 DNS 全部答案、固定連到公開 IP，保留原始 Host／TLS SNI；每個重導重新檢查。
  TLS 診斷連線也沿用已檢查位址。已被 L0 拒絕的目標不再抓頁面。
  外部 MCP 抓取服務先做本機重導檢查，但服務端仍須自行限制出站位址，本機不能保證其 DNS 視圖與後續抓取行為。
- 平台錨點不替重導終點的不同 owner 背書；CDN 上猜出的檔名不得取代 owner 身分。
- npm／PyPI 的版本狀態與 provenance 跟隨目標指定版本／檔案，不拿最新版替代；無明確檔案時 PyPI 仍只查所選版本的代表檔案，不能推論所有檔案均有 provenance。
- 只有 TRUE 的身分結果寫入快取，避免把別的產品身分存到呼叫方專案名稱下。身分快取命中不延長原本 TTL；證據政策改變、舊快取格式均重新調查。快取不能提高先前裁決的信心上限。
  信心加分依獨立來源家族數，不依同一家媒體的引文數。
- 已知官方網域清單可能不完整；不在清單內本身不是明確反證，應回 UNVERIFIABLE。
- Tier3 時間升級與注入即 FALSE 維持原狀。冷卻期仍不改 verdict／confidence；依使用者確認，訊號改為 `RELEASE_COOLDOWN_PERIOD:<已發布小時數>`，期間內或時間不明皆提示呼叫端先告知風險、取得使用者確認後才可下載／安裝。確認由呼叫端執行，工具不聲稱已阻擋下載或取得同意。

## 10. 工作規範
- **未經指示不得直接修改程式碼。** 發現問題先回報與分析，提出修法，等指示再動手；這包括「順手修」與「小修」。
- **不做任何針對性的硬化處理。** 不為單一案例、單一網站、單一套件加特例或補丁；只做能一般化的規則，並說明它為什麼一般化。
  若一個案例只能靠特例解決，回報這個事實，讓維護者決定。
- **修正的兩條判準**（2026-09-24，roger 定）：一、盡量走通則或原則性修正，盡量少用針對性固化（保留字表、單一網站白名單之類都算固化）；
  二、以不增加長尾風險為優先：一個修正若會讓某些罕見情況從「無法確認」變成「誤判 TRUE」，寧可不做。拿不準的細節依這兩條自行判定，並在回報中說明判定理由。
- 先討論、再寫碼；重大設計變更先更新本文件。
- 每個新檢查都要附 fixtures 迴歸案例。
- 回覆與文件用繁體中文；程式碼、識別字、prompt 用英文。

## 11. 本機環境與啟動
三個外部端點皆以 `config.yaml` 為準；`.\start.ps1 env`（或 `urlverify-mcp check-env`）可一次探測：
- **LLM**：任一 OpenAI 相容端點。本機使用 LM Studio 內建本地 API `http://127.0.0.1:1234/v1`（穩定埠位，model id `qwen3.8-27b-uncensored`）；
  **不要直接連它底下的 llama.cpp backend 程序**（每次啟動隨機埠位）。啟動/停止屬於使用者操作，agent 不自行啟動。
- **SearXNG MCP**：mcp-searxng 容器（isokoliuk，LITE 模式），Streamable HTTP，預設 `http://127.0.0.1:3000/mcp`；
  以 docker compose 部署於本機或任一可達主機。工具：
  `searxng_web_search(query)`、`web_url_read(url)`、`searxng_search_suggestions(query)`、`searxng_instance_info()`（無 category / time_range 參數）；
  限流 20 req/min（回應標頭 RateLimit-Limit）。
- **SearXNG HTTP**（備援路徑，`search.provider: searxng_http`）：JSON API，預設 `http://127.0.0.1:8888`。
- Python ≥3.11；建議以 uv 管理（`uv sync --extra dev`）。無 uv 時用系統 python：
  `python -m venv .venv && pip install -e ".[dev]"`（Windows 直接跑 `.\start.ps1`，會自動偵測並重建非本平台的舊 `.venv`）。
  **`mcp` 必須釘 `<2`**（2.x 改名 FastMCP→MCPServer、client API 亦變）。
- Windows 啟動腳本 `start.ps1`：`.\start.ps1 admin|serve|verify|test|env`（預設 admin，http://127.0.0.1:8765）；
  `start.bat` 是它的雙擊包裝（參數透傳、失敗時暫停視窗）。進度輸出走 stderr，`serve` 模式的 stdout 保持乾淨供 MCP JSON-RPC。
- 開發指令：`uv sync --extra dev`、`uv run pytest`（離線單元測試 + 需網路的假 LLM 管線測試）、
  `uv run pytest tests/test_live.py --live -s`（需 LLM）；Windows 等價於 `.\start.ps1 test`。

## 12. 踩過的坑
- **MCP client 的死端點會以 `CancelledError` 浮出**（anyio cancel scope），`except Exception` 攔不到，且在被取消的 task 內
  `aclose()` 會撞「exit a cancel scope that isn't current」。`task.cancelling()` 分不出內外取消。
  定案：`providers/search.py` 把 transport 的 context manager 放進專屬 worker task，呼叫端經 Future 取得 session
  或普通的 `SearchUnavailable`（2026-09-15，來自另一套 LLM 的 bug 報告，已核實並修復）。
- **探測 vs 觀察**（2026-09-17 定案）：外部依賴不做自動探測；每次真實呼叫回報成敗到 `health.py`（持久化於 `log/health.json`），失敗在 stderr 印
  `!! DEPENDENCY …`、full log 記 `dependency_failure`、結果帶 `degraded`。健康表**只是報告，永遠不是啟用與否的判準**。
  `check-env` 只在使用者手動觸發時跑，且只用各服務最輕的端點；管理頁載入時不打任何外部服務。
- **Wikidata 的 `mul` 標籤**（2026-09-24）：Wikidata 2024–25 起把許多條目的標籤／別名移到語言無關的 `mul`（例：7-Zip Q215051 沒有 `en` 標籤），
  只讀 `en` 會拿到空標籤、名稱比對失敗（`WIKIMEDIA_NO_MATCH`）。現在讀 `en|mul`，標籤以 `en` 優先；別名原本根本沒有要（props 漏了 `aliases`），一併修正。
- **待議：專案名稱比對可否採用已成立網域自己的頁面**（2026-09-19 記錄，尚未動）：Vulkan SDK 案例中 lunarg.com 由 3 個獨立來源成立、
  LunarG 的 GitHub org 也成立，但被計入的引文只提「LunarG」，沒有一條提「Vulkan SDK」，`_project_matches` 擋下 TRUE。LLM 抓到的
  lunarg.com 自家頁面明確寫著 Vulkan SDK、引文驗證通過，卻因「自我宣稱」在名稱比對前就被剔除。自我宣稱不能用來**成立**網域是對的；
  但網域已獨立成立之後，用該網域自己的頁面回答「這個站是否在發佈 host 問的產品」性質不同：假站永遠成立不了網域，放寬不會讓假站得利，
  殘餘風險只剩 host 把產品歸錯廠商（今天已存在）。長尾比「分段比對」小得多。是否採用由使用者決定；未經指示不得實作。

## 13. Source 介面（v0.2.0，2026-09-24 定案）
- 解析與驗證分開：`source/parse.py` 只做確定性解析（旗標白名單，不認得的旗標回 `UNSUPPORTED_FLAG:<flag>`，絕不忽略），
  `source/resolve.py` 查 registry 決定實際版本、從 winget-pkgs manifest 取安裝檔網址，`source/run.py` 逐一交給既有的 per-URL pipeline 再彙總。
- 絕不猜：裸名、未知指令、多 index 並存（`--extra-index-url`、`--find-links`、`--no-index`）、requirements／lock 檔、本地路徑、`&&`／`;` 串接
  一律回固定代碼、不驗證。不加 `channel=` 參數：指令語法本身就是逃生門。
- registry 優先序：指令旗標／環境變數 → `source.registries` 設定 → 公開預設。不自動讀本機 pip.conf／.npmrc（MCP 走 HTTP 時 server 與 host
  可能不是同一台）。非公開 registry 回 `REGISTRY_UNSUPPORTED`，不以公開來源冒充。
- 會改變實際安裝對象的語法一律解析到真正的對象：npm 別名 `x@npm:y` → y；`user/repo`、`github:` → GitHub repo；pip `name @ url`、`git+` → 該 URL／repo。
- WinGet：以 PackageIdentifier 取官方 manifest（分支 master）的 InstallerUrl，依 `--architecture`／`--scope`／`--installer-type`／`--locale`
  或 artifact 內的架構字樣篩選；多個不同網址全部驗證（上限 4）。manifest 那一行作為 tier‑1 種子證據（seed）交給規則引擎驗證；
  winget-pkgs 的 owner（microsoft）不是產品 owner。查詢字串若恰為既存 ID 視同 ID（winget 本身要嘛裝它、要嘛報多筆衝突）。
- 腳本管線（curl | sh、irm | iex…）：全句恰有一個遠端位址才受理，驗該腳本網址；腳本之後下載的東西不在範圍內（SCRIPT_MAY_DOWNLOAD_MORE）。
  混入套件管理器或多個網址即拒絕。
- Homebrew（v0.3.x）：formulae.brew.sh API；同名 formula 優先於 cask（與 brew 本身相同），`--cask`／`--formula` 可指定；只接受官方 tap。
  cask 驗下載網址（依 artifact 內的架構篩 arm64／Intel），formula 驗 Homebrew 據以建置的上游原始碼網址。API 記錄那幾行作為種子證據（brew.sh 家族）。
- Scoop：只接受 ScoopInstaller 官方 bucket（main、extras、versions、java、nonportable）；未指明 bucket 視為 main，main 沒有就回
  `SCOOP_BUCKET_AMBIGUOUS` 請呼叫方指明，不去其他 bucket 猜。`app@version` 不受理（Scoop 會臨時產生 manifest，沒有審核過的內容可查）。
- Go：已知託管平台的模組路徑直接對應 repo；自訂網域依 `go-import` meta，**驗證模組網域本身**（Go 的信任模型是網域擁有者決定程式碼位置），
  repo 位置只記錄。
- Flatpak：只接受 Flathub（未寫 remote 視為 Flathub 並提示）。固定預查取 Flathub appstream 與開發者驗證狀態存成 `flathub` 紀錄：
  已驗證的 app 記錄驗證網域（website 方式）或 appstream 官網（manual／帳號方式），套用「平台已驗證連結」規則（該網域須已獨立確立為專案官網）；
  **未驗證的 app 不記官網**（社群打包者自填的網址不得替任何網域投票），結果 UNVERIFIABLE 並附 `FLATHUB_UNVERIFIED`。
  平台目標的專案名稱比對可採用「目標自己那筆平台紀錄」的顯示名稱（Flathub app ID 常不含名稱），他人紀錄不算。
  發行管道的 manifest 倉庫（github.com/flathub、Homebrew、ScoopInstaller…）與該發行管道同一家族，不另計一家；
  未驗證的 Flathub app 不接受任何 Flathub 家族的支持（Flathub 對此類 app 明示「非開發者所屬」）。（VLC 實測發現，2026-09-24）
- 發行版套件（apt／dnf／pacman，2026-09-24）：MCP 看不到主機的倉庫設定，所以由呼叫方把套件管理器自己的來源報告放進 `options.origin`
  （`apt-cache policy`、`dnf info`、`pacman -Si`，在要安裝的那台機器上執行）。判定純確定性、不連網：
  候選版本只來自官方 archive（*.debian.org、*.ubuntu.com，或 `source.distro_archives` 加列的鏡像）／官方 repo ID → TRUE 並附 `DISTRO_PACKAGE`
  （發行版以上游原始碼建置並簽章，屬發行版官方管道）；apt 第三方 repo → 以該 repo 網址走網址驗證；官方與非官方混合 → UNVERIFIABLE；
  未附報告 → FIX_INPUT 並給出該跑的指令。不以路徑或 component 名稱猜「是不是發行版鏡像」（廠商 repo 與 PPA 也長得一樣）。
- 支援但尚未驗證的生態系（cargo、gem、composer、choco、conda、zypper、apk、AUR、snap、ollama、Install-Module、msstore、其他容器 registry、非 Flathub 的 flatpak remote）
  回 `ECOSYSTEM_NOT_YET_VERIFIED:<eco>` 並列出解析結果；`cargo install --git` 走 git 驗證。apt 家族優先度最低（信任模型是發行版簽章）。
- next_action：FALSE → DO_NOT_PROCEED；呼叫方可修正的代碼 → FIX_INPUT_AND_RETRY；其餘非 TRUE → INFORM_USER_AND_CONFIRM；
  TRUE 但信心 < 0.8 或帶警示（腳本會再下載、冷卻期、雜湊檢查關閉、自我發佈上限…）→ INFORM_USER_AND_CONFIRM；否則 PROCEED。
- v0.2 的結果代碼部分由 checks／risk_signals／engine_notes 推導（`source/run.py: result_codes`）；v0.3 的身分圖重寫改由規則直接產生。
- 開發工具：設 `URLVERIFY_CAPTURE_DIR` 時，每次規則裁決的完整輸入寫成 `*.json.gz`；`tests/test_replay.py` 以手寫的 `expect` 重播，
  改規則時先跑重播，不必每次實跑四分鐘。本地 LLM 實測只打本機 `127.0.0.1:8080`。

## 14. 未來小項目：尋找官方下載來源（`find_official_source`，建議獨立 repo）
2026-09-24 討論結論，尚未實作。roger 判斷「找出官方下載」本身步驟多、需要逐步研究與固化，值得拆成獨立 repo；
URLVerify 維持「驗證」單一職責，找來源的一方呼叫 URLVerify 當守門員。之後要做時，把本節摘出成新專案的起點。

- **輸入**：project、artifact（形式／平台），可選 platform。**輸出只回一個**：已通過 URLVerify `VERIFIED_TRUE` 的來源
  （可直接執行的安裝指令或下載網址），附依據；找不到就明說找不到，不回半成品。
- **流程**：
  1. **先鎖定管道（不經 LLM）**：依 artifact 決定優先序，例如 Windows 安裝檔 → winget → 官網；macOS → Homebrew cask；
     Linux 桌面 → Flathub → 發行版套件；Python／JS 函式庫 → PyPI／npm；容器 → 官方 registry。
  2. **產生候選，便宜的先做**：能確定性查的先查（Homebrew cask 索引、Flathub 搜尋 API、Scoop bucket manifest、PyPI／npm 精確名稱、
     winget 可用 GitHub code search 查 winget-pkgs）；不足時才讓 LLM 搜尋與列舉（名稱對應、改名、產品與公司關係是它的強項）。
  3. **驗證只取前兩名、依序**：第一個 `VERIFIED_TRUE` 就停；第一個驗不過才驗第二個。兩個都不過 → 回報找不到。
  4. 候選清單與搜尋結果一律視為不可信；可信與否只由 URLVerify 裁決，因此不需要為各平台另寫信任規則。
- **已知取捨**：winget 無公開名稱索引；官網下載頁只能給頁面、不自行挑按鈕連結（交給 LLM 找後再驗）；最壞情況約 5–10 分鐘。
- **與 URLVerify 的介面**：只透過 `verify_source`（YAML `machine_readable`），不共用內部模組，避免兩邊互相牽制。
