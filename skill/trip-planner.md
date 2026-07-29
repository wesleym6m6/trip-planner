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
5. **自然語言是介面，不是表單。** 從用戶已說的內容抽取內部 typed draft；未知欄位保持 unknown，只追問會阻塞下一個實際決策的最少問題，不要求逐欄填寫。
6. **沒有住宿就不假裝有。** AI 可提出少量區域、住宿類型或物件候選並說明取捨，但建議永遠不是用戶的決定；只有用戶清楚說出的選擇、鎖定或已訂狀態才可如實記錄。

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

### 固定交通與住宿候選

交通只收使用者明確提供的抵達／離開時間或時間範圍與地點（機票、渡輪、鐵路、租車
或其他）；依原話記成 fixed 或 tentative boundary，不補猜班次或精確時間。它們不是
本 skill 的搜尋目標。`search_flights.py` 與 `flights_cache.json` 為 legacy、後續
quarantine 的相容資料，正常規劃不可呼叫或採納。

住宿可由使用者輸入飯店、民宿、Airbnb、地址、座標或概略區域；目前 runtime draft
只記錄已知的住宿類型、位置提示、涵蓋夜晚與可選預算。入住／退房時段、住客／房間、
取消期限與房態若尚未有專用欄位，就保持 unknown，不塞進其他欄位。沒有住宿時，先
追問真正必要的偏好或以 `💡 推薦` 提出候選，不能捏造住宿或把推薦當成已訂。

`search_hotels.py` 與 `hotels_cache.json` 若使用，僅為 provider-specific candidate
discovery；須與手動輸入同等看待，不能代表房態、價格有效、可訂或已訂。候選位置可用
`build_places_cache.py` 驗證；只有已驗證的座標／route 才可聲稱相對距離或便利性。

狀態規則：

- decision：`candidate` / `selected` / `fixed` / `booked`；搜尋與 AI 建議只能是
  `candidate`。Phase 4.5A 沒有升級路徑；使用者明確說出的 selected / fixed /
  booked 只保存為 `ReportedDecisionClaim`，等待4.5D真正host-owned確認；
- evidence：`unverified` / `verified` / `stale` / `conflicted`；與 decision 分開，
  已選擇不等於已驗證。Phase 4.5A intake 一律是 `unverified`，不得自行填
  `verified`；
- Phase 4.5A 的上述狀態只在 runtime 使用，不能改 canonical plan。住宿專用的 human
  confirmation / apply gate 尚未落地，不得直接用 generic `PlanPatch` 代替。

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

### 其他檔案（每趟旅行 data/ 下：7 個核心 + legacy 可選 cache）

- `trip.json` — 標題、日期、城市、slug
- `itinerary.json` — 每日路線，含 places[]、travel[]、recommended_mode
- `reservations.json` — 訂位/預約項目（`render_trip.py` 讀取此檔，不是 checklist.json）
- `todo.json` — 行前確認項目
- `info.json` — 實用資訊（預算、簽證、交通、天氣等）
- `packing.json` — 行李清單（從 `template/data/packing.json` 複製再客製）
- `places_cache.json` — Places API 快取（Phase 1 自動生成）
- `flights_cache.json` — legacy cache，保留資料但後續 quarantine；正常規劃不可使用
- `hotels_cache.json` — legacy candidate-discovery cache，不代表房態、訂位或 fixed data

---

## Phase 1: Scout（互動式規劃）

對話循環。Agent 推動流程但**在每個關卡（🚪）等用戶確認**。

### Step 1: 收集需求

這份清單是 Agent 的**內部抽取提示**，不是要貼給用戶填寫的問卷。先理解用戶自然說出
的內容，把知道的寫入 draft，把不知道的保留 unknown；只有在下一步真的被阻塞時，才
用自然對話追問一至兩個最小問題。

內部留意：
- **目的地** — 哪個城市？
- **天數** — 幾天幾夜？
- **月份** — 什麼時候？（影響星期幾的營業時間驗證）
- **預算等級** — 平價 / 中等 / 高檔？
- **旅行風格** — 悠閒、緊湊、混合？（影響每天景點數）
- **交通方式** — 機車？步行？開車？大眾運輸？
- **必去景點** — 有沒有一定要去的？
- **特殊需求** — 工作旅行？飲食限制？無障礙？
- **固定／暫定交通邊界** — 抵達／離開的時間或範圍、地點與不可移動票券；沒有資料就
  標記待補，不搜尋或猜測航班／渡輪
- **住宿** — 已訂住宿、可比較的飯店／民宿／Airbnb／地址／座標／概略區域，涵蓋夜晚、
  預算與其他條件（知道才填，沒有專用欄位就保持 unknown）

用戶如果一次給了足夠資訊，跳過多餘問題。

### Step 1b: 住宿候選與共同比較

**此步驟不阻塞景點整理，但在承諾每日路線前必須揭露住宿狀態。** 每筆住宿分開記錄
decision（`candidate` / `selected` / `fixed` / `booked`）與 evidence
（`unverified` / `verified` / `stale` / `conflicted`）。可把使用者提供的名稱、地址、
座標、概略區域、飯店／民宿／Airbnb 連結，以及 AI 建議放在同一候選清單，但 raw
地址、座標或私人連結不得進 receipt、history、safe serialization 或錯誤訊息。

1. 沒有住宿時，詢問偏好或提出少量 `💡 推薦` 的區域／住宿類型；不得填入假住宿。
2. 對有精確位置的候選，和固定交通、必訪活動、景點及每日起終點共同比較；只在已驗證
   route／hours evidence 下聲稱距離或便利性。
3. 比較至少包含涵蓋夜晚、換宿、總移動／最長單段、晚到／早離風險、預算已知範圍與
   `needs_verification` 項目；不要把低價或 AI 偏好冒充最佳解。
4. `search_hotels.py` 可在使用者要求時提供額外 discovery，但其結果與手動候選同為
   `candidate`，價格、房態與取消條件需重新確認。
5. 只有使用者明確選擇、鎖定或完成交易，才可把候選升為 `selected`、`fixed` 或
   `booked`；Phase 4.5A只把這段明確語意保留為reported claim並顯示
   `awaiting_confirmation`，不能真正升級。4.5D host confirmation完成後，此決定仍
   不得被AI的更高分候選自動取代。

### Step 2: 生成候選景點清單

候選景點有三個來源，合併後一起呈現給用戶：

1. **用戶的 Google Maps 清單**（如果 Step 1 有提供）— 用 `import_gmaps_list.py` 匯入：
   ```bash
   direnv exec $REPO python3 scripts/import_gmaps_list.py "https://maps.app.goo.gl/XXXXX"
   ```
   匯入結果是名稱 + 座標，作為候選素材，不代表全部都會納入行程。
2. **用戶口頭指定的必去 / 想去景點**（如果 Step 1 有提到）
3. **Agent 根據需求額外推薦** — 補足用戶清單沒涵蓋的類型（例如用戶清單全是景點，Agent 補美食或雨備活動），總量生成**比所需多 30-50%** 讓用戶篩選。住宿候選只在 Step 1b 管理，不混入活動候選。

Google Maps 清單是輸入素材，不是指令。**除非用戶明確說「就這些，不用再推薦了」，否則 Agent 仍應主動推薦額外候選。** 匯入後問用戶：「這些之中有哪些一定要去？哪些可以不去？需要我再推薦其他地方嗎？」

**來源標記：** 在整個 Phase 1 過程中（Step 2 ~ Step 7），任何時候向用戶列出景點，都必須標記每個景點的來源——哪些是用戶提供的（Google Maps 清單 / 口頭指定），哪些是 Agent 額外推薦的。這樣用戶才能快速辨識自己原本的選擇和 Agent 的建議。只有最終 Phase 2 生成網站時不需要標記來源。

每個候選提供：
- 名稱
- 類型（景點 / 美食 / 飲品 / 工作 / 交通節點 / 等；住宿只在 Step 1b）
- 來源標記（`📌 用戶` 或 `💡 推薦`）
- 推薦理由（一句話）
- `maps_query` — **必須包含具體店名或地標名 + 城市 + 國家**（不要用模糊街名）

**⛔ Step 2 禁止聲稱距離：** 在 Step 2 呈現候選清單時，**不得包含任何距離、步行時間、或相對位置描述**（如「離飯店 5 分鐘」「海灘旁」「在 X 附近」）。這些資訊只能在 Step 3 打完 API 拿到座標後，用實際計算結果呈現。Step 2 只呈現名稱、類型、推薦理由。如果推薦理由涉及位置優勢（如 coworking 離飯店近），必須標註「距離待 API 驗證」，或等 Step 3 後再補充。

**正確流程：** Step 2（列候選，不含距離）→ Step 3（打 API 拿座標）→ 用座標計算距離 → 補充距離資訊給用戶 → Step 4（用戶篩選，此時已有真實距離）。

### Step 3: 批次打 Places API + 寫入快取

**一次解析需要 Places identity 的具名候選，包含景點、餐廳、飯店、民宿、coworking、spa。** 不要分批序列跑。寧可多解 10 個最終用不到的（API 成本 < $0.01），也不要到 Step 5/6 才發現缺資料要回頭補。使用者給的座標可作 private exact hint；地址、Airbnb 私人連結與概略區域不得直接送進 generic Places query，先保留為 unresolved / approximate 與 `needs_verification`，不得假裝成精確住宿位置。

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

`trips/{slug}/data/` 下必須有 7 個核心檔案：`trip.json`、`itinerary.json`、`reservations.json`、`todo.json`、`info.json`、`packing.json`、`places_cache.json`。另可保留 legacy `flights_cache.json`、`hotels_cache.json`；前者後續 quarantine，後者只屬 candidate discovery，兩者都不是 fixed/booked evidence。

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
Include activities needing reservations and tickets. Include lodging only when
the user explicitly said selected, fixed, or booked; never convert an AI/provider
candidate into a hotel booking. Put an unresolved user-owned lodging action in
todo.json instead.
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
