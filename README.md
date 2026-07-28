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

- **互動式行程規劃** — AI agent 提案景點，你篩選、排序、加約束
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

兩階段流程：
1. **Scout** — 互動式規劃：收集需求 → 解析景點 → 用戶篩選 → 路線優化 → 驗證
2. **Build** — 生成網站：組裝 JSON → 充實交通 → 驗證營業時間 → 渲染 HTML → 部署

## 專案結構

```
trip-plan/
├── trip_planner/          # deterministic kernel、canonical codec、mutation/store
├── tests/                 # offline adversarial regression
├── docs/                  # roadmap、ADR 與 correctness contracts
├── scripts/               # 所有腳本
│   ├── build_places_cache.py   # 批次解析景點 → places_cache.json
│   ├── build_itinerary.py      # 從簡化輸入 + cache → itinerary.json
│   ├── enrich_itinerary.py     # 充實交通資料（距離/時間/模式）
│   ├── routes_coverage.py      # Routes API 地區覆蓋資料（transit/two_wheeler 支援國家）
│   ├── check_hours.py          # 營業時間驗證（含到達時間）
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
