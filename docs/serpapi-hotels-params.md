# SerpApi Google Hotels 參數參考

engine: `google_hotels` | 歷史行為曾用bounded private fixture驗證；exact query、日期、party與結果快照不進Git

## 參數表

**必填：**

| param | type | default | values / format | notes |
|-------|------|---------|----------------|-------|
| `engine` | str | — | `"google_hotels"` | 固定值 |
| `q` | str | — | 自由文字 `"Example City hotels"` | 可加入synthetic area placeholder縮小範圍 |
| `check_in_date` | str | — | `YYYY-MM-DD` | |
| `check_out_date` | str | — | `YYYY-MM-DD` | |

**⚠️ 地域參數（影響搜尋結果正確性）：**

| param | type | default | values / format | notes |
|-------|------|---------|----------------|-------|
| `gl` | str | **無** | ISO country code | **必須設為目的地國碼**。未設或設錯可能讓結果偏向其他地區；腳本無預設值，caller應根據目的地明確傳入 |

**高頻使用：**

| param | type | default | values / format | notes |
|-------|------|---------|----------------|-------|
| `adults` | int | `2` | 正整數 | |
| `currency` | str | `"USD"` | `"TWD"`, `"VND"` | 影響價格和 min/max_price 單位 |
| `hl` | str | `"en"` | `"zh-TW"`, `"ja"` | 影響飯店名稱/設施語言 |
| `sort_by` | int | 相關度 | 3=最低價, 8=最高評分, 13=最多評論 | |
| `hotel_class` | str | — | 逗號分隔星級 `"4,5"`, `"2,3"` | 嚴格篩選 |
| `min_price` | int | — | 幣別單位 | 基於稅前價，含稅可能略超 |
| `max_price` | int | — | 幣別單位 | 同上 |
| `rating` | int | — | 7=3.5+, 8=4.0+, 9=4.5+ | 只三檔；需更精確用本地篩選 |
| `amenities` | str | — | 逗號分隔 ID（見下表） | 多個 = AND（全部必備） |
| `free_cancellation` | bool | `false` | `true` | 規劃初期建議開啟 |

**住客：**

| param | type | default | values / format | notes |
|-------|------|---------|----------------|-------|
| `children` | int | `0` | 正整數 | 須搭配 `children_ages` |
| `children_ages` | str | — | 逗號分隔年齡 `"5"`, `"3,7"` | 數量須與 children 一致 |

**品牌/類型篩選：**

| param | type | default | values / format | notes |
|-------|------|---------|----------------|-------|
| `brands` | str | — | 逗號分隔 ID `"28"` | 母品牌含子品牌；ID 從結果 `brands[]` 取 |
| `special_offers` | bool | `false` | `true` | 有特惠的飯店 |
| `eco_certified` | bool | `false` | `true` | 環保認證；約半數有 |
| `vacation_rentals` | bool | `false` | `true` | 完全切換為度假屋/民宿（非混合顯示） |
| `property_types` | str | — | ID 如 `"17"` | ⚠️ 部分 ID 會 400，實用性有限 |

**分頁/詳細：**

| param | type | values / format | notes |
|-------|------|----------------|-------|
| `next_page_token` | str | 從 `serpapi_pagination.next_page_token` 取 | 每頁 20 筆；消耗 1 次額度 |
| `property_token` | str | 從 `properties[].property_token` 取 | 查單一飯店詳細（地址、電話、OTA 比價、30 項評論分析） |

## Amenity ID 對照

| ID | 設施 | ID | 設施 |
|----|------|----|------|
| 1 | Free Wi-Fi | 9 | Pool |
| 2 | Free parking | 12 | Kitchen |
| 4 | Restaurant | 19 | Accessible |
| 5 | Fitness center | 22 | Laundry |
| 6 | Pet-friendly | 25 | Airport shuttle |
| 7 | Spa | 29 | Beach access |
| 33 | Bar | **35** | **Free breakfast** |

常用：`"35,9"` 含早餐+泳池、`"35,2"` 含早餐+免費停車

## 常見品牌 ID

| ID | 品牌 | 子品牌 |
|----|------|--------|
| 28 | Hilton | 54=Hilton Hotels, 71=Garden Inn, 286=Tru |
| 33 | Accor | 47=Novotel, 90=Pullman, 91=Mercure |
| 37 | Hyatt | 122=Hyatt Regency |
| 17 | IHG | 2=InterContinental, 42=Crowne Plaza |
| 289 | Four Seasons | — |

母 ID 篩選包含所有子品牌。ID 從搜尋結果 `brands[]` 欄位取得。

## ⚠️ Filter 導致地理擴散（二線城市陷阱）

Google Hotels 固定回傳 20 筆。當 filter 條件太嚴、目的地城市的符合結果不夠 20 筆時，**SerpApi 會自動擴散搜尋範圍到整個國家甚至跨國**，用遠處的飯店填滿頁面，且不會標記哪些是擴散結果。

Historical bounded research observed that a strict filter can make returned properties drift
outside the requested city when the local result set is small. Exact query, country, hit ratio,
price threshold and expanded destinations are private research context and are intentionally
omitted. `sort_by=3`（最低價排序）是高風險訊號，但不能假設任何城市永遠安全。

**防禦做法：**
1. 先不帶 filter 搜一次確認地理命中正常
2. 逐步加 filter，每次檢查結果座標是否仍在目的地
3. 用 `max_price` 替代 `sort_by=3` 做價格篩選（前者限制範圍，後者改變排序邏輯導致擴散）
4. 如果必須用 `sort_by=3`，caller 應在收到結果後用 `gps_coordinates` 做本地過濾

## 注意事項

1. **每頁固定 20 筆**，翻頁用 `next_page_token`（消耗額度）
2. **`min_price`/`max_price` 基於稅前價**，含稅後可能略超
3. **`vacation_rentals=true` 完全切換結果集**，不是同時顯示飯店+民宿
4. **`property_token` 詳細查詢消耗 1 次額度**，回傳地址/電話/所有 OTA 比價/30 項評論分析
5. **`children` 必須搭配 `children_ages`**，否則用預設年齡

## 回傳結構

```
properties[20]               飯店列表（每頁 20 筆）
├── name / type / link
├── property_token           查詳細用
├── gps_coordinates          {latitude, longitude}
├── check_in_time / check_out_time
├── rate_per_night           {lowest(str), extracted_lowest(int), before_taxes_fees}
├── total_rate               {lowest(str), extracted_lowest(int)}
├── deal                     特惠標籤（如 "比市價便宜 30%"）
├── hotel_class / extracted_hotel_class(int)
├── overall_rating / reviews / location_rating
├── amenities[]              ["Free breakfast", "Pool", ...]
├── nearby_places[]          [{name, transportations[{type, duration}]}]
├── ratings[]                星級分布 [{stars, count}]
├── reviews_breakdown[]      分類評論 [{name, positive, negative}]
└── images[]                 [{thumbnail, original_image}]

brands[]                     品牌篩選用（含子品牌 children）
ads[]                        贊助結果
serpapi_pagination           {current_from, current_to, next_page_token}

property_token 額外回傳：address, phone, prices[], featured_prices[],
  typical_price_range, amenities_detailed{groups[]}, reviews_breakdown[30]
```

## 歷史 bounded research 結論

Exact query、dates、party、result counts、prices、brands與token-derived snapshots不保存於
public repository。可保留的結論只有：sorting與filters會改變集合、複合filters可能造成
地理擴散、occupancy會改變price semantics、pagination與property detail各自消耗額度，且
所有結果都需要caller以目的地範圍重新驗證。這些是歷史觀察，不是current provider保證。
