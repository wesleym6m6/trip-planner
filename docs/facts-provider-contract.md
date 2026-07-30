# Facts and Provider Contract

狀態：Phase 4 implementation contract（4.4 Places profile / hours offline exit gate）
契約版本：`fact-query/v1`、`fact-observation/v1`、
`evidence-snapshot/v1`、`provider-result/v1`、
`place-identity-review/v1`

## 目的

Phase 4 讓 planner 使用真實、可追溯且有期限的外部資料，但不讓 provider、
cache 或研究文字取得修改 canonical plan 的權限。

核心資料流固定為：

```text
AI / research
  └─ query intent、candidate、fact requirement
                    │
                    ▼
trusted host ── provider adapter ── normalized observation
                                      │
                                      ▼
                             trusted policy gate
                              ┌───────┴────────┐
                              ▼                ▼
                    run-scoped memory   authorized disk cache
                    evidence            (explicit policy only)
                              └───────┬────────┘
                                      │
canonical plan ───────────────────────┤
                                      ▼
                     immutable composed TripState
                                      │
                                      ▼
                            kernel / scheduler
                                      │
                                      ▼
                         reviewed PlanPatch → TripStore
```

Provider 只能更新 evidence。任何行程變更仍須經既有 PlanPatch、preview、
approval、CAS、kernel validation 與 atomic replace。

## ADR-008：canonical intent 與 provider evidence 分離

`plan.json` 與永久 rollback history 只保存：

- 使用者意圖、decision、flexibility、constraints；
- stable activity / location identity；
- 經 provider policy 明確允許長期保存的 identity，例如 Google `place_id`；
- 使用者明確採納的私人訂位或人工估計。

以下 provider values 不進 canonical plan、receipt 或永久 history：

- 座標、地址、營業時間、business status；
- route duration、distance、transit steps；
- 航班／飯店搜尋價格與 availability；
- reviews、photos、editorial 或 generative summaries；
- raw response、booking/departure token。

原因不只是架構整潔。Google Places／Routes 的大部分內容受 caching restriction；
`place_id` 才是明確的長期保存例外。TripStore 會永久保存 before-snapshot，
因此把受限制內容先寫進 canonical 再標 stale，仍無法在 retention deadline
後真正清除。

Provider evidence不建立單一「有 TTL 就能落盤」的 store，而分成：

- run-scoped memory evidence：供當次 planning snapshot使用，不跨 process；
- authorized disk cache：只有 static policy明確給 `DISK_TTL` 或
  `INDEFINITE_ID` 的 normalized fields；
- disk cache使用 current-state atomic replace，不建立永久 observation
  history；
- evidence消失只會讓行程回到 `needs_verification`；
- evidence refresh不直接變更 plan revision或persistent-change budget。

`purge_at` 只限制「最晚何時刪除」，不會創造 storage permission。標準
non-EEA Google Maps條款下，V1 fail-closed profile是：

- Google `place_id`：`INDEFINITE_ID`，語意上仍每 12 個月 refresh；
- 條款雖明示Places lat/lng可暫存最多30天，但V1尚未建立不和full profile
  source slot衝突的coordinate-only fact，因此先保守採`MEMORY_ONLY`；
- Places opening hours、display name、business status與其他 Places content：
  `MEMORY_ONLY`；
- Routes duration、distance、time與 path content：`MEMORY_ONLY`；
- 複合 fact只要包含任一memory-only field，整筆採最嚴格的`MEMORY_ONLY`。
  未來只有實測需要跨process coordinate cache時，才新增獨立
  `place_coordinates` kind；不以policy ID硬拆同一semantic source slot。

參考政策：

- [Places API policies and caching exception](https://developers.google.com/maps/documentation/places/web-service/policies)
- [Google Place ID storage and refresh](https://developers.google.com/maps/documentation/places/web-service/place-id)
- [Routes API policies](https://developers.google.com/maps/documentation/routes/policies)
- [Google Maps Platform general terms](https://cloud.google.com/maps-platform/terms)
- [Google Maps Platform service-specific terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)

Provider policy可能因帳單地區、合約與日期不同；trusted host使用 immutable
`ProviderPolicyRegistry`。每個 policy綁 provider / adapter / version /
contract region、allowed fact kinds / value fields / query fields、persistence、
validity / retention上限與required attribution。未知 region、policy、provider
或field一律fail closed。policy ID、confidence、`valid_until`與`purge_at`即使
長得合理，也不能由AI、response content或query自行授權。

V1唯一內建的production catalog由
`google_maps_policy_registry("google-maps-non-eea-2026-06-10")`建立。
它只授權Google place ID永久保存；Places profile/hours與Routes均為
`MEMORY_ONLY`，不提供Google `DISK_TTL` policy。不同region或未經審查的新
contract profile不會猜測相近條款，而是直接拒絕。

## ADR-009：freshness 與 retention 是兩個時鐘

每個 observation 都包含：

- `retrieved_at`：trusted host 收到並驗證資料的時間；
- `valid_until`：資料在產品語意上仍適合做決策的期限；
- `purge_at`：provider content 最晚必須從 evidence cache 移除的期限。

兩者不可合併：

- `valid_until` 已過、`purge_at` 未到：可以作可追溯的 stale LKG，但不能
  travel-ready；
- `purge_at` 已到：即使資料看似仍 fresh，也必須移除 value，kernel 只能看到
  missing / unverified；
- 實際可依賴期限永遠不晚於兩者較早者。

`purge_at`不是UI建議。run-scoped ledger與authorized disk store在read、
merge、write及explicit cleanup都必須fail closed；expired content不可因stale
review、receipt或cache replay復活。

兩個判斷使用不同時鐘：

- semantic freshness 使用此次規劃固定的 `evaluation_at`；
- compliance deletion 使用 trusted host 的當前 `purge_now`。

把 `evaluation_at` 設回過去不能讀回現在已到 retention deadline 的內容；
`purge_now` 不由 AI、trip date 或 replay request提供。
Phase 4.0的pure functions要求caller顯式傳入`purge_now`，但這只是測試與組合
邊界；Phase 4.1的`EvidenceStore`必須從不可由AI/API覆寫的trusted wall clock
注入它，並在read、merge、write及cleanup共用同一判斷。舊receipt或replay
不得自帶過去時間繞過刪除。

## 最小 fact contract

V1 只建立少量 enum，不建立 fact DSL、knowledge graph 或動態 plugin registry。

### `FactKey`

一個 claim 的完整 scope：

```text
kind
subject_ids
qualifiers
key_id
```

第一批 kind：

- `place_identity`
- `place_profile`
- `place_opening_hours`
- `route_estimate`
- `flight_offer`（legacy reservation；Phase 4.5 不啟用）
- `hotel_offer`（legacy discovery reservation；不得代表訂位）

Phase 4.0 先落地 place identity/profile、opening hours 與 route value schema；
flight/hotel enum只保留 legacy roadmap identity，未有明確 policy 前一律回
`UNSUPPORTED_FACT_KIND`。Phase 4.5 不建立航班搜尋；固定交通是 user-owned
intent。住宿可由飯店、民宿、Airbnb、地址、座標或概略區域提出為候選；未解析的
地址或概略區域可先參與保守比較，但必須揭露不確定性。只有具 exact identity／route
evidence 才可聲稱距離或便利性，任何候選都不構成訂位或可用性。

Qualifier是kind-specific allowlist、排序後的scalar tuple；unknown／拼錯欄位
fail closed，不接受filesystem path、API key、authorization、secret或provider
session token。Place identity key明確綁 `identity_provider`，profile / hours再
綁exact `provider_place_id`，避免不同provider identity互相假衝突。Route key
至少綁定：

- ordered origin / destination identity；
- exact mode；
- departure / arrival context；
- 必要的 routing preference。

Legacy hotel discovery 若被使用，key 必須完整綁定日期、住房人數、房型 scope、
currency 與 provider offer identity；不完整 scope 的價格不可互相比較。它只可產生
tentative lodging candidate，不得把 property token、價格或 availability 升格為
selected/fixed/booked。flight offer 仍在 reservation / HTTP 前停止。

### `ProviderRequest`

Outbound intent使用不可變的exact envelope：

```text
provider / adapter / adapter_version / operation
FactKey set
policy_id + policy_digest
allowlisted normalized query scope
request_fingerprint
```

Fingerprint由完整envelope deterministic計算；`ProviderResult`只有相同digest
仍不代表可信。trusted promotion gate還會拿request preimage重新驗證exact
requested key set、source、policy與batch coverage。query values只留在當次
request object；durable diagnostics只保存fingerprint與field names。

### `ProviderProvenance`

每筆 observation 至少保存：

```text
provider_id
adapter_id + adapter_version
request_fingerprint
provider_record_id / response_id（若有）
retention_policy_id
source URI 與必要 attribution（若有）
```

Fingerprint只由normalized non-secret query與exact static policy產生；不包含
API key、cache path、wall-clock timestamp或raw provider URL。source URI只能是
credential-free public URI，不能拿signed request URL當provenance。

### `FactObservation`

```text
observation_id
FactKey
kind-specific normalized value
ProviderProvenance
retrieved_at
valid_until
purge_at
confidence
```

- `observation_id` 由完整 normalized content deterministic 計算。
- `confidence` 由 trusted adapter 的 identity/match policy計算；不是 authority。
- 高 confidence 不會自動把 research hint 升級成 verified。
- Raw response、任意文字與 provider instructions 不屬於 normalized value。
- 不同 kind 使用固定 value schema；unknown field 或 binding mismatch fail closed。

### Evidence resolution

沿用既有 `EvidenceState`：

- `verified`：有 eligible LKG、尚 fresh、未衝突；
- `stale`：LKG 已過 semantic validity，但仍在合法 retention 內；
- `conflicted`：同一完整 FactKey 有 material disagreement；
- `unverified`：沒有合格 observation、只有 research hint，或 binding 不符。

`verified`代表observation在其schema與時間內可信，不等於任何用途都
travel-ready。例如fresh `regular_typical` opening schedule仍只可支援draft；
date-specific hard open必須有fresh `current` / special-hours evidence，並在
coverage結束前重驗。V1用`supports_travel_ready_use`明確分開這兩層語意。

同 provider、同 FactKey 的較新 observation 取代較舊 LKG。不同 provider
的相同 normalized value可互相支持；materially different values 保留為
conflict candidates，不以 confidence 平均或 lexical tie-break 偷選 winner。

高變動 offer 的新價格通常是同 source 的時間序列更新；FactKey 必須含 offer
identity，不能把不同 property／flight 或不同 query scope 泛化成 conflict。

## Last-known-good 與 partial failure

Raw provider result固定分成：

- `success`：至少一筆完整、可驗證 observation，沒有 item failure；
- `partial`：同批同時有 valid observations 與 typed item failures；
- `failed`：沒有 valid observation，至少一個 typed failure；
- `cache_hit`：exact key、scope、policy 與 freshness 均可重用，zero outbound
  attempt。

以下結果不得覆寫 LKG：

- timeout、rate limit、provider unavailable；
- auth / quota failure；
- malformed / out-of-scope response；
- ambiguous match；
- generic empty response 或 not-found；
- unsupported mode。

Partial batch 只 merge 成功的 exact keys。失敗 key 的既有 LKG 繼續保留至
`purge_at`；不可用空 dict、空 modes 或 `null` 覆寫。整批沒有新 observation
時 evidence semantic revision 不變，唯獨到期 purge 仍可使 revision 前進。

Explicit negative fact 只有在 endpoint contract 能證明其語意、adapter 正規化成
具期限的 typed observation 時才可取代 LKG；「API 回空陣列」本身不是 negative
fact。

Raw result不能直接merge。`authorize_provider_result()`先驗：

- observation / problem keys完整覆蓋exact request；
- success與failure key sets不重疊；
- 每個source slot最多一筆observation；
- provenance source、fingerprint、policy完全相同；
- validity、retention、persistence與attribution符合static policy。

Google place identity是更窄的例外：generic
`authorize_provider_result()`一律拒絕`resolve-place`與`refresh-place-id`；
candidate assessment與review只能由strict evaluator mint，review／refresh
finalizer再直接產生gate-bound `AuthorizedProviderResult`。Merge時仍以同一
specialized gate重驗。只有`AuthorizedProviderResult`可進
`merge_provider_result()`。

## EvidenceStore current-state boundary

Phase 4.1A 的 durable evidence 只使用一個 per-trip current-state 檔：

```text
trips/{slug}/data/.trip-planner-evidence.json
```

- 與 canonical `TripStore` 共用 `.trip-planner.lock`，每次 load、merge、cleanup
  都在同一把 exclusive lock 內重讀 current state，避免 lost update；
- 只接受目前 static policy明確允許的 `DISK_TTL` / `INDEFINITE_ID`；
  `MEMORY_ONLY` result在取得 lock、建立 temp或讀取disk前就拒絕；
- 不建立 history、receipt、backup或資料庫；provider values也不進plan；
- store自行注入trusted UTC clock，每個locked operation只採樣一次；
- read與merge都先做retention deletion；cleanup是同一條idempotent path；
- 寫入採same-directory temp、file fsync、atomic replace、directory fsync；
  replace call開始後任何失敗一律是`CACHE_OUTCOME_UNKNOWN`，caller必須reload並
  比對`expected_revision`；
- process在replace前死亡留下的reserved temp，下一個lock owner會驗證為regular
  file、刪除並fsync directory，避免TTL bytes脫離retention lifecycle；
- strict decoder先要求目前trip ID、stored policy registry / policy digest、
  record/value digest、normalized order與canonical UTC bytes自洽；registry
  revision升級時，只有通過舊文件自身canonical bytes與store revision驗證的
  records才可逐筆接受目前policy重新授權。仍合法的records保留，不再合法的
  records刪除，並在同一把lock內atomic rewrite；格式、trip/schema或digest
  真正損毀時不得偽裝成migration；
- cache schema `evidence-store/v2`加入256-bit `store_epoch`。正常merge與purge
  保持epoch；目前trip的每一次corruption reset都由CSPRNG取得新的256-bit nonce，
  並與domain-separated raw-content digest綁成新epoch（oversize時raw digest只
  涵蓋bounded prefix與file size）、generation歸零並atomic replace成canonical
  empty cache，避免
  corruption讓受限制bytes脫離retention lifecycle，也避免empty-state ABA；
- 唯有可明確辨識為其他trip的cache，以及strict-valid但只因trusted clock
  rollback而含future observation的cache，才fail closed且保留原bytes。若同一
  strict-valid cache同時含retention-expired與future records，privacy deletion
  優先，整個current-trip cache清空；
- writer與reader共用4,096-record、16 MiB與signed 64-bit generation界線。
  generation飽和後禁止promotion，但不可逆retention deletion仍可繼續，並由
  exact record-set store revision防止ABA。

`EvidenceLedger.revision`是provider LKG的semantic revision；durable
`EvidenceStoreResult.current_revision`另外綁定trip ID、store schema、policy
registry、store epoch、generation與record wrappers，兩者不可互換。從disk result建立
snapshot必須使用：

```text
store_result.snapshot(evaluation_at=...)
```

這個入口不接受caller提供`purge_now`，並把exact current store revision帶入
`EvidenceSnapshot`。一般`to_dict()`仍只輸出redacted diagnostics，不輸出
normalized provider values。

## Composite snapshot 與 race safety

Runtime composition 是純函式：

```text
compose_trip_state(
    canonical_plan,
    evidence_snapshot
) -> ComposedTripState
```

它只在記憶體投影 normalized coordinates、opening windows、route estimates
與 evidence refs；`evaluation_at`只有`EvidenceSnapshot.evaluation_at`一個來源，
不回寫 canonical plan。回傳wrapper而非裸`TripState`，因為live attribution與
exact evidence binding不能遺失。

Evidence snapshot建立時先用 `purge_now`移除不可再保存的 records，再用固定
`evaluation_at`判斷剩餘 records是 fresh或 stale。

完整snapshot只存在記憶體。跨review / receipt / history保存的是redacted
`EvidenceBinding`：policy/store/evidence revision、snapshot digest、evaluation /
purge time、observation refs、expiry與attribution requirement；不含provider
value bytes。run-scoped evidence在process結束或遺失後，舊review必須stale /
refetch，不能靠digest把值復活。

`repr()`、一般`to_dict()`與durable binding同樣只能輸出redacted identity /
digest，不得輸出normalized value、query value、response ID、source URI或
動態attribution URL。這不代表attribution可以丟失：compose必須從仍存活的
live observation把完整sanitized attribution帶到renderer。若binding標記
`requires_live_attribution=true`但live observation已不存在，readiness與
delivery必須回stale/refetch；不得只靠durable binding呈現provider content。

任何依 evidence 產生的決策都必須綁定：

- exact `plan_revision`；
- exact `evidence_revision` / active-record digest；
- exact timezone-aware `evaluation_at`；
- exact composed state / report digest。

因此後續需把 `evidence_revision` 納入：

- `PlannerSnapshot`；
- `ScheduleProblem`；
- AI proposal review；
- schedule staging review。

Provider refresh 不需和 plan 做雙檔 transaction，因 refresh 本身不能改 plan。
但 plan commit 前必須重新 compose / replay；evidence changed 時舊 review回
`EVIDENCE_REVISION_CHANGED`，不能偷偷套用新 facts 或沿用舊 score。

## Provider budget、timeout 與 retry

兩層 budget 不混淆：

- Repair run 的 unique logical query budget；
- Provider runner 的實際 outbound attempt / billable unit budget。

每個實際 HTTP attempt 都必須計數。Adapter 不得內藏三次 retry 卻只回報一次。
Batch fan-out（例如 route edge × mode）要在呼叫前展開並檢查上限。

Retry policy：

- 所有 network calls 都有 explicit connect/read timeout；
- 只對 allowlisted transient failure重試；
- 429、5xx、connection timeout 與 permanent 403/auth failure分開；
- backoff 仍受 remaining attempt budget；
- exception、log 與 result 都不得包含 API key、完整帶 credential URL 或 raw
  provider response。

只有validated observation通過policy promotion，且依其persistence成功進入
run-scoped ledger或authorized disk cache後，logical reservation才可標
completed cache hit。Failed/in-flight reservation不能假裝cached。

## Research content trust boundary

`ResearchHint` 與 `FactObservation` 是兩條不同路徑。

AI 可以：

- 提出 normalized provider query；
- 提議 candidate；
- 在當前 review 中引用 observation ID 解釋方案；
- 根據 kernel issues 提出 PlanPatch。

AI提出的query仍只是intent；trusted host負責建立`ProviderRequest`、挑選static
policy、填入request fingerprint，AI不能提供policy digest或authorized result。

AI、reviews、網頁、社群貼文、editorial/generative summary 不可以：

- 建立 provider observation；
- 設定 confidence、validity、retention 或 `verified`；
- 選擇 conflict winner；
- 建立 fixed/booked decision、hard constraint 或 approval；
- 把文字內的指令、路徑、URL 或 tool call當作可執行命令。

需要升級 research claim時，只能經 allowlisted adapter再次驗證，或由使用者
透過精確 human confirmation採納。Provider response本身也不能產生 PlanPatch。

## Provider-specific correctness

### Google Places

- Search 不可默認取第一筆；name、city/country、type、geographic boundary與
  ambiguity policy 必須通過才可建立 identity fact。
- Identity Text Search固定`pageSize=5`並綁exact minimal field mask；回應若有
  `nextPageToken`、多個eligible candidate、非exact token-boundary name match，
  或既有LKG將被換成不同place ID，一律進30分鐘human review，不得auto-promote。
- display name、address、coordinates、types、locality與pagination token只存在
  ephemeral review payload；review畫面固定附Google Maps attribution。Promotion
  前要以host clock與目前evidence/store revision重新驗證。
- `place_id` 可長期保存，但官方建議超過 12 個月刷新。
- ID-only refresh只能從trusted EvidenceSnapshot中的既有fresh/stale Google
  identity LKG建立；provider若回不同ID，必須重新走candidate search與review。
- 標準policy只有lat/lng有明確30天disk TTL；hours、display name與business
  status採`MEMORY_ONLY`，不能因為設定`purge_at=30d`就落盤。
- `currentOpeningHours` 只覆蓋 request day起算的 7 天，包含 special hours；
  `regularOpeningHours` 只是 typical schedule，不能證明遠期假日營業。
- Place Details分成profile、current hours、regular hours三個exact request；各自
  使用固定minimal field mask，並把identity endpoint、observation/value、
  evidence snapshot、durable store revision、locale與target dates綁進
  fingerprint。原始Place ID只出現在runtime URL，不進safe binding或repr。
- Current-hours七日起點綁實際send instant在place timezone的日期，不使用可能
  已跨午夜的response completion date。Point的date/day、truncation、overnight
  與DST local time都需一致；ambiguous或nonexistent wall time fail closed。
- `periods`缺席代表unknown；明確空陣列代表never open。Current periods會轉成
  date-specific half-open UTC intervals及完整closed dates；regular periods只
  投影typical schedule，永遠不能成為travel-ready hard constraint。
- Adapter只接受injected transport與bounded bytes；64 KiB、strict UTF-8 JSON、
  duplicate key、NaN、depth/node、unknown field、HTTP/transport錯誤與每次實際
  send budget均有typed boundary。Credential由transport自行持有。
- `weekdayDescriptions` 的順序依 locale，不可假設 Monday-first；execution
  validator使用 machine-readable periods。
- 只 request product需要的最小 field mask。Reviews、photos、generative
  summaries預設不抓。

參考：

- [Places resource and opening-hours semantics](https://developers.google.com/maps/documentation/places/web-service/reference/rest/v1/places)

### Google Routes

- Route observation綁 exact directed arc、mode 與 departure context。
- Adapter固定使用`POST /directions/v2:computeRoutes`、Place ID waypoint與
  最小field mask；endpoint observation/value、evidence snapshot與durable
  store revision全部進request fingerprint。API credential只由injected
  transport持有，不進request model、safe binding或測試fixture。
- Response先以64 KiB、JSON depth/node count、duplicate key、NaN與單一路線
  邊界驗證，再轉成allowlisted normalized value；raw bytes、provider message、
  Place ID與自由文字warning不進ledger、diagnostic或repr。
- 標準policy的duration、distance、time與path採`MEMORY_ONLY`；跨process
  planning需refetch，redacted digest不是cache value。
- `EvidenceSession`每次load/merge都重讀durable source；durable revision漂移
  會清除run-scoped route LKG，舊response也因`basis_store_revision`不符而拒絕。
  Observation revision與完整typed provider outcome分別綁入snapshot/review
  identity，global或multi-key problem不被改寫。
- 實際HTTP attempt使用獨立thread-safe budget，預設每個request只送一次；
  opt-in retry每個send都計費且單request最多三次。Batch上限256個exact
  requests，duplicate或evidence drift在送出前fail closed。
- Transit query若沒指定時間，provider會使用 query執行當下，不可拿來驗證未來
  行程。
- Transit schedule的官方 query horizon為目前時間前 7 天、後 100 天；超出
  horizon回 typed precondition，不浪費 API call。
- Walking、bicycle與 two-wheeler route仍有官方 beta/path warning，delivery
  必須揭露。
- Driving fallback只能建立 driving fact並附typed
  `TRANSIT_UNAVAILABLE`問題與runtime disclosure，不得冒充verified transit。
  Google response的`fallbackInfo`只表示provider內部routing preference
  fallback，不等同產品的transit-to-driving fallback。
- Scooter proxy不是 Routes fact；若保留，只能是明確的 unverified derived
  estimate，不能把 bicycle duration乘 0.5後仍標 `source=api`。
- Departure time來自 kernel timeline的實際離開時間，不是 activity start。

參考：

- [Compute Routes REST method](https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes)
- [Compute a route and field masks](https://developers.google.com/maps/documentation/routes/compute_route_directions)
- [Waypoint Place ID contract](https://developers.google.com/maps/documentation/routes/reference/rest/v2/Waypoint)
- [Transit route horizon and parameters](https://developers.google.com/maps/documentation/routes/transit-route)
- [Routes travel modes and beta warnings](https://developers.google.com/maps/documentation/routes/reference/rest/v2/RouteTravelMode)
- [Routes error handling](https://developers.google.com/maps/documentation/routes/handle-errors)
- [Routes `FallbackInfo`](https://developers.google.com/maps/documentation/routes/reference/rest/v2/FallbackInfo)

### Legacy SerpApi hotel discovery 與 flight quarantine

- Phase 4.5 不提供 flight search；`search_flights.py`、flight cache 與其 tokens
  保留供歷史相容與後續 quarantine，不是正常產品路徑，也不得 promotion；
- `search_hotels.py` 若使用，只是 provider-specific discovery input。Airbnb、民宿、
  地址、座標與概略區域必須可同等進入住宿候選；
- `search_metadata.status`、top-level `error` 與 empty-success 必須分開，且 query
  scope與 provider search ID不可在 normalization時丟失；
- 4.5B的`normalize_serpapi_hotel_discovery()`只接受caller提供的bounded raw
  response與trusted completion time，不執行HTTP。Request完整綁query、dates、
  occupancy、currency/minor unit、region與language；unknown metadata status fail
  closed，search/property identity只留process-HMAC ref；
- normalizer result只是non-provenance DTO；status、provider search ref與diagnostic
  ref都不可作authorization、cache hit、evidence、receipt、history或住宿決策依據。
  真正evidence consumer只接受既有typed `EvidenceSnapshot`，不接受discovery result；
- booking/departure/property token只作短期 provider session，不進 durable fact；
- price若可精確normalize，必須與currency/minor unit、occupancy及日期完整綁定，
  且只可標為tentative。4.5B不產生availability claim；未來provider若提供
  availability，也必須綁相同完整scope並有獨立freshness policy；
- 搜尋或 AI 建議都不是已訂位。Phase 4.5A 的所有 binder 只能建立 process-local
  candidate + unverified；清楚的使用者 selected / fixed / booked 語意只保留為
  non-authoritative `ReportedDecisionClaim`與opaque source ref，不能提升candidate；
- canonical lodging mutation 必須等 Phase 4.5D 的住宿專用 confirmation grant 與
  apply gate，再疊加既有 PlanPatch / preview / approval / validation；不能直接把
  generic PlanPatch 當成住宿決定的充分授權。

參考：

- [SerpApi status and error semantics](https://serpapi.com/api-status-and-error-codes)

## Typed failure taxonomy

Contract：

- `INVALID_PROVIDER_REQUEST`
- `INVALID_PROVIDER_RESPONSE`
- `UNSUPPORTED_FACT_KIND`
- `UNTRUSTED_PROVENANCE`
- `EVIDENCE_BINDING_MISMATCH`

Budget / precondition：

- `PROVIDER_BUDGET_EXHAUSTED`
- `PROVIDER_CALL_RESERVED`
- `PENDING_REVIEW`
- `EVIDENCE_REVISION_CHANGED`
- `OUTSIDE_PROVIDER_HORIZON`

Transport：

- `AUTH_FAILED`
- `QUOTA_EXHAUSTED`
- `RATE_LIMITED`
- `TIMEOUT`
- `PROVIDER_UNAVAILABLE`

Semantic：

- `EMPTY_RESPONSE`
- `NOT_FOUND`
- `UNSUPPORTED_MODE`
- `TRANSIT_UNAVAILABLE`
- `AMBIGUOUS_MATCH`
- `OUT_OF_SCOPE_RESULT`
- `PARTIAL_FAILURE`

Evidence / cache：

- `STALE_EVIDENCE`
- `CONFLICT_DETECTED`
- `RETENTION_EXPIRED`
- `STALE_PROVIDER_RESULT`
- `CACHE_CORRUPTED`
- `CACHE_WRITE_FAILED`
- `CACHE_OUTCOME_UNKNOWN`

每個 failure帶 stable code、`retryable` 與 `next_action`；不保存 raw exception
text。`retryable=true` 不代表可越過 budget無限重試。

## Legacy evidence migration

2026-07-28 的唯讀盤點：

- 84 個 activities；80 個有 place ID、84 個有座標；
- 71 個 travel edges（65 `source=api`、6 `source=manual`）；
- 71 個 edges全部缺 `fresh_until` 與明確 `evidence_state`；
- 81 筆 Places cache含座標、hours、reviews、photos或 summaries；
- 既有 flight cache缺完整 query context；
- 尚無 local `plan.json` 或 `.trip-planner-history`。

因此首次正式 migration不能照舊把所有 provider-derived bytes永久塞入 canonical：

- 65 個 legacy API edges因無 retrieved time、request identity與 freshness，
  一律視為 expired unknown，產生 `LEGACY_PROVIDER_EVIDENCE_REFRESH_REQUIRED`；
- 6 個 manual edges先是 unverified user assertion，需使用者明確分類／採納；
- provider coordinates與hours不匯入canonical；legacy Places content也不能只因
  尚未超過30天就整筆匯入，必須先符合field-level storage policy；
- 可依法長存的 place ID可保留，但仍需 identity refresh policy；
- 已過 `purge_at` 的 cache不可匯入 EvidenceStore；
- flight/hotel舊 cache不作 usable evidence。

真正刪除 legacy cache或清理可能含 restricted content的舊 history屬於破壞性
compliance cleanup：必須先產生 exact preview並由使用者審核，不在 migration
或 provider failure時偷偷執行。

## 實作 slices

### Phase 4.0 — Fact contract（offline）

- immutable FactKey / ProviderRequest / observation / provenance / result；
- deterministic secret-free fingerprint與exact request preimage；
- static ProviderPolicyRegistry與trusted promotion gate；
- explicit `MEMORY_ONLY | DISK_TTL | INDEFINITE_ID`；
- strict normalized value allowlist；
- pure LKG merge、conflict resolution與雙時鐘；
- redacted durable bindings與restricted-content sentinel tests；
- 不呼叫 provider、不修改 trips。

### Phase 4.1A — EvidenceStore（完成）

- per-trip lock、atomic current-state replace、revision；
- disk store只接受`DISK_TTL` / `INDEFINITE_ID`；
- store自己注入trusted wall clock；任何API/AI/replay提供的`purge_now`無效；
- 無 permanent history，purge-on-read/write與 explicit cleanup；
- normalized canonical codec、capacity、cache corruption、concurrency、真實
  process crash、write fault、rename lost-ack與outcome-unknown tests。

### Phase 4.1B — Composition + evidence revision wiring（完成）

- run-scoped ledger另行管理`MEMORY_ONLY`，與disk LKG compose成同一immutable
  snapshot；
- `compose_trip_state()`傳遞live sanitized attribution；缺live attribution時
  readiness / render fail closed並要求refetch；
- timed route只在exact departure / arrival clock相符時覆蓋，否則回退untimed
  evidence或fail closed；
- planner/scheduler/review/staging綁canonical state、composed state、
  evidence revision與單一evaluation clock；
- stage / commit前重新load與compose；post-commit drift回已套用但需外部更新，
  不沿用舊score，也不把已確認的canonical write誤稱unknown；
- safe `repr()` / `to_dict()`只保留redacted identity / digest；provider-derived
  route values、source URI與live attribution不進review、receipt或history。
- Phase 4 facts/store/composition專項116個、全套400個離線tests與三個
  real-trip validators全過；未呼叫真實provider或修改`trips/`。

### Phase 4.2 — Minimal Places identity（完成）

- `PlaceIdentityIntent`與trusted EvidenceSnapshot共同建立exact
  `resolve-place` request；field mask、`pageSize=5`、match scope與existing LKG
  basis全部進request fingerprint。
- candidate parser只接受bounded allowlist；country、locality、primary type與
  hard radius不可由review grant覆寫，結果排序與candidate-set digest不受provider
  回傳順序影響。
- 只有無截斷、唯一eligible、token-boundary exact-name，且不會改綁既有ID時
  可auto-promote；其他eligible結果需由host-clock authority簽發exact
  candidate-set grant。Expired review、倒退clock或evidence/store revision
  drift一律拒絕；assessment/review皆為evaluator-only factory values，caller
  不能自行組裝match結果後繞過review gate。
- 只有reviewed `provider_place_id`進`FactValue`與EvidenceStore；其他Places
  content、query、pagination token與座標不進disk、safe binding或receipt。
- ID refresh綁trusted existing observation/value/snapshot；changed ID回
  `PENDING_REVIEW`，且promotion在持鎖merge時再做basis CAS，舊refresh不能
  覆蓋已reviewed的新ID。Routes前置端點只接受fresh、unconflicted Google
  identity，factory-only endpoint再綁provider ID/value digest；safe binding
  只公開observation/value/endpoint digests。
- 本slice不含HTTP transport；19個identity專項、91個facts/identity tests與
  全套441個offline tests通過，三個real-trip validators通過，未呼叫provider
  或修改`trips/`。

### Phase 4.3 — Routes end-to-end（offline exit gate完成）

- injectable bytes transport；credential injection留在transport implementation，
  核心沒有內建HTTP client，也沒有在測試呼叫真實provider；
- fixed endpoint/minimal field mask、Place ID waypoint與
  exact arc/mode/departure/evidence/store fingerprint；
- bounded strict decoder、typed HTTP/transport/preflight failures、single-attempt
  default、opt-in bounded retry與共享actual-attempt budget；
- exact transit-to-driving fallback；driving observation保留
  `fallback_from_mode=transit`，transit fact保留typed
  `TRANSIT_UNAVAILABLE`problem；
- reloadable `EvidenceSession`只在記憶體merge Routes facts；durable或outcome
  drift、clock rollback與retention均fail closed，partial failure保留per-mode
  LKG；
- normalized duration/distance/static duration/fallback/warning metadata投影到
  runtime `TravelEstimate`，timeline以non-blocking typed issue揭露provider
  warnings與transit fallback；
- 真正E2E regression涵蓋adapter → batch budget → session snapshot →
  composition → timeline disclosure；
- offline gate通過後才可在明確授權下做真實API驗收；本checkpoint未呼叫
  provider、未render、未deploy，也未修改`trips/`。472個offline tests與
  三個real-trip validators通過。

### Phase 4.4 — Places profile / hours（offline exit gate完成）

- dedicated Place Details authorization gate拒絕generic promotion；profile、
  current hours與regular hours各自綁exact single-key scope、fixed minimal
  field mask、fresh Google Place endpoint與snapshot/store revision。
- full profile維持`MEMORY_ONLY`；本slice沒有新增coordinate disk fact，也不把
  purge deadline誤當落盤許可。Raw response、Place ID、provider text與dynamic
  attribution不進canonical、disk、receipt或safe views。
- injectable GET adapter支援typed HTTP/transport failure、single-attempt
  default、opt-in最多三次retry、共享actual-send budget、strict bounded decoder
  與batch/session drift rejection。
- current hours依request send date建立七日date-specific intervals，保留
  special-day coverage、overnight、24/7、explicit never-open與closed dates；
  absent periods、錯誤weekday/date、超界、overlap及DST ambiguity均fail closed。
- runtime composition以process-local `ActivityAvailability` sidecar帶入timeline；
  fresh unconflicted current hours才可成為hard constraint，而且活動完整duration
  必須落在manual/provider交集。Fixed-time活動不會被暗中搬動，slack使用實際
  交集終點。
- regular、stale、conflicted與missing hours只產生machine-readable
  `OPENING_HOURS_NEEDS_VERIFICATION`；多份等價current evidence可合併，多份
  不同current evidence則保留全部refs並fail closed。
- legacy `check_hours.py`只共用pure full-duration evaluator；舊
  `regularOpeningHours`永不輸出綠燈，locale-dependent
  `weekdayDescriptions`不作判定。Broad/full-mask cache builder預設拒絕，
  必須以`--legacy-full-mask-cache`明確承認quarantine才可執行。
- canned Busan/Hokkaido E2E涵蓋identity → adapter → batch budget →
  memory session → composition → timeline；514個offline tests、三個real-trip
  validators與Python compile通過。未呼叫真實provider、未render、未deploy，
  `trips/`保持byte-for-byte不變。

### Phase 4.5 — Natural-language intake / fixed transport / lodging optimization

- 自然語言是唯一 user-facing intake；typed schema 是 AI／trusted host 的內部安全
  結構，不得轉成要求使用者逐欄填寫的表單。只抽取使用者明確提供的日期、旅伴／預算、
  抵離交通、必訪活動與住宿偏好，未提供的欄位保持 unknown，只追問真正阻塞的資訊；
- 航班、渡輪、鐵路與其他交通只作 user-owned fixed 或 tentative
  arrival/departure boundary，可記 exact time 或 bounded window；不建立航班搜尋或
  provider promotion。legacy flight script/cache 後續 quarantine，不刪除既有資料；
- provider-neutral lodging candidate 接受飯店、民宿、Airbnb、地址、座標或概略區域；
  hotel search 僅是 candidate discovery，並與其他輸入同等處理；
- Phase 4.5A draft 只包含住宿類型、private location hint、`[check_in, check_out)`
  local dates與可選的 minor-unit price；住客／房間、check-in/out time window、取消
  條件與可訂性不進4.5A candidate。4.5B discovery request可綁住客／房間query
  scope，但不把搜尋結果冒充availability或booking；
- candidate 的 decision 與 evidence 是獨立維度。4.5A不提供任何decision或evidence
  promotion function；即使caller能直接import module，也只能產生candidate +
  unverified。使用者明確語意留在reported claim並回`awaiting_confirmation`，
  snapshot-bound evidence projection由4.5B sidecar處理，真正host-owned decision
  boundary留給4.5D。missing或概略位置一律揭露`needs_verification`，但可參與保守
  runtime比較；
- Phase 4.5A 所有 draft、binding 與 assessment 都是 process-local，不建立
  `FactKey`、`ProviderRequest`、`PlanPatch`，也不修改 canonical plan。私人位置、
  label、時間與價格不進safe view；公開binding ID使用process-secret keyed digest，
  不可跨process當durable identity。ID刻意在不同process改變；determinism只保證同一
  intake session內的結構結果、coverage與permutation invariance，不宣稱cross-process
  ID replay。單次assessment上限366晚、256個候選；
- Phase 4.5B已加入snapshot-bound住宿identity／route evidence與comparison-ready
  sidecar。只有`LOCATION_ID`經fresh identity resolution才可成為route endpoint；
  route observation另須原request receipt，且當時／目前endpoint observation與value
  digest相同。缺receipt、missing、stale、conflicted或endpoint drift不暴露route
  數值，只建立current-snapshot refresh request。Sidecar仍要求原candidate維持
  candidate + unverified + empty evidence refs；
- 4.5B hotel discovery normalizer無HTTP、cache或durable fact，只把strict bounded
  response轉成provider-discovered candidate + unverified。Success、partial、
  empty、provider error與invalid response有互斥shape；raw query、provider status
  payload、search/property token、位置與價格amount不進safe view；
- 4.5C以runtime-only joint recommendation把住宿錨點、固定交通、景點、Routes、
  current hours、冬季／換宿buffer放入同一detached `ScheduleProblem`。所有option
  必須共享exact policy/store/evidence/outcome revisions、evaluation／purge clocks、
  snapshot ID、composed canonical identity、scope、preferences與limits；solver
  candidate須在同一problem replay。Option state只能等於composed state加明示住宿
  anchors，不能夾帶其他day／location mutation；
- 每個可比較route slot需有相同day、direction、mode、anchor、departure context與
  minimum buffer，且duration只能來自fresh exact request receipt及未漂移endpoint。
  所有solver required arcs與`HARD_CURRENT` activity availability都須綁同一snapshot；
  stale、conflicted、missing、缺receipt、reported decision claim或snapshot drift
  一律不產生numeric winner。Price在有同scope可比較price evidence前只回
  `LODGING_PRICE_NOT_SCORED`；
- 4.5C輸出只表示`priority_review_option_id`，固定
  `supports_authoritative_use=false`，沒有`PlanPatch`、decision promotion、
  reservation writer或canonical mutation。Busan／Hokkaido canned acceptance已涵蓋
  固定抵達、booked活動、每日anchor、split stay、冬季跨城buffer與同分不選；
  4.5D才加入住宿專用canonical confirmation/apply gate；
- 全套須離線、deterministic且real-trip files byte-for-byte不變。Provider refresh
  可在caller明確授權後另行執行，但4.5C scorer本身沒有HTTP、cache或provider fan-out。

目前Phase 4.5已完成A/B/C runtime intake、evidence與joint recommendation：
63個Phase 4.5專項、全套589個offline tests、三個real-trip validators與Python
compile通過；29個trip files hash aggregate維持
`8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
未呼叫provider、未render、未deploy、未修改`trips/`；下一個slice是4.5D
canonical lodging confirmation / apply。

### Phase 4.6 — readiness與 compliance preview

- draft / review / travel-ready facts checklist；
- `recheck_required_at`；
- legacy evidence migration / cleanup preview；
- 釜山、北海道先用 canned facts，再由使用者授權真實 provider驗收。

## Phase 4 exit gate

- provider空回應、timeout或partial failure不覆寫合法 LKG；
- unknown policy/provider/field/region不能promotion；
- memory-only Google content永不出現在disk store、receipt或history；
- 需要live attribution的provider content在observation消失後不可render；
- stale / conflict / missing / retention-expired有不同 machine-readable結果；
- evidence refresh不改 plan revision，但會使舊 evidence-bound review stale；
- driving / transit fallback、beta warning與query horizon完整揭露；
- travel-ready列出出發前需要重驗的 exact facts與deadline；
- restricted sentinel不會出現在 `plan.json`、history、receipts或 durable logs；
- 全套測試離線、deterministic，不需 API key；
- real-trip files保持 byte-for-byte不變。

## 刻意不做

- 通用 provider plugin framework或dynamic discovery；
- knowledge graph、fact DSL、database、event sourcing、message queue；
- 永久 raw response/history；
- provider自動修改活動、constraint、fixed/booked或approval；
- AI-authored confidence／freshness／retention／verified；
- ML confidence模型；
- 自動訂票、訂房、付款；
- 尚未建立 retention-aware store前搬動或清理既有 `trips/`。
