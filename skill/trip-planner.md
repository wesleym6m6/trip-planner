---
name: trip-planner
description: 規劃旅行並生成完整旅遊網站。兩階段流程——Phase 1（Scout）互動式規劃，用真實 API 資料讓用戶篩選景點、加約束、迭代路線；Phase 2（Build）渲染 HTML 網站並部署。當用戶說 /trip-planner 或描述想規劃旅行時觸發。
---

# 旅行規劃 Skill

兩階段流程：**Scout**（互動式規劃，用真實資料）→ **Build**（渲染網站 + 部署）。

**專案根目錄：** 此 skill 所在 repo 的根目錄。以下所有指令用 `$REPO` 代表，agent 執行時替換為實際路徑（通常是 `git rev-parse --show-toplevel` 的結果）。

## 資料模式護欄

本 skill 的完整 user-facing 流程只適用 **legacy 模式**：`trip.json`、
`itinerary.json`、`reservations.json`、`todo.json`、`info.json`、`packing.json`、
`places_cache.json` 七個核心檔案。`plan.json` canonical kernel 目前是 developer
preview；它以 `plan.json` 加 `reservations.json`、`todo.json`、`info.json`、
`packing.json`、`places_cache.json` 五個 sidecar 取代前兩個檔案。renderer、validator
與部分 reader 可相容讀取，但本 skill 的 legacy writer（例如
`build_itinerary.py`、`enrich_itinerary.py`、`import_gmaps_list.py --merge`）會拒絕修改
已 migration 的 trip。

不要直接編輯 canonical JSON。只能使用已支援的 `TripStore` / `PlanPatch` 路徑；
Phase 5 的 `tripctl` CLI 尚未提供。除非使用者已明確接受 developer workflow，否則
不得 migration 真實 trip，並繼續使用下方 legacy 流程。

## 核心原則

1. **API 資料一次快取，同一趟旅行不重複查詢。** 每個透過 Places API 解析的地點都寫入 `places_cache.json`。從行程刪除景點不會刪 cache——用戶可能會加回來。
2. **用真實資料規劃。** 用戶在每個決策點看到的是實際交通時間和營業時間，不是估計值。
3. **用戶掌控計畫。** Agent 提案，用戶決定——打分、刪除、重排、加約束。循環持續到用戶滿意為止。
4. **⛔ 距離/位置資訊必須來自 API，禁止憑印象估算。** 任何涉及「A 離 B 多遠」「步行 X 分鐘」「在 Y 附近」的說法，都**必須**先透過 `build_places_cache.py` 取得真實座標，再用 haversine 或 `resolve_places.py` 計算。**在 cache 沒有座標之前，不得向用戶聲稱任何距離或步行時間。** 這條規則適用於所有 step，不只是 Step 5——包括 Step 2 推薦候選時如果要提到「離飯店近」「海灘旁」等位置描述，都必須先有座標佐證。違反此規則會導致用戶基於錯誤距離做出住宿和 coworking 的決策。

## 可用工具（不要自己寫，直接呼叫）

以下腳本涵蓋 skill 執行所需的全部功能。**優先使用現有腳本，不要重複造輪子。**

### 景點解析與快取

| 用途 | 腳本 | 輸入 | 輸出 | 備註 |
|------|------|------|------|------|
| 批次解析景點 + 寫入 cache | `build_places_cache.py` | stdin JSON（見下方範例） | 寫入 `places_cache.json` + stdout 摘要 | **Step 3 專用**，自動 dedup、batch resolve、append-only |
| 座標 + 距離矩陣 + 分群 | `resolve_places.py` | stdin JSON: `{"places": [{"name": "...", "maps_query": "..."}]}` | stdout JSON（含 `distance_matrix` + `clusters`） | 用於 Step 5 前觀察哪些景點在同一區 |
| 匯入 Google Maps 清單 | `import_gmaps_list.py` | Google Maps 分享連結 URL | stdout JSON 或 `--merge` 寫入 itinerary | 用戶有現成清單時的捷徑，可跳過手動候選 |

`build_places_cache.py` 輸入格式：
```bash
echo '{
  "candidates": [
    {"name": "赤崁樓", "maps_query": "赤崁樓, Tainan, Taiwan"},
    {"name": "林百貨", "maps_query": "林百貨, Tainan, Taiwan"}
  ],
  "cache_path": "trips/{slug}/data/places_cache.json"
}' | direnv exec $REPO python3 scripts/build_places_cache.py
```

### 機票與住宿搜尋

| 用途 | 腳本 | 輸入 | 輸出 |
|------|------|------|------|
| 機票搜尋 | `search_flights.py` | stdin JSON | 寫入 `flights_cache.json` + stdout 摘要 |
| 飯店搜尋 | `search_hotels.py` | stdin JSON | 寫入 `hotels_cache.json` + stdout 摘要 |
| 查看快取內特定項目完整資料 | `cache_detail.py` | CLI args | stdout 完整 JSON |

**`search_flights.py` 必填：** `departure_id`, `arrival_id`, `outbound_date`, `cache_path`（`return_date` 在 type=1 時必填）
**`search_hotels.py` 必填：** `q`, `check_in_date`, `check_out_date`, `cache_path`

Optional 參數詳見 `docs/serpapi-flights-params.md` / `docs/serpapi-hotels-params.md`。

#### 兩層存取模式

SerpApi cache 單檔可達 50K+ tokens，直接 Read 會爆 context。搜尋腳本採兩層設計：

**快速通道（stdout 摘要）** — 搜尋完畢 stdout 直接輸出每筆的 key fields，agent 從 Bash output 讀取，零額外 tool call。

Hotels 摘要欄位：`index`, `name`, `rate`（每晚最低價）, `total`（全住宿期總價）, `rating`, `reviews`, `class`, `amenities`(前6), `nearby`(前3, name+duration), `check_in_time`, `check_out_time`, `lat`, `lng`, `deal`

Flights 摘要欄位：`index`, `airline`, `flight_number`, `departure`, `arrival`, `duration`, `price`, `is_lcc`, `aircraft`, `legroom`, `often_delayed`, `stops`, `departure_token`

**深入通道（`cache_detail.py`）** — 用 index 或名稱從 cache 提取**單筆完整 JSON**（含 description、所有 OTA 報價、detailed nearby transportations、reviews breakdown 等）。

```bash
# 用 index（從 stdout 摘要的 index 欄位）
direnv exec $REPO python3 scripts/cache_detail.py trips/{slug}/data/hotels_cache.json 3

# 用名稱（case-insensitive 子字串匹配）
direnv exec $REPO python3 scripts/cache_detail.py trips/{slug}/data/hotels_cache.json "Nesta"

# Flights 也通用
direnv exec $REPO python3 scripts/cache_detail.py trips/{slug}/data/flights_cache.json "虎航"
```

#### 搜尋範例

```bash
# 機票搜尋
echo '{
  "departure_id": "TPE",
  "arrival_id": "DAD",
  "outbound_date": "2026-10-08",
  "return_date": "2026-10-12",
  "cache_path": "trips/danang-2026-10/data/flights_cache.json"
}' | direnv exec $REPO python3 scripts/search_flights.py

# 飯店搜尋（gl 必須設為目的地國碼）
echo '{
  "q": "Da Nang beach area",
  "gl": "vn",
  "check_in_date": "2026-10-08",
  "check_out_date": "2026-10-12",
  "cache_path": "trips/danang-2026-10/data/hotels_cache.json"
}' | direnv exec $REPO python3 scripts/search_hotels.py
```

#### ⚠️ SerpApi Hotels 注意事項

1. **`gl` 必須設為目的地國碼**（如越南 `vn`、日本 `jp`）。Flights 的 `gl: "tw"` 保留不動（出發地視角）。

2. **先搜再 filter，不要在 API 端加太多 filter。** SerpApi Hotels 的行為：當目標地區不夠筆數滿足 filter 條件（`max_price` + `rating` + `sort_by`），它會**往外擴大搜尋範圍**直到湊滿，導致結果偏離目標城市。正確做法：用最少 filter 搜尋（只 `q` + `gl`），拿到結果後從 stdout 摘要做 client-side filter。

3. **小城市 fallback。** SerpApi Hotels 對二線城市（如會安、九份、清邁古城）定位能力差。如果 stdout 摘要中 nearby 顯示的地標不在目標城市（如搜會安但 nearby 出現大叻車站），改用 WebSearch 搜 "best hotels [city] [year]"，再用 Places API 解析具體飯店。

4. **來回機票二階段搜尋。** SerpApi type=1（來回）只回傳去程航班 + `departure_token`。用 `departure_token` 搜第二次才能拿到回程航班 + `booking_token`。`departure_token` 已包含在 stdout 摘要中。

5. **飯店候選必須經過 Places API 解析。** SerpApi Hotels 沒有 Google Maps 商家連結（只有 GPS 座標和飯店官網）。用戶需要看照片、評論、營業時間來做決定，這些只有 Google Maps 商家頁面有。因此：從 SerpApi 篩出 top 候選後，**一律用 `build_places_cache.py` 解析一次**，拿到 `google_maps_uri`（完整商家頁）和 `website`（官網）。

#### 飯店呈現格式

表格負責比較，連結用編號腳註列在下方。表格內不嵌入 URL（tmux 不支援 Markdown 嵌入式超連結）。

```
| # | 飯店 | 價格/晚 | 星級 | 評分 | 評論 | 離古城 | 特色 |
|---|------|---------|------|------|------|--------|------|
| 1 | Little Oasis Eco Hotel & Spa | 1,857 | 5★ | ⭐4.9 | 1,468 | ~7 min | 最高分 5★ |
| 2 | The Signature Hoi An | 查 Booking | — | ⭐4.8 | 1,108 | 3 min | 最近古城 |

**連結：**
1 Little Oasis Eco Hotel & Spa — 🗺️ Maps https://maps.google.com/?cid=... — 🌐 官網 http://...
2 The Signature Hoi An — 🗺️ Maps https://maps.google.com/?cid=... — 🌐 官網 https://...
```

規則：
- 每間飯店一行，開頭重複飯店名稱
- 🗺️ Maps 和 🌐 官網 各自 emoji + 文字 + 空格 + 裸 URL
- URL 保持裸露（不用 Markdown `[text](url)`），讓終端機 Cmd+Click 可直接開啟

### 行程組裝

| 用途 | 腳本 | 輸入 | 輸出 |
|------|------|------|------|
| 從簡化輸入 + cache 組裝 itinerary | `build_itinerary.py` | stdin JSON（見下方範例） | 寫入 `itinerary.json` |

`build_itinerary.py` 是 **Phase 1 → Phase 2 的橋樑**。Agent 只需提供 name / type / time / note，腳本自動從 cache 補齊 place_id / lat / lng / maps_query / display_name。

**輸入範例：**
```bash
echo '{
  "cache_path": "trips/{slug}/data/places_cache.json",
  "output_path": "trips/{slug}/data/itinerary.json",
  "days": [
    {
      "day": 1, "date": "2026-04-17",
      "title": "奇美博物館 × 老宅義式晚餐",
      "subtitle": "仁德→中西區",
      "places": [
        {"name": "奇美博物館", "type": "spot", "time": "09:30", "note": "距高鐵站步行 15 min"},
        {"name": "奇美博物館", "type": "food", "time": "12:00", "note": "館內餐廳", "title": "奇美博物館內午餐"},
        {"name": "森根", "type": "food", "time": "18:15", "note": "老宅義式", "lat": 22.9898, "lng": 120.2088}
      ]
    }
  ]
}' | direnv exec $REPO python3 scripts/build_itinerary.py
```

**欄位說明：**
- `name`（必填）— 用來 fuzzy match cache（match 順序：exact display_name → name 在 display_name 內 → name 在 maps_query 內 → display_name 在 name 內）
- `type`（必填）— spot / food / drink / hotel / transport / flight / work
- `time`（必填）— 24h HH:MM。此欄位貫穿整個流程：`enrich_itinerary.py` 用來建構 transit 的 `departure_time`；`check_hours.py` 用來驗證營業時間；`trip.html` 模板顯示在每個景點的 description 行左側（藍色）；`generate_ics.py` 用來產生帶具體時間的行事曆事件
- `note`（必填）— 說明、注意事項
- `title`（可選）— 顯示標題，預設 = name。同一地點多次使用時需要（如「奇美博物館內午餐」）
- `lat` + `lng`（可選）— 手動座標。**有填就跳過 cache lookup，place_id 自動設 null**。用於 Google Maps 未收錄的店

**輸出範例（自動生成）：**
```json
{
  "type": "spot",
  "title": "奇美博物館",
  "note": "距高鐵站步行 15 min",
  "maps_query": "奇美博物館, Tainan, Taiwan",  ← 自動從 cache
  "place_id": "ChIJq6qqqnp0bjQR...",           ← 自動從 cache
  "lat": 22.9346,                               ← 自動從 cache
  "lng": 120.2260,                              ← 自動從 cache
  "display_name": "Chimei Museum",              ← 自動從 cache
  "time": "09:30"
}
```

### 路線規劃與驗證

| 用途 | 腳本 | 輸入 | 輸出 |
|------|------|------|------|
| SA 路線優化（分天 + 排序） | `plan_route.py` | stdin JSON（景點、天數、約束） | stdout 前 N 組最佳方案 |
| 評估特定路線（不優化） | `score_route.py` | stdin JSON（指定順序的路線） | stdout JSON（各段交通時間 + 總計） |
| 充實行程交通資料 | `enrich_itinerary.py` | 檔案路徑引數 | 原地修改 itinerary.json（加入 travel + recommended_mode） |
| 營業時間衝突檢查 | `check_hours.py` | `trips/{slug}` 目錄引數 | stdout JSON（每個景點 ✅/⚠️/🔓/❓ 狀態） |

`enrich_itinerary.py` 行為：**已有 lat/lng 的 entry 不會被重新解析**，只計算路線交通。這代表 `build_itinerary.py` 產出的 itinerary 可以直接 enrich，不會覆蓋任何資料。

`score_route.py` 使用時機：用戶提出「我想走這個順序 A → B → C」時，**不需要重跑 SA 優化**，直接用 `score_route.py` 測量該路線的實際交通時間即可。

### 網站生成與部署

| 用途 | 腳本 | 輸入 | 輸出 |
|------|------|------|------|
| 渲染單趟旅行 HTML | `render_trip.py` | trip 目錄引數 | 寫入 `index.html`（同時自動呼叫 `generate_ics.py` 產生行事曆檔）。模板在每個景點的 description 行左側顯示 `time` 欄位（藍色）。自動從 `places_cache.json` 讀取 `utc_offset_minutes` 將 transit 的 UTC 時間轉為當地時間 |
| 重建首頁 | `build_index.py` | 無 | 寫入根目錄 `index.html` |
| 部署到 GitHub Pages | `deploy.sh` | 無 | 重新渲染所有 trip → force-push 到 gh-pages |

### 底層函式（已在腳本內部使用，一般不需直接呼叫）

- `directions.resolve_place(query, field_mask=None)` — 支援 `FULL_FIELD_MASK`（50 欄位）或預設 3 欄位
- `directions.resolve_places_batched(queries, field_mask=None)` — 8/batch + 1s 間隔
- `directions.FULL_FIELD_MASK` — 完整欄位常數，觸發 Enterprise + Atmosphere SKU
- **這些函式已經寫好，不要重寫。** `build_places_cache.py` 和 `resolve_places.py` 已經包裝了它們。

### 所有腳本的呼叫方式

```bash
# 一律用 direnv exec，不要 cd
direnv exec $REPO python3 scripts/<腳本名>.py [引數]
```

## 資料檔案

### `trips/{slug}/data/places_cache.json`（per-trip API 快取）

以 `place_id` 為 key，每個地點一筆。**只增不刪。**

```json
{
  "ChIJbYl7d2F2bjQRnFdvyMBuZfI": {
    "maps_query": "赤崁樓, Tainan, Taiwan",
    "display_name": "赤崁樓",
    "types": ["tourist_attraction"],
    "primary_type": "tourist_attraction",
    "lat": 22.997,
    "lng": 120.202,
    "formatted_address": "...",
    "short_address": "...",
    "google_maps_uri": "...",
    "website": "...",
    "rating": 4.3,
    "rating_count": 12847,
    "regular_opening_hours": { "weekdayDescriptions": ["Monday: 8:30 AM – 9:30 PM", "..."] },
    "business_status": "OPERATIONAL",
    "editorial_summary": "...",
    "fetched_at": "2026-04-04T17:30:00Z"
  }
}
```

完整欄位共 50 個（含 `serves_*`、`payment_options`、`reviews` 等），不適用的欄位值為 `null`，一律保留不篩除。

### 其他檔案（每趟旅行 data/ 下：7 個核心 + 最多 2 個可選）

- `trip.json` — 標題、日期、城市、slug
- `itinerary.json` — 每日路線，含 places[]、travel[]、recommended_mode
- `reservations.json` — 訂位/預約項目（`render_trip.py` 讀取此檔，不是 checklist.json）
- `todo.json` — 行前確認項目
- `info.json` — 實用資訊（預算、簽證、交通、天氣等）
- `packing.json` — 行李清單（從 `template/data/packing.json` 複製再客製）
- `places_cache.json` — Places API 快取（Phase 1 自動生成）
- `flights_cache.json` — 機票搜尋快取（Step 1b `search_flights.py` 寫入）
- `hotels_cache.json` — 飯店搜尋快取（Step 1b `search_hotels.py` 寫入）

---

## Phase 1: Scout（互動式規劃）

對話循環。Agent 推動流程但**在每個關卡（🚪）等用戶確認**。

### Step 1: 收集需求

詢問用戶：
- **目的地** — 哪個城市？
- **天數** — 幾天幾夜？
- **月份** — 什麼時候？（影響星期幾的營業時間驗證）
- **預算等級** — 平價 / 中等 / 高檔？
- **旅行風格** — 悠閒、緊湊、混合？（影響每天景點數）
- **交通方式** — 機車？步行？開車？大眾運輸？
- **必去景點** — 有沒有一定要去的？
- **特殊需求** — 工作旅行？飲食限制？無障礙？

用戶如果一次給了足夠資訊，跳過多餘問題。

### Step 1b: 機票與住宿搜尋（可選）

**此步驟不阻塞 Step 2。** 使用者可以先規劃景點再回來搜機票，或反過來。

**Gate 條件：**
- 機票搜尋：需確認出發地機場、目的地機場、出發日期、人數
- 飯店搜尋：需確認住宿區域、入住/退房日期、人數

#### 機票搜尋流程

**Step 1b-1: 初搜去程**

1. 確認 gate 條件已滿足
2. 呼叫 `search_flights.py`（可帶 `outbound_times`、`return_times` 等篩選）
3. 呈現結果（見下方「呈現規則」）
4. 使用者可篩選（`lcc_only`、`max_price`、`stops`、`outbound_times` 等）後重搜

**Step 1b-2: 選去程 → 查回程**

使用者選定一班去程後，用該筆的 `departure_token` 再搜一次：

```bash
echo '{
  "departure_id": "TPE",
  "arrival_id": "DAD",
  "outbound_date": "2026-10-08",
  "return_date": "2026-10-12",
  "departure_token": "<從 Step 1b-1 結果取得>",
  "cache_path": "trips/{slug}/data/flights_cache.json"
}' | direnv exec $REPO python3 scripts/search_flights.py
```

回傳**該去程對應的所有可用回程**（每筆帶 `booking_token`）。筆數因航線而異——直飛廉航可能只有 1 班回程，轉機航線可能有 8+ 種組合。

呈現回程選項表格，使用者選定回程。

**Step 1b-3:（可選）查訂票連結**

使用者選定回程後，用 `booking_token` 取得各 OTA 訂票連結：

```bash
echo '{
  "departure_id": "TPE",
  "arrival_id": "DAD",
  "outbound_date": "2026-10-08",
  "return_date": "2026-10-12",
  "booking_token": "<從 Step 1b-2 結果取得>",
  "cache_path": "trips/{slug}/data/flights_cache.json"
}' | direnv exec $REPO python3 scripts/search_flights.py
```

每步消耗 1 次 SerpApi 額度（共 3 次完成一組來回選擇）。

#### 飯店搜尋流程

1. 確認 gate 條件已滿足
2. **決定 `gl` 國碼**（必要）— 根據目的地國家設定，例如越南 `"vn"`、日本 `"jp"`、台灣 `"tw"`。腳本無預設值，**未傳 `gl` 或傳錯會導致結果完全偏向錯誤地區**（例：搜 "Hoi An" 不帶 `gl=vn` → 全部回傳台灣飯店）
3. **首次搜尋不帶 filter**（不加 `sort_by`、`max_price`、`rating` 等）— 先確認地理命中正常。二線城市（如會安、寧平）加 filter 後 SerpApi 會將搜尋範圍擴散到全國填滿 20 筆，`sort_by=3`（最低價）是最危險的參數，單獨加就能讓命中率從 85% 掉到 6%。詳見 `docs/serpapi-hotels-params.md` 「Filter 導致地理擴散」
4. 呼叫 `search_hotels.py`（stdin JSON 須包含 `gl`）
5. 呈現結果（見下方「呈現規則」）
6. 使用者可篩選（`max_price`、`hotel_class`、`free_cancellation`、`amenities` 等）後重搜 — **每次加 filter 後檢查結果座標是否仍在目的地**，如果擴散就改用本地過濾
7. `free_cancellation` 在規劃初期建議開啟，方便後續行程變動時調整

#### 呈現規則

搜尋結果通常有 20~100+ 筆，**不要全部列出**。Agent 自行判斷呈現哪些，規則：

- **最多呈現 10 筆**
- Agent 根據使用者需求（預算、偏好、風格）從全部結果中挑選最相關的 10 筆。例如使用者偏好平價，就以價格排序取前 10；使用者沒特別偏好，就混合 best_flights + 最低價 + 最短時長
- **必須告知使用者搜尋總筆數和篩選邏輯**，例如：「共搜尋到 104 筆航班，以下依價格排序列出前 10 筆：」或「共 104 筆，以下列出 best_flights 4 筆 + 最低價 6 筆：」
- 使用者可以要求換排序方式、看更多、或加篩選條件重搜

**機票表格格式：**
```
共搜尋到 104 筆航班，以下依價格排序列出前 10 筆：

 # | 航班       | 出發  | 到達  | 轉機 | 時長    | 價格(TWD) | 廉航
 1 | IT 551     | 07:10 | 09:15 | 直飛 | 2h 05m | 10,579    | ✅
 2 | UO 113→552 | 08:30 | 14:20 | 1 轉 | 5h 50m | 12,520    | —
 ...
```

**飯店表格格式：**
```
共搜尋到 20 間飯店，以下依每晚價格排序：

 # | 飯店名稱           | 星級 | 評分 | 每晚(TWD) | 特色
 1 | A La Carte Da Nang | ★★★★ | 4.3  | 2,800     | 含早餐, 泳池
 2 | Fusion Suites      | ★★★★★| 4.5  | 4,200     | 免費取消
 ...
```

#### Freshness 規則

- flights/hotels cache 的 `fetched_at` 超過 **3 天** → 告知使用者資料年齡 + 建議重抓
- places cache：不設時間限制，需要時手動重抓
- **最終確認階段（Phase 2 前）：** 如果 flight/hotel 資料超過 3 天，**必須重抓**後再進入 Build

### Step 2: 生成候選景點清單

候選景點有三個來源，合併後一起呈現給用戶：

1. **用戶的 Google Maps 清單**（如果 Step 1 有提供）— 用 `import_gmaps_list.py` 匯入：
   ```bash
   direnv exec $REPO python3 scripts/import_gmaps_list.py "https://maps.app.goo.gl/XXXXX"
   ```
   匯入結果是名稱 + 座標，作為候選素材，不代表全部都會納入行程。
2. **用戶口頭指定的必去 / 想去景點**（如果 Step 1 有提到）
3. **Agent 根據需求額外推薦** — 補足用戶清單沒涵蓋的類型（例如用戶清單全是景點，Agent 補美食和住宿），總量生成**比所需多 30-50%** 讓用戶篩選。

Google Maps 清單是輸入素材，不是指令。**除非用戶明確說「就這些，不用再推薦了」，否則 Agent 仍應主動推薦額外候選。** 匯入後問用戶：「這些之中有哪些一定要去？哪些可以不去？需要我再推薦其他地方嗎？」

**來源標記：** 在整個 Phase 1 過程中（Step 2 ~ Step 7），任何時候向用戶列出景點，都必須標記每個景點的來源——哪些是用戶提供的（Google Maps 清單 / 口頭指定），哪些是 Agent 額外推薦的。這樣用戶才能快速辨識自己原本的選擇和 Agent 的建議。只有最終 Phase 2 生成網站時不需要標記來源。

每個候選提供：
- 名稱
- 類型（景點 / 美食 / 住宿 / 等）
- 來源標記（`📌 用戶` 或 `💡 推薦`）
- 推薦理由（一句話）
- `maps_query` — **必須包含具體店名或地標名 + 城市 + 國家**（不要用模糊街名）

**⛔ Step 2 禁止聲稱距離：** 在 Step 2 呈現候選清單時，**不得包含任何距離、步行時間、或相對位置描述**（如「離飯店 5 分鐘」「海灘旁」「在 X 附近」）。這些資訊只能在 Step 3 打完 API 拿到座標後，用實際計算結果呈現。Step 2 只呈現名稱、類型、推薦理由。如果推薦理由涉及位置優勢（如 coworking 離飯店近），必須標註「距離待 API 驗證」，或等 Step 3 後再補充。

**正確流程：** Step 2（列候選，不含距離）→ Step 3（打 API 拿座標）→ 用座標計算距離 → 補充距離資訊給用戶 → Step 4（用戶篩選，此時已有真實距離）。

### Step 3: 批次打 Places API + 寫入快取

**一次解析所有候選，包含景點、餐廳、飯店、coworking、spa。** 不要分批序列跑。寧可多解 10 個最終用不到的（API 成本 < $0.01），也不要到 Step 5/6 才發現缺資料要回頭補。飯店候選需要 `google_maps_uri` 讓用戶看照片評論（SerpApi Hotels 沒有此欄位），所以**必須在這一步一起解析**。

**直接呼叫 `build_places_cache.py`**，不要自己寫 API 呼叫邏輯：

```bash
echo '{
  "candidates": [
    {"name": "赤崁樓", "maps_query": "赤崁樓, Tainan, Taiwan"},
    {"name": "度小月", "maps_query": "度小月擔仔麵 原始店, Tainan, Taiwan"},
    {"name": "某飯店", "maps_query": "Hotel Name, City, Country"},
    {"name": "某 Coworking", "maps_query": "Coworking Name, City, Country"},
    {"name": "某 Spa", "maps_query": "Spa Name, City, Country"}
  ],
  "cache_path": "trips/{slug}/data/places_cache.json"
}' | direnv exec $REPO python3 scripts/build_places_cache.py
```

腳本自動處理：
- 載入既有 cache → 跳過已快取的 → batch resolve 新的（8/batch + 1s 間隔）→ 寫回 cache
- 解析失敗的會列出，依以下順序 fallback：

**解析失敗 fallback 流程：**
1. **換 query 重試** — 加地址、換英文/中文名、加「餐廳」「咖啡」等類型關鍵字
2. **用戶提供地址** — 請用戶給具體地址或 Google Maps 連結
3. **網路搜尋** — 用 WebSearch 搜店名 + 城市，從 Instagram、Facebook、食記部落格找到地址/座標/營業時間
4. **手動建 cache entry** — 以上都找不到時，用找到的座標在 `places_cache.json` 手動加一筆 entry（key 用 `manual_` 前綴），`editorial_summary` 註明「Google Maps 未收錄」。在 `build_itinerary.py` 的輸入中，這類景點直接給 `lat` + `lng`，腳本會自動設 `place_id: null`（模板用座標連結）

很多小店（私房餐廳、新開的甜點店、預約制料理）不在 Google Maps 上但在 IG/Facebook 有頁面。**不要在 Step 1 解析失敗就放棄，先搜網路。**

**快取規則：**
- 以 `place_id` 為 key（穩定識別碼）
- **只增不刪** — 從行程移除景點不會刪 cache entry
- 後續加新景點時，先查 cache → 沒有才打 API → 打完一律寫回 cache

**API 成本：** Field mask 決定計費 tier（取最高）：
- **Pro**（$32/1000，免費 5,000/月）：`displayName`、`location`、`types`、`photos`、`formattedAddress`、`googleMapsUri`、`businessStatus`、`timeZone`、`accessibilityOptions` 等
- **Enterprise**（$35/1000，免費 1,000/月）：`regularOpeningHours`、`rating`、`websiteUri`、`internationalPhoneNumber`、`priceLevel`、`userRatingCount` 等
- **Enterprise + Atmosphere**（$40/1000，免費 1,000/月）：`reviews`、`editorialSummary`、`generativeSummary`、`serves*`、`allows*`、`goodFor*`、`paymentOptions`、`parkingOptions` 等

目前 `FULL_FIELD_MASK` 觸發最高 tier（Enterprise + Atmosphere），免費 1,000/月，實際用量 < 500/月 = **$0**。如需省成本可改用 `DEFAULT_FIELD_MASK`（只拿 3 欄位，走 Pro tier）。

### Step 4: 🚪 呈現景點清單 → 用戶打分 / 篩選

用 cache 的真實資料呈現候選清單：

```
候選景點（共 25 個，需選 ~18 個填入 3 天行程）

 # | 景點              | 類型 | 評分  | 營業時間摘要                | 網站
 1 | 赤崁樓            | 景點 | ⭐4.3 | 08:30-21:30 每日           | twtainan.net/...
 2 | 度小月（原始店）    | 美食 | ⭐4.1 | 11:00-21:00 週一公休        | duxiaoyue.com/...
 3 | 花園夜市           | 美食 | ⭐4.0 | 僅 四/六/日 18:00-01:00     | —
 4 | 神農街             | 景點 | —    | 🔓 戶外街道，全天開放        | —
 5 | 某私房小店          | 美食 | ⭐4.5 | ❓ API 無營業時間，需人工確認 | —
```

**營業時間標注規則：**
- API 有 `regular_opening_hours` → 直接顯示
- API 無營業時間，但類型為戶外/公共空間（`street`、`park`、`neighborhood` 等）→ 標 `🔓 戶外，全天開放`
- API 無營業時間，但類型為店家/景點/餐廳 → 標 `❓ API 無營業時間，需人工確認`

**請用戶：**
- ❌ 刪除不要的景點
- ➕ 新增遺漏的景點（agent 查 cache → 沒有才打 API → 寫回 cache）
- ⭐ 打分（1-5）標記優先度（可選，不打分預設 3）
- 📌 加約束條件（見下方「約束處理」）

**等用戶回覆。** 有修改就重複此步驟。

### Step 5: 路線規劃

先用 `resolve_places.py` 看分群（哪些景點在同一區 < 1.5 km）：

```bash
echo '{"places": [...]}' | direnv exec $REPO python3 scripts/resolve_places.py
```

再用 `plan_route.py` 跑 SA 優化：

```bash
echo '{
  "places": [
    {"name": "赤崁樓", "lat": 22.997, "lng": 120.202, "type": "spot"},
    ...
  ],
  "days": 3,
  "start": "飯店",
  "fixed": {
    "赤崁樓": 1,
    "花園夜市": {"day": 1, "pos": "last"},
    "安平古堡": 2
  },
  "per_day_min": 3,
  "per_day_max": 7,
  "available_modes": ["walking", "bicycling", "driving"]
}' | direnv exec $REPO python3 scripts/plan_route.py
```

`plan_route.py` 處理：
- `fixed`：指定天數（int）或天數 + 位置（dict `{"day": N, "pos": "last"}`）
- `start`：每天起點（軟偏好，不是硬約束——有 pos 約束時 pos 優先）
- SA 回傳前 N 組方案，按總交通距離排序

### 約束處理（Agent 判斷，不靠算法）

`plan_route.py` **只優化距離，不懂語意**。以下約束由 agent 在拿到 SA 結果後，用常識判斷和調整：

| 約束類型 | 範例 | Agent 怎麼做 |
|----------|------|-------------|
| 時段 | 「夜市排晚上」「早餐排早上」 | **常識判斷**：夜市當然排晚上、早餐店排早上、博物館排室內午後。不需要跑算法，直接在每天內調整順序。 |
| 先後順序 | 「先去 A 再去 B」 | 檢查 SA 結果，A 在 B 前面就不動，否則手動交換。 |
| 優先度 | 用戶打 5 星的景點被 SA 丟掉 | 告知用戶哪些高優先景點被排除，問要不要替換低優先的。 |
| 分組 | 「安平區的排同一天」 | 用 `resolve_places.py` 的 `clusters` 結果確認同區景點，檢查 SA 有沒有分到同一天。 |
| 避開正午戶外 | 「戶外景點不要排中午」 | 戶外景點排早上或傍晚，室內景點排正午。這是常識，不需要額外腳本。 |

**原則：算法給大方向（哪些景點分哪天），agent 用常識微調順序。不要把所有邏輯都丟給算法——算法可能走極端。**

### Step 6: 驗證 + 呈現路線

SA 結果 + agent 調整後：

1. **充實交通資料：**
   ```bash
   direnv exec $REPO python3 scripts/enrich_itinerary.py trips/{slug}/data/itinerary.json
   ```

2. **營業時間驗證：**
   ```bash
   direnv exec $REPO python3 scripts/check_hours.py trips/{slug}
   ```
   輸出每個景點的狀態：`✅ 到達時間在營業內`、`⚠️ 營業日但到達時間不對（早到/遲到/休息時段）`、`❌ 當天公休`、`🔓 戶外全天`、`❓ 無資料`

3. **呈現路線：**
   ```
   Day 1 — 古蹟美食巡禮（週六）
     🏨 Check-in 飯店
     🛵  5 min ｜ 1.2 km → 赤崁樓 (08:30-21:30 ✅)
     🚶  3 min ｜ 0.2 km → 度小月 (11:00-21:00 ✅)
     🛵  5 min ｜ 1.1 km → 林百貨 (11:00-21:00 ✅)
     🛵 10 min ｜ 2.9 km → 花園夜市 (18:00-01:00 ✅)

   📊 全程：機車 35 min / 步行 29 min / 總距離 12.3 km
   ```

### Step 7: 🚪 用戶回饋循環

**等用戶回覆。** 可能的回饋：

| 回饋類型 | Agent 動作 |
|----------|-----------|
| 「滿意，繼續」 | → 進入 Phase 2 |
| 「Day 1 太趕」 | 移動景點到其他天，重跑 enrich，回 Step 6 |
| 「把 X 換成 Y」 | 查 cache → 沒有則打 API 寫回 cache → 替換後重跑 Step 5-6 |
| 「加一個景點 Z」 | 查 cache → 沒有則打 API 寫回 cache → 加入候選 → 重跑 Step 5-6 |
| 「刪掉 X」 | 從 itinerary 移除（cache 保留）→ 重跑 Step 5-6 |
| 「X 改到第 3 天下午」 | 更新約束 → 重跑 Step 5-6 |
| 「整體順序 OK 但交通方式想改」 | 改 available_modes → 只重跑 enrich → 回 Step 6 |
| 「我想走 A → B → C 這個順序」 | 用 `score_route.py` 測量該路線，不需重跑 SA |

**新增景點 → 查 cache → 沒有才打 API → 一律寫回 cache。**

循環持續到用戶明確確認路線。

---

## Phase 2: Build（網站生成）

用戶已確認路線。以下是機械式生成。

### 資料 Template

`template/data/` 下有每個 JSON 的模板。建檔前先 `Read` 對應 template 看格式。

| 模板 | 建檔方式 | 備註 |
|------|----------|------|
| `template/data/trip.json` | 手寫 | 6 欄位：title, subtitle, date_range, cities, slug, icon（emoji，用於 iPhone 書籤圖示） |
| `template/data/reservations.json` | 手寫 | 訂位/預約項目。陣列，每項 `{label, note}` |
| `template/data/todo.json` | 手寫 | 行前確認項目。陣列，每項 `{label, hint}` |
| `template/data/info.json` | 手寫 | sections 陣列，每個 section 有 type: "table" 或 "text" |
| `template/data/packing.json` | `cp` 複製再客製 | 預設行李清單，依目的地增減項目 |
| （`itinerary.json`） | `build_itinerary.py` 生成 | **不要手寫**，用腳本從 cache 自動補齊 |
| `template/data/places_cache.json` | `build_places_cache.py` 生成 | **不要手寫**，Phase 1 Step 3 自動產生。template 僅供參考結構 |

`trips/{slug}/data/` 下必須有 7 個核心檔案：`trip.json`、`itinerary.json`、`reservations.json`、`todo.json`、`info.json`、`packing.json`、`places_cache.json`。另可選 `flights_cache.json`、`hotels_cache.json`（Step 1b 搜尋後產生）。

### Step 8: 決定 slug + 建立資料檔

**Slug 格式：** `{city}-{year}-{month}`，如 `tainan-2026-04`

依序建立 `trips/{slug}/data/` 下的檔案：

#### 8a. `trip.json`（手寫，格式參照 `template/data/trip.json`）

#### 8b. `itinerary.json`（用 `build_itinerary.py` 生成，不要手寫）
```bash
echo '{
  "cache_path": "trips/tainan-2026-04/data/places_cache.json",
  "output_path": "trips/tainan-2026-04/data/itinerary.json",
  "days": [
    {
      "day": 1, "date": "2026-04-17",
      "title": "奇美博物館 × 老宅義式晚餐",
      "subtitle": "仁德→中西區",
      "places": [
        {"name": "奇美博物館", "type": "spot", "time": "09:30", "note": "距高鐵站步行 15 min"},
        {"name": "奇美博物館", "type": "food", "time": "12:00", "note": "館內餐廳", "title": "奇美博物館內午餐"},
        {"name": "森根", "type": "food", "time": "18:15", "note": "老宅義式，僅現金", "lat": 22.9898, "lng": 120.2088},
        {"name": "小滿西點", "type": "food", "time": "20:30", "note": "千層蛋糕，週六日公休"},
        {"name": "Moonrock", "type": "drink", "time": "22:00", "note": "亞洲百大酒吧"}
      ]
    }
  ]
}' | direnv exec $REPO python3 scripts/build_itinerary.py
```
Agent 只提供 name / type / time / note，腳本自動從 cache 補齊 place_id / lat / lng / maps_query / display_name。Google Maps 未收錄的店給 lat + lng，place_id 自動設 null。

#### 🔍 Review Checkpoint 1 + Step 8c-8f：平行執行

`build_itinerary.py` 完成後，**一次 spawn 5 個 sub-agents 同時執行**（Checkpoint 1 + 4 個資料檔）。這 5 個任務互不依賴，平行可省 ~150 秒 agent 思考時間。

**同時 spawn 以下 5 個 sub-agents：**

##### Sub-agent 1：Checkpoint 1（itinerary 驗證）

```
Review the itinerary.json just generated by build_itinerary.py.
Read these two files:
1. trips/{slug}/data/itinerary.json
2. trips/{slug}/data/places_cache.json

Check ALL of the following. Report each as ✅ or ❌ with specifics:

1. MATCH CORRECTNESS: For every place entry, compare "title" vs "display_name".
   If display_name looks unrelated to the title, the fuzzy match hit the wrong place.
   Example of a BAD match: title="森根 Sengen Studio" but display_name="森·鍋燒意麵"

2. COORDINATES: For entries with place_id=null, verify lat/lng are within the
   destination city (not in a different city). Check against other entries' coordinates.

3. DUPLICATE TITLES: If the same place appears multiple times (same lat/lng),
   each must have a distinct "title" (e.g. "奇美博物館" vs "奇美博物館內午餐").

4. MISSING COORDINATES: Every entry MUST have both "lat" and "lng" (non-null).
   Missing coordinates will cause enrich_itinerary.py to attempt API resolution.

5. TIME FORMAT: Every "time" field must be HH:MM (24h). Within each day,
   times must be in ascending order.

If ANY check fails, list the specific entries that need fixing.
Do NOT modify any files — report only.
```

Checkpoint 1 有問題就修正 `build_itinerary.py` 的輸入重跑，不要手改 `itinerary.json`。

##### Sub-agent 2：`reservations.json`

每個 sub-agent 都需要一份 **trip context summary**（目的地、日期、每日行程摘要含景點名+時間+note 重點、交通方式、住宿、特殊需求）。主 agent 從 Phase 1 確認的行程中整理這份 summary，作為每個 sub-agent prompt 的開頭。

```
[Trip context summary — 主 agent 自行整理]

Write trips/{slug}/data/reservations.json.
Format: JSON array of {label, note}. Read template/data/reservations.json for format reference.
Include all places needing reservations, ticket purchases, and hotel bookings from the itinerary.
Use Traditional Chinese.
```

##### Sub-agent 3：`todo.json`

```
[Trip context summary — 同上]

Write trips/{slug}/data/todo.json.
Format: JSON array of {label, hint}. Read template/data/todo.json for format reference.
Pre-trip checklist items: confirmations, preparations, weather, transport setup, etc.
IMPORTANT: Only state facts explicitly provided in the trip context. Do not guess or assume
store policies (e.g. "不接受預約") that are not mentioned in the context.
Use Traditional Chinese.
```

##### Sub-agent 4：`info.json`

```
[Trip context summary — 同上]

Write trips/{slug}/data/info.json.
Format: JSON object with "sections" array. Read template/data/info.json for format reference.
IMPORTANT: table type sections use "rows" as array of arrays: [["項目","預估"],["高鐵","~1,500 元"],...],
NOT array of objects. Include "footnote" string for totals.
text type sections use "content" string.
Include: 預算概覽 (table), 交通 (text), 天氣 (text), and any trip-specific sections.
Use Traditional Chinese.
```

##### Sub-agent 5：`packing.json`

```
[Trip context summary — 同上]

Write trips/{slug}/data/packing.json.
First read template/data/packing.json as the base, then customize for this trip.
Format: JSON array of {label, category}. Categories: 證件, 衣物, 盥洗, 電子, 醫療, 財務, 其他.
Add trip-specific items based on activities, weather, and transport in the context.
Use Traditional Chinese.
```

**等全部 5 個 sub-agent 完成。** Checkpoint 1 失敗則修正 itinerary 重跑（含重新 spawn 受影響的 sub-agents）；資料檔 sub-agent 完成後主 agent 快速掃一眼合理性即可。

### Step 9: enrich + 驗證

建完 itinerary.json 後依序跑：

```bash
# 1. 充實交通資料（加入每段 travel 的距離/時間/推薦模式）
#    第三個引數是 UTC offset（當地時區），讓 transit 查詢使用行程中的實際出發時間。
#    ⚠️ 時區必須是目的地的當地時區，不是用戶所在時區！
direnv exec $REPO python3 scripts/enrich_itinerary.py trips/{slug}/data/itinerary.json walking,transit,driving +09:00

# 2. 營業時間驗證（每個景點的到訪時間 vs 營業時間）
direnv exec $REPO python3 scripts/check_hours.py trips/{slug}
```

**常見時區對照：**
| 目的地 | UTC Offset |
|--------|-----------|
| 台灣 | `+08:00` |
| 日本 | `+09:00` |
| 越南/泰國 | `+07:00` |
| 韓國 | `+09:00` |
| 新加坡/馬來西亞 | `+08:00` |
| 英國（夏令） | `+01:00` |
| 法國（夏令） | `+02:00` |
| 美東（夏令） | `-04:00` |
| 美西（夏令） | `-07:00` |
| 澳洲雪梨（夏令） | `+11:00` |

**時區影響：** 當有 UTC offset 時，enrich 會用 `{day.date}T{place.time}:00{offset}` 建構每段路線的 `departure_time`，Routes API 據此回傳**對應該時間點的實際班次資訊**（哪班車、幾點發、幾點到、經過幾站）。沒有 offset 則 transit 查不到準確班次。

**Transit 回傳資料：** 當 transit 有班次資料時，每段 travel 的 `modes.transit` 會包含 `transit_steps` 陣列，每個 step 有完整的 `transitDetails`（站名、發車時間、到達時間、路線名、營運公司、車種、經過站數）。

**Transit HTML 渲染：** `render_trip.py` 會在每段交通下方顯示 transit 細節：
- 路線膠囊標籤（綠色 = 公車，深藍 = 火車/高鐵/地鐵）
- 上車站 → 下車站
- 當地發車時間（自動從 UTC 轉換，時區來自 `places_cache.json` 的 `utc_offset_minutes`）
- 轉乘段會顯示多行（每班車一行）

渲染範例：
```
🚇 28 分鐘 ｜ 5.9 km
   [77]  民族路西華南街口 → 南紡購物中心  18:28
```

enrich 不會動已有座標的 entry，只計算路線交通。check_hours 會報告 ✅/⚠️/❌/🔓/❓ 狀態。

#### 🔍 Review Checkpoint 2：全資料 pre-render 審查

enrich + check_hours 完成後、render 之前，**派 sub-agent（必須 block，通過才 render）**。使用以下 prompt：

```
Pre-render review for trip: trips/{slug}
Read ALL files in trips/{slug}/data/ and verify the following.
Report each as ✅ or ❌ with specifics.

1. FILE COMPLETENESS: These 7 files must all exist in data/:
   trip.json, itinerary.json, reservations.json, todo.json,
   info.json, packing.json, places_cache.json

2. OPENING HOURS: Run check_hours.py output (already provided by main agent).
   Are there any ⚠️ (visit time outside hours) or ❌ (closed day)?
   If yes, list each conflict.

3. TRANSIT SANITY: In itinerary.json, check every "travel" segment:
   - No 0 km / 0 min segments UNLESS both places share the same lat/lng (same location)
   - No single urban segment > 30 min or > 15 km (likely wrong coordinates)
   - "recommended_mode" exists for every segment

4. RESERVATIONS COVERAGE: Read itinerary.json notes for any mention of
   "預約", "訂位", "reservation", "需預約". Cross-check that each such
   place appears in reservations.json. List any missing.

5. PACKING CUSTOMIZATION: Compare packing.json against template/data/packing.json.
   If they are identical, the agent forgot to customize. List trip-specific items
   that should be added (based on itinerary activities).

6. INFO CONSISTENCY: Check info.json mentions correct city, dates, transport mode,
   and weather season matching the trip.json date_range.

If ALL checks pass, respond: "✅ All 6 checks passed. Ready to render."
If ANY check fails, list failures. Do NOT modify any files.
```

通過後才進入 render。

### Step 10: 渲染 + 部署

```bash
# 3. 渲染 HTML + 行事曆
direnv exec $REPO python3 scripts/render_trip.py trips/{slug}

# 4. 重建首頁
direnv exec $REPO python3 scripts/build_index.py

# 5. 部署（直接執行，不需用戶確認）
direnv exec $REPO bash scripts/deploy.sh
```

`deploy.sh` 會重新渲染所有 trip、重建首頁、force-push 到 gh-pages。**部署只影響 gh-pages branch，不動 master，直接執行即可。** 部署完成後只回報該趟旅行的網址（不需附首頁和行事曆連結）：

```
部署完成！🌐 https://BigDumbBird.github.io/trip-planner/{slug}/
```

---

### Phase 2 完整範例（端到端）

以台南三天兩夜為例，Phase 1 結束後 agent 執行：

```bash
# Step 8a: trip.json（主 agent 手寫）
# Step 8b: itinerary.json（build_itinerary.py 生成）
echo '{"cache_path":"trips/tainan-2026-04/data/places_cache.json","output_path":"trips/tainan-2026-04/data/itinerary.json","days":[...]}' \
  | direnv exec $REPO python3 scripts/build_itinerary.py
# → "Done: 30 places (29 from cache, 1 manual coords)"

# 🔍 Checkpoint 1 + Step 8c-8f：同時 spawn 5 個 sub-agents
#    - Sub-agent 1: Checkpoint 1（驗證 itinerary.json）
#    - Sub-agent 2: reservations.json
#    - Sub-agent 3: todo.json
#    - Sub-agent 4: info.json
#    - Sub-agent 5: packing.json
# 全部完成後繼續（~60 秒，而非串行 ~200 秒）

# Step 9: enrich + 驗證
direnv exec $REPO python3 scripts/enrich_itinerary.py trips/tainan-2026-04/data/itinerary.json walking,bicycling,driving,transit +08:00
# → "Places: 30 pre-resolved, 0 need API resolution"
# → "Enriched 30 places and 27 routes."

direnv exec $REPO python3 scripts/check_hours.py trips/tainan-2026-04
# → 逐一驗證營業時間，報告衝突

# 🔍 Review Checkpoint 2: sub-agent 全資料審查（7 檔案齊全、無衝突、交通合理、訂位完整）

# Step 10: 渲染 + 部署
direnv exec $REPO python3 scripts/render_trip.py trips/tainan-2026-04
direnv exec $REPO python3 scripts/build_index.py
direnv exec $REPO bash scripts/deploy.sh
```

---

## 交通模式選擇

`enrich_itinerary.py` 自動選擇每段的 `recommended_mode`：
- **≤ 1 km：** 步行
- **1–5 km：** bicycling（機車的代理模式）或 two_wheeler（真實機車路線）
- **> 5 km：** driving（計程車/Grab）

`available_modes` **直接控制 API 查詢範圍**——只查指定的模式，不會浪費 API call 在用不到的模式上。同時也限制 `recommended_mode` 只從這些模式中選。

常見組合範例：
```bash
# 有機車（台灣/越南常見）
enrich_itinerary.py itinerary.json walking,bicycling,driving +08:00

# 純大眾運輸 + 偶爾 Uber
enrich_itinerary.py itinerary.json walking,transit,driving +09:00

# 純步行 + 大眾運輸（沒車沒機車沒 Uber）
enrich_itinerary.py itinerary.json walking,transit +08:00

# 真實機車路線（東南亞，Enterprise 層級）
enrich_itinerary.py itinerary.json walking,two_wheeler,driving +07:00
```

### Routes API 交通模式

| 內部名稱 | Routes API 模式 | 計費層級 | 說明 |
|----------|----------------|---------|------|
| `driving` | DRIVE | Essentials | 汽車路線 |
| `walking` | WALK | Essentials | 步行路線 |
| `bicycling` | BICYCLE | Essentials | 自行車路線（也可作為機車代理，×0.5 校正） |
| `transit` | TRANSIT | Essentials | 大眾運輸（支援 `departure_time`，受地區限制） |
| `two_wheeler` | TWO_WHEELER | **Enterprise** | 真實機車路線（$15/千次，免費 1,000/月） |

### Routes API 地區覆蓋（實測 + 官方文件，2026-04-04 驗證）

**完整資料見 `scripts/routes_coverage.py`。** 以下是常見旅遊目的地摘要：

| 地區 | DRIVE | WALK | BICYCLE | TWO_WHEELER | TRANSIT |
|------|-------|------|---------|-------------|---------|
| 🇹🇼 台灣 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 🇯🇵 日本 | ✅ | ✅ | ✅ | ❌ | ❌ **官方排除** |
| 🇻🇳 越南 | ✅ | ✅ | ❌ | ✅ | ✅ |
| 🇰🇷 韓國 | ✅ | ✅ | ✅ | ❌ | ✅ |
| 🇹🇭 泰國 | ✅ | ✅ | ❌ | ✅ | ✅ |
| 🇸🇬 新加坡 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 🇺🇸 美國 | ✅ | ✅ | ✅ | ❌ | ✅ |
| 🇬🇧 英國 | ✅ | ✅ | ✅ | ❌ | ✅ |
| 🇫🇷 法國 | ✅ | ✅ | ✅ | ❌ | ✅ |
| 🇦🇺 澳洲 | ✅ | ✅ | ✅ | ❌ | ✅ |

**關鍵規則：**
- **TRANSIT**：Google 官方明確排除日本（所有城市）和印度 IRCTC（長途鐵路）。其他國家看城市層級 GTFS 合作夥伴覆蓋。
- **TWO_WHEELER**：僅 ~40 個國家支援（主要東南亞、南亞、南美、非洲）。完整清單見 `routes_coverage.py`。
- **BICYCLE**：東南亞普遍不可用（越南、泰國、馬來西亞、印尼等），但東亞、歐美可用。
- **東南亞旅行**：用 `two_wheeler` 取代 `bicycling` 估算機車時間更準確。
- **日本旅行**：只有 driving / walking / bicycling 可用。TRANSIT 需改用其他方案（見下方降級規則）。

### Agent 處理不支援模式的流程

`directions.py` 的 `get_directions()` 接受 `country_code` 參數，自動跳過不支援的模式（省 API 呼叫）。

**當用戶選擇的 `available_modes` 包含不支援的模式時：**

1. Agent 在 Phase 1 Step 1 收集需求時，根據目的地國家查 `routes_coverage.py`
2. 如果用戶需要的模式不支援（例如日本的 transit），**必須告知用戶**：
   - 說明哪些模式不可用、原因
   - 建議替代方案（如 driving 時間作為參考、或使用 Google Maps app 手動查 transit）
   - 讓用戶決定是否接受
3. 在 `enrich_itinerary.py` 呼叫時，只傳入支援的 `available_modes`
4. `directions.py` 的 `skipped_modes` 回傳值會標記哪些模式被跳過

## 降級規則

- **沒有 API key：** `directions.py` 回傳 `source: "unavailable"`，place_id 為 null。模板降級用 `maps/search/` URL，顯示「估計」。
- **API 限速：** 批次平行 + 重試。Places: 8/batch + 1s 間隔；Routes: 15/batch + 1s 間隔。
- **缺 place_id：** 模板用 `maps_query` 搜尋 URL 作為替代。
- **Transit 不支援（日本等）：** 用 driving 時間作為大眾運輸的近似參考。東京市區電車通常比開車快，但 driving 至少給出量級。Agent 應在行程表備註「交通時間為開車估計，實際電車可能更快/更慢」。

## 常見陷阱

- **`maps_query` 必須具體** — `"國華街"` 會解到錯的地方。一律用具體店名 + 城市：`"邱家小卷米粉 國華街 台南"`。
- **`plan_route.py` 不懂語意** — 只優化距離，會把早餐排下午、夜市排早上。Agent 必須用常識在 SA 結果後調整。
- **direnv exec 必須** — Claude Code 的 Bash 跑非互動 shell，`cd` 不會觸發 direnv。一律：`direnv exec $REPO <指令>`。
- **新開的店可能 Google Maps 沒收錄** — 解析失敗時，先用 WebSearch 搜 IG/Facebook/部落格找座標。找到後在 `places_cache.json` 手動建 entry（key 用 `manual_` 前綴）。在 `build_itinerary.py` 輸入中給 `lat` + `lng`，腳本自動設 `place_id: null`，模板會用座標連結。
- **機車路線有兩種方式** — (1) Routes API 的 `TWO_WHEELER` 模式可取得真實機車路線（Enterprise 層級，僅 ~40 國支援，見覆蓋表）。(2) 不支援的地區用 `bicycling` 作為代理，`enrich_itinerary.py` 自動將 bicycling 時間 ×0.5 校正為機車速度。`render_trip.py` 將 bicycling 顯示為 🛵。東南亞旅行優先用 `two_wheeler`（但注意 BICYCLE 在東南亞普遍不可用，不能混用）。
- **行李清單要從 template 複製** — `template/data/packing.json` 是預設清單，每趟旅行都要複製再依目的地增減（如加 VR 票、高鐵票等特定項目）。

## 完成檢查清單

宣告完成前驗證：
- [ ] `places_cache.json` 包含所有景點，有營業時間、網站、評分
- [ ] 所有行程景點有有效 `place_id`（`ChIJ` 開頭）或 `null`（未收錄）
- [ ] 營業時間無衝突（`check_hours.py` 全部 ✅ 或 🔓）
- [ ] 每段交通都有 `recommended_mode`
- [ ] 交通時間不超過風格門檻（悠閒：單段 30 min / 全天 60 min）
- [ ] 有網站的景點已附連結
- [ ] HTML 所有分頁正常渲染
- [ ] Google Maps 連結指向正確位置（**特別檢查 place_id=null 的座標連結**）
- [ ] `reservations.json` 有目的地專屬訂位項目
- [ ] `todo.json` 有行前確認項目
- [ ] 實用資訊分頁有當地資訊
- [ ] 行李清單已從 `template/data/packing.json` 複製並客製
- [ ] 首頁列出新行程

## 封存行程（Archive）

要從網站移除某趟旅行但保留資料：

1. 在 `trips/{slug}/data/trip.json` 加入 `"archived": true`
2. 重新 deploy：`direnv exec $REPO bash scripts/deploy.sh`

`build_index.py` 和 `deploy.sh` 都會跳過 archived trips。首頁不顯示、gh-pages 不部署，但本地資料完整保留。要恢復就移除 `"archived"` 欄位再 deploy。
