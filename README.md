# Trip Planner

用 Claude Code + Google Maps API 規劃旅行，自動生成靜態網站部署到 GitHub Pages。

AI-native 自動排程的分階段改造、架構決策與驗收門檻記錄在
[`docs/planning-kernel-roadmap.md`](docs/planning-kernel-roadmap.md)；canonical
plan、PlanPatch、原子寫入與 rollback 的保證記錄在
[`docs/safe-mutation-contract.md`](docs/safe-mutation-contract.md)；AI snapshot、
proposal、budget、progress 與 human checkpoint 邊界記錄在
[`docs/ai-repair-loop-contract.md`](docs/ai-repair-loop-contract.md)；Phase 3
排程 problem、candidate、score、failure、safe staging 與 solver selection gate 記錄在
[`docs/scheduling-contract.md`](docs/scheduling-contract.md)；provider request、
static policy、memory/disk evidence分流、雙時鐘與promotion gate記錄在
[`docs/facts-provider-contract.md`](docs/facts-provider-contract.md)。

## 功能

- **自然語言互動式規劃** — AI 先理解固定交通、住宿偏好與景點需求，再提出可比較的行程
- **真實資料驅動** — Google Places API 營業時間 + Routes API 交通時間
- **自動生成網站** — 行程表、地圖、行事曆下載、訂位清單、行李清單
- **GitHub Pages 部署** — 一鍵部署，手機隨時查看

## Setup

### 1. Clone + 環境

```bash
git clone <your-fork-url> trip-plan
cd trip-plan

# Python 虛擬環境（需要 uv）
uv venv
pip install -r requirements.txt

# direnv（管理環境變數）
cp .envrc.example .envrc
# 編輯 .envrc，填入你的 Google Maps API Key
direnv allow
```

### 2. Google Maps API Key

到 [Google Cloud Console](https://console.cloud.google.com/apis/credentials) 建立 API Key，啟用：
- **Places API (New)**
- **Routes API**

API Key 的「API restrictions」需包含這兩個 service。舊版 Directions API 自 2025/3 起已無法新啟用。

填入 `.envrc`：
```bash
export GOOGLE_MAPS_API_KEY="your-key-here"
```

### 3. 安裝 Claude Code Skill

```bash
# 建立 skill 目錄
mkdir -p ~/.claude/skills/trip-planner

# 複製 skill 檔案
cp skill/trip-planner.md ~/.claude/skills/trip-planner/SKILL.md
```

### 4. GitHub Pages

到你 fork 的 repo → Settings → Pages → Source 選 `gh-pages` branch。

## 使用

在 Claude Code 裡輸入 `/trip-planner` 開始規劃，或直接描述你的旅行需求。

### 資料模式護欄

目前完整、可直接給使用者與 agent 操作的流程是 **legacy 模式**（七個核心檔案：
`trip.json`、`itinerary.json`、`reservations.json`、`todo.json`、`info.json`、
`packing.json`、`places_cache.json`）。`plan.json` canonical kernel 是 developer
preview：它以 `plan.json` 加五個 sidecar（後五個檔案）取代前兩個檔案；現有
renderer、validator 與部分讀取工具可相容讀取，但 legacy writer 會刻意拒絕修改
已 migration 的 trip。不要直接編輯 canonical JSON；只能經已支援的 `TripStore` /
`PlanPatch` 路徑操作。Phase 5 的 `tripctl` CLI 尚未提供，除非已明確接受 developer
workflow，否則不要 migration 真實 trip。

Phase 4.4 的 Places profile / opening-hours runtime目前也是developer
boundary：核心只有injected transport，沒有內建credential或live CLI。
只有fresh、無衝突的`currentOpeningHours`可限制完整活動時段；
`regularOpeningHours`、stale、conflicted或missing資料只會要求重新確認。
舊`check_hours.py`使用regular cache，因此永遠只輸出advisory，不代表當日
確定營業。

Phase 4.5 的產品方向是「固定／暫定交通 + 住宿錨點 + 每日行程共同最佳化」。使用者
只要自然描述已知資訊，不需要填規格表；AI 會把明確內容寫入 process-local typed
draft，沒有提到的內容保持 unknown。機票、渡輪、鐵路等只接受使用者輸入的抵達／
離開時間或時間範圍與地點，不提供航班搜尋。住宿可以是飯店、民宿、Airbnb、地址、
座標或概略區域；沒有住宿資料時，AI 只能追問或提出候選，不能捏造住宿。

Phase 4.5A 的公開 binding 一律是 candidate + unverified。使用者若清楚說「選這間」
或「已訂」，AI 會保留成附 opaque source reference 的 reported decision claim，
不會遺失語意，也不會把 claim 冒充成權威 selected / fixed / booked。

Phase 4.5B 以獨立 runtime sidecar 將候選投影成 comparison-ready view：位置 identity
與 route observation 綁 exact `EvidenceSnapshot`；route 另須保留原 request receipt，
且 receipt 的 endpoint observation/value 仍與目前 snapshot 相同，才可使用
duration/distance。missing、stale、conflicted、缺 receipt 或 endpoint drift 都只回
`needs_verification` 與去重後的 refresh request，不會改寫 4.5A candidate。比較欄位
只表達涵蓋晚數、住宿類型、位置精度、價格是否已知與 evidence readiness；「哪間最
好」不在 4.5B 裡決定。

Phase 4.5C 新增純 runtime 的 joint recommendation sidecar。Caller 先把已知的固定
抵離交通、已訂活動、住宿候選錨點、Routes 與 current opening hours 組成同一
`ComposedTripState`／exact `EvidenceSnapshot`，再為各住宿配置建立 detached
`ScheduleProblem`。只有共同 route slots、solver required arcs 與 hours 都可由同一
snapshot 重播時，才依固定 lexicographic vector 排出「優先 review」；missing、stale、
conflicted、缺 receipt、reported booking claim、snapshot drift 或同分都不會產生
winner。價格在有可比較且 scope 一致的 price evidence 前明確排除。結果永遠
`supports_authoritative_use=false`，不含 `PlanPatch`、decision promotion 或 canonical
writer；真正的住宿確認／套用仍由 Phase 4.5D 負責。

4.5B 另提供純 offline 的 SerpApi hotel response normalizer，嚴格區分 metadata
status、top-level error、empty success 與 partial result；query/search ID/token/位置
與價格原值不進 safe view，所有結果仍只產生 provider-discovered candidate +
unverified。Result 只是 non-provenance DTO；status 與 process-local diagnostic ref
不可作 authorization、cache、evidence、receipt 或住宿決策依據。此 normalizer 沒有
HTTP、cache、`HOTEL_OFFER` fact、房態或訂位語意。
真正 host-owned 的住宿確認／canonical apply gate 仍留在 Phase 4.5D。舊
`search_flights.py` 與其 cache 保留資料相容性但已 quarantine；`search_hotels.py`
也只可在使用者明確授權 live provider、成本與資料保留政策後，作 exit gate 外的
legacy 候選 discovery；不能代表房態、訂位或可直接寫入計畫。一般 4.5B 流程只離線
正規化 caller-supplied response，不會自行呼叫 provider。

兩階段流程：
1. **Scout** — 互動式規劃：收集需求 → 解析景點 → 用戶篩選 → 路線優化 → 驗證
2. **Build** — 生成網站：組裝 JSON → 充實交通 → legacy營業時間提示 → 渲染 HTML → 部署

## 專案結構

```
trip-plan/
├── trip_planner/          # deterministic kernel、canonical codec、mutation/store
├── tests/                 # offline adversarial regression
├── docs/                  # roadmap、ADR 與 correctness contracts
├── scripts/               # 所有腳本
│   ├── build_places_cache.py   # legacy full-mask cache（預設 quarantine）
│   ├── build_itinerary.py      # 從簡化輸入 + cache → itinerary.json
│   ├── enrich_itinerary.py     # 充實交通資料（距離/時間/模式）
│   ├── routes_coverage.py      # Routes API 地區覆蓋資料（transit/two_wheeler 支援國家）
│   ├── check_hours.py          # legacy regular-hours advisory（不輸出綠燈）
│   ├── search_flights.py       # legacy：後續 quarantine，非正常規劃功能
│   ├── search_hotels.py        # legacy candidate discovery，非訂房／房態來源
│   ├── render_trip.py          # 渲染 HTML + 行事曆
│   ├── build_index.py          # 重建首頁
│   └── deploy.sh               # 部署到 GitHub Pages
├── template/
│   ├── trip.html               # 行程頁面 Jinja2 模板
│   ├── index.html              # 首頁模板
│   └── data/                   # JSON 格式模板（agent 參照用）
├── skill/
│   └── trip-planner.md         # Claude Code skill 定義
├── trips/                      # 每趟旅行的資料
│   └── {city}-{year}-{month}/
│       └── data/
│           ├── trip.json
│           ├── itinerary.json
│           ├── reservations.json
│           ├── todo.json
│           ├── info.json
│           ├── packing.json
│           └── places_cache.json
├── .envrc.example
└── requirements.txt
```
