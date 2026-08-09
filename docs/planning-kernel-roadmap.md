# Trip Planner 成熟化 Roadmap

狀態：進行中
啟動日期：2026-07-27
目前 checkpoint：Phase 3A 排程核心、Phase 3B safe staging、Phase 3C
solver decision gate、Phase 4.0 facts/policy foundation、Phase 4.1A
trusted-clock EvidenceStore 與 Phase 4.1B offline composition / evidence
revision wiring、Phase 4.2 minimal Places identity、Phase 4.3 Routes offline
exit gate、Phase 4.4 Places profile / hours offline exit gate與Phase 4.5A
natural-language runtime intake、Phase 4.5B snapshot-bound lodging evidence /
comparison candidate、Phase 4.5C joint lodging / itinerary recommendation與
Phase 4.5D host-signed canonical lodging confirmation / apply，以及Phase 4.6A
read-only readiness projection與Phase 4.6B legacy evidence / cleanup preview已完成；
Phase 5 現已有 legacy-only、read-only 的 `tripctl inspect` evidence review 與
`tripctl validate` deterministic timeline review 起始入口，以及Phase 5.2 static browser
timeline review。Phase 5.3另完成新旅行的private guided draft，Phase 5.4在其後提供一至
三張帶來源 badge、需要一次主觀審閱的private direction cards；兩者都只在process memory
中運作。Phase 5.5再把明確方向偏好安全交給下一輪private refinement，Phase 5.6則以
source-preserving carryover產生一張可再次審閱的整合方向，Phase 5.7再將明確接受／繼續調整
綁回exact整合方向，Phase 5.8則把accepted refinement的source indexes完整映射到相對day
candidate，Phase 5.9再綁定使用者對該candidate的明確接受／調整回覆，Phase 5.10則為每條
refined line建立typed、provider-neutral evidence requirement，Phase 5.11再建立bounded、可審閱的
provider capability scope，Phase 5.12再將scope的明確接受／縮小／取消綁回exact context；
Phase 5.13現已加上短效、host-attested、exact-context-bound的offline provider preflight，
Phase 5.14再將使用者對fresh preflight的接受／縮小／取消綁回exact context，Phase 5.15
則為已接受preflight衍生每項capability仍需要的private execution-target type，Phase 5.16再將
每個target與canonical private preimage／trusted evidence request contract及source lines做exact binding；
Phase 5.17–5.18完成private execution-authorization review與exact response，Phase 5.19在執行當下重核
pricing／policy／retention／billing／credential state，Phase 5.20–5.21完成request-materialization review與
exact response，Phase 5.22建立不可送出的typed private request contracts，Phase 5.23–5.24再完成最後一份
private send review與exact `accept_send`／`request_smaller`／`cancel` response，Phase 5.25再把accepted exact
contracts綁到allowlisted public transport profile、endpoint／method、field placement與credential slot，Phase 5.26
再建立該transport-bound bundle的exact private credential-binding review，Phase 5.27再擷取exact
`accept_credential_binding`／`request_smaller`／`cancel` response，Phase 5.28再以host提供的slot-level boolean
availability attestation建立exact live credential-binding review，Phase 5.29再擷取exact
`accept_live_credential_binding`／`request_smaller`／`cancel` response。這條chain仍沒有CLI、自然語言response
parser、credential value access／binding、HTTP request、provider call、schedule、render或mutation；accepted
response也仍不可執行或送出，只能前進到後續獨立ephemeral credential-value binding gate。
M0已把Phase 5.13–5.29凍結為`Provider Execution Safety Reference v1`，並把剩餘產品交付
收斂為Phase 5.30–5.33；runtime gate名稱不再各自占用roadmap phase編號。Phase 6.0
fail-closed public release boundary亦已完成；真實provider驗收仍只在明確授權範圍內進行，
完整canonical CLI/interface仍待後續有限切片。

## 產品目標

把 Trip Planner 從「由 prompt 協調多支腳本、生成一份可看的行程」升級成「AI 可以可靠操作的自動排行程產品」。

理想結果不是讓 AI 直接寫出更漂亮的 JSON，而是讓 AI 能：

1. 理解旅客的自然語言需求與主觀偏好。
2. 提出、比較及修復候選行程。
3. 透過 deterministic kernel 證明行程是否真的可執行。
4. 對未知、過期或互相衝突的資料誠實標示。
5. 在不破壞已確認項目的前提下，反覆迭代同一趟旅行。

第一批端到端驗收旅行為：

- 2026 年 10 月釜山。
- 2027 年 1 月北海道。

兩者只在底層能力達到對應 checkpoint 後才開始作為真實驗收，不以手動補資料掩蓋核心缺口。

## 核心架構決策

### ADR-001：AI 提案，kernel 裁決

AI 負責語意理解、候選探索、偏好取捨與修復建議；deterministic kernel 負責：

- schema 與 reference integrity；
- 時間線計算；
- hard constraints；
- evidence 與 freshness；
- revision、idempotency 與原子寫入；
- readiness gate。

AI 不直接覆寫 canonical trip JSON，也不能自行把 inference 升級成 verified fact。

### ADR-002：先建立正確性契約，再選 optimizer

排程器只能在相同的 fixture、invariant 與 score contract 下比較。Phase 0 不導入 OR-Tools，也不把現有距離排序器包裝成完整 scheduler。

後續以 benchmark 決定採用：

- bounded deterministic insertion / repair heuristic；或
- OR-Tools time-window solver。

正式產品只保留一個主要 solver；其他實作只作 benchmark，不形成兩套長期維護路徑。

### ADR-003：三個狀態維度分開

每個 activity / fact 不再用單一模糊狀態同時表示「是否決定」與「是否可信」：

- decision：candidate / selected / fixed / booked / cancelled；
- flexibility：movable / fixed_day / fixed_time；
- evidence：unverified / verified / stale / conflicted。

### ADR-004：四個最小 domain abstraction

目前只建立四個跨階段穩定抽象：

1. `TripState`：一次規劃所需的完整唯讀 aggregate。
2. `Constraint`：少量具名、typed 的 hard / soft 規則；不建立通用 DSL。
3. `PlanPatch`：下一階段加入的 semantic mutation 與 revision contract。
4. `CheckReport`：`feasible`、`infeasible`、`needs_verification` 與可修復 issue。

Timeline simulator 是 kernel 的純函式實作，不另外形成 framework。

### ADR-005：舊 JSON 先讀取相容，不立即搬家

Phase 0 用 read-only loader 把現有 `trip.json` 與 `itinerary.json` 正規化為 `TripState`：

- 不改寫 `trips/` 內的本機資料；
- 缺少 timezone、duration、route 等資料時產生 typed issue；
- synthetic ID 只供這個唯讀版本使用，不假裝是可長期依賴的 persisted ID；
- malformed JSON、核心 day / activity / constraint reference，或無法安全解釋的型別以 `LoadError` 拒絕；
- 可重建的 derived travel edge 若 stale 或 index 越界，則忽略該 edge、產生 typed issue，並讓結果維持 `needs_verification`。

正式 migration 留到 semantic writer 與 rollback 已存在之後。

### ADR-006：一份 canonical plan，一次 atomic replace

已遷移旅行以 `data/plan.json` 作為唯一 authoritative planning document：

- `state.trip` 與 `state.itinerary` lossless 保留 legacy unknown fields；
- persisted stable IDs 取代 array index identity；
- revision hash 包含 schema、trip ID、generation 與完整 semantic state；
- receipts 與 semantic state 在同一份 bytes 原子提交；
- generation 單調遞增，rollback 不會造成 ABA。

不拆成兩份互相依賴的 canonical JSON，也不引入 database 或 transaction journal。
Legacy `trip.json` / `itinerary.json` 在 migration 後只作 compatibility source；
canonical reader 與 writer 不依賴它們繼續存在。

### ADR-007：Receipt-first recovery 與 exact approval

安全寫入順序為：lock → strict load → receipt replay → revision CAS → pure mutation →
approval → kernel validation → exact history snapshot → atomic replace。

- 同 idempotency key、同 request 回 replay；
- 同 key、不同 request 拒絕；
- replace 前失敗保證 canonical bytes 不變；
- replace 後結果不明時，以同 key 重試取得真實 outcome；
- fixed、booked 與 migration-unclassified 的破壞性 net diff 必須由外部
  `ApprovalGrant` 精確綁定；
- rollback 是新的 revisioned transaction，且舊 patch receipt 會標成
  `rolled_back`，不能被 replay 復活。

完整 contract 見 [`safe-mutation-contract.md`](safe-mutation-contract.md)。

## 不可妥協的 invariants

- hard constraint violation 為零後，才比較 soft score。
- must-do activity 必須恰好出現一次；optional activity 最多一次。
- 一段完整行程包含 travel、buffer、wait、service duration 與 return-to-base。
- 「抵達時仍營業」不等於可行；完整活動必須在 time window 內結束。
- missing travel 或 duration 不視為 0，而是 `needs_verification`。
- fixed / booked activity 不得被 AI 靜默移動或刪除。
- unknown、stale 與 conflicted evidence 不得產生假綠燈。
- 同一 input 必須產生 deterministic report。
- rename / reorder 不應改變 persisted entity identity。
- 所有 mutation 最終都必須通過 revision check、dry-run、validation 與 atomic commit。

## 分階段計畫與 exit gate

### Phase 0 — Planning Kernel

範圍：

- immutable domain models；
- read-only legacy loader；
- timezone-aware timeline simulator；
- typed constraints；
- structured `CheckReport`；
- 至少 20 個聚焦於 invariants 的 adversarial offline fixtures；
- 接入 `scripts/check.sh`。

Exit gate：

- 所有測試離線、可重複且不需 API key；
- 單日、跨午夜、缺資料、window、return-to-base 與跨項 constraint 都有回歸測試；
- 現有 local trips 仍通過既有 validator；
- kernel 對舊資料的未知資訊回 `needs_verification`，不誤報 `feasible`；
- 不修改任何現有 trip data。

### Phase 1 — Safe Mutation

範圍：

- persisted stable IDs 與 schema version；
- `PlanPatch` semantic operations；
- `base_revision` optimistic locking；
- idempotency key；
- dry-run、atomic apply、rollback；
- legacy migration preview。

Exit gate：

- stale revision 必須拒絕；
- patch 重播不得重複新增；
- validation 失敗時 canonical state 完全不變；
- fixed / booked 項目的破壞性變更需要明確 human approval。

### Phase 2 — AI Repair Loop

範圍：

- planner snapshot；
- AI proposal envelope；
- issue owner 與 repair options；
- iteration、provider-call、change budget；
- repeated-state / oscillation detection；
- human checkpoint policy。

Exit gate：

- AI 漏排 must-do、引用未知 ID 或重複 activity 時不能 commit；
- 相同 state hash 不會無限修復；
- 每次變更都有可讀 diff 與理由；
- AI 不需知道 storage implementation。

### Phase 3 — Scheduling Quality

範圍：

- candidate generation；
- priority、energy、pace、daily capacity 與 slack；
- actual travel-time matrix；
- simple heuristic 與 OR-Tools bounded spike；
- lexicographic objective 與 score breakdown；
- partial replan，盡量保留既有行程。

Objective 優先序：

1. hard violations = 0；
2. must-do coverage；
3. preserve fixed / booked / previously accepted plan；
4. maximize preference priority；
5. minimize risk、tight slack 與不合理密度；
6. minimize travel time / cost。

Exit gate：

- 同一 benchmark 上選出一個 production solver；
- solver 失敗時回傳 typed infeasibility，不輸出殘缺「最佳解」；
- 釜山 fixture 可處理大眾運輸與每日住宿 anchor；
- 北海道 fixture 可處理冬季 buffer、跨城市與 fixed reservations。

目前進度（2026-07-30）：

- `schedule-problem/v3`、`schedule-candidate/v1`、完整 assignment replay、
  per-day summary、lexicographic scorer 與 typed failures 已落地；v3把
  runtime `ActivityAvailability` 納入problem identity與solver/replay，
  process-local v2 problem fail closed；
- production solver 已升為 `bounded-deterministic-best-first/v2`；可跨
  non-improving plateau，並加入 coverage-first in-place promotion、lazy
  `REQUIRES` closure 與 bounded selective-time subset repair，不宣稱全域
  optimal；
- 六個 offline fixture family、釜山／北海道 composite golden、permutation、
  replay、window/buffer/return/missing-evidence regressions已接入測試；
- canonical plan → scheduling problem → candidate → typed patch → TripStore
  preview/exact approval/atomic apply 已有 migrated-store integration；canonical
  trip ID 不再由 slug 猜測，travel invalidation 後會誠實降為待驗證；
- common projection 會在宣告 `SOLVED` 前執行 128-operation stageability gate，
  semantic digest 也保留 numeric/text scalar type，避免 replay collision；
- OR-Tools pre-adoption gate 已完成但未安裝 dependency；修正通用搜尋缺口後，
  固定 oracle/challenge corpus 沒有剩餘 failing target，只有未來固定 budget
  下出現 search exhaustion 或 exact objective regret 才重啟隔離 CP-SAT spike；
- schedule-specific staging seam 已具備 exact-type trusted replay/score、
  full canonical preview contract、per-review full-diff caps、human/store 雙重
  approval、deterministic re-preview、CAS 與 exact replay/rollback race recovery；
- repository ACK 不具權威性；commit 後必須由完整 semantic state digest、
  expected generation/revision、exact receipt request digest 與 persisted
  schedule 共同確認；raw after-write exception 以同一 patch 在整個 review
  合計最多兩次 write attempt，internal mismatch 不偽裝成 provider 工作；
- solver label 只作 version compatibility，不宣稱 provenance。Phase 3B
  adversarial exit gate 與 Phase 3C solver decision gate 均已通過。

### Phase 4 — Facts and Providers

範圍：

- static provider policy與exact ProviderRequest promotion gate；
- Places identity優先，再接 Routes、Places profile/hours 與 provider-neutral
  住宿候選定位；交通只接受使用者輸入的固定抵離邊界；
- normalized facts與kind-specific exact scope；
- provenance、retrieved-at、valid-until、purge-at與confidence；
- run-scoped memory evidence與policy-authorized disk LKG分流；
- partial failure 與 provider budget；
- research content 與執行指令的 trust boundary。

Exit gate：

- provider 空回應不覆寫 last-known-good；
- unknown policy/provider/field/region與out-of-scope result fail closed；
- memory-only provider content不進disk、canonical、history或receipt；
- stale / conflicting facts 能追溯來源；
- driving / transit fallback 清楚揭露；
- travel-ready profile 能指出需要出發前重新驗證的項目。

目前進度（2026-07-29）：

- Phase 4.0 已建立 immutable FactKey / FactValue / FactObservation、
  exact ProviderRequest、static ProviderPolicyRegistry、trusted promotion gate、
  pure LKG merge與immutable evidence snapshot；
- semantic freshness與compliance retention使用獨立時鐘；unknown
  provider/policy/region/field、out-of-scope result與過期content均fail closed；
- 內建Google non-EEA profile只讓place ID長期保存；Places profile/hours與
  Routes content維持run-scoped `MEMORY_ONLY`，不把TTL誤當storage permission；
- durable binding、`repr()`與safe `to_dict()`不含provider value、query value、
  response/source URI或dynamic attribution；live attribution缺失時後續
  readiness/render必須refetch；
- Phase 4.1A 已建立single-file current-state EvidenceStore、shared per-trip
  lock、trusted host clock、purge-on-read/merge/cleanup與atomic replace；
- strict normalized codec要求one revision對應one canonical bytes，並以共同
  4,096-record / 16 MiB boundary防止writer產生reader無法載入的cache；
- 真實process death orphan-temp cleanup、rename lost-ack outcome unknown、
  capacity/size fault、generation飽和不可逆purge與exact store-revision
  snapshot均已回歸；
- composition sidecar只在記憶體投影fresh/stale route evidence，保留canonical
  plan；exact timed departure / arrival route會依timeline clock選取；
- planner snapshot、repair dedupe、schedule problem、stage與commit均綁
  canonical/composed/evidence digests及單一evaluation clock；
- stage / commit前會重新compose；post-commit evidence drift保留已知canonical
  outcome並回`WAITING_EXTERNAL`，不沿用舊score；
- provider-derived values與live attribution不進safe repr、review serialization、
  receipt或history；
- Phase 4.2已建立snapshot-bound Places identity request、bounded candidate
  review、hard geographic/type gates、30分鐘promotion clock、ID-only refresh
  與fresh route-endpoint extraction；既有ID若改變必須人工複核，只有place ID
  可進EvidenceStore；
- Phase 4.3已建立fixed-endpoint/minimal-field-mask Google Routes adapter、
  injected transport、strict bounded decoder、typed HTTP/transport failure、
  actual-attempt budget與exact transit-to-driving fallback；
- reloadable EvidenceSession會將Routes維持run-scoped memory-only，durable
  revision、endpoint、retention與provider outcome drift均綁入review identity；
  per-mode partial failure保留LKG，fallback與provider warning一路投影到
  runtime TravelEstimate及timeline disclosure；
- canned-response E2E已涵蓋adapter → batch → session → composition →
  timeline。本checkpoint完成Routes offline exit gate但沒有呼叫真實provider；
- Phase 4.4已建立三個fixed-mask Place Details request、dedicated authorization
  gate、injected GET transport、strict bounded decoder、typed retry/budget與
  snapshot/session drift protection；current七日coverage綁實際send date，
  profile/hours content持續只存在memory；
- fresh unconflicted current hours透過runtime-only availability sidecar與manual
  windows取交集，完整活動duration才算可行；regular、stale、conflict與missing
  只產生needs-verification。Fixed-time不暗移，slack使用實際交集終點；
- legacy regular-hours checker永不輸出verified green，locale文字不參與判定；
  broad/full-mask cache builder預設quarantine。Busan／Hokkaido canned E2E、
  514個offline tests與三個real-trip validators已通過，trip files hash未變；
  Phase 4.4 live API驗收仍需明確授權；
- Phase 4.5A已建立process-local自然語言住宿／交通draft、candidate-only extraction、
  non-authoritative reported decision claim與逐夜coverage assessment。
  AI／provider不能提升decision或自行宣稱verified，raw私人位置／時間／價格不進
  safe view，且本slice沒有provider call、共同optimizer或canonical mutation；
- Phase 4.5B已建立exact snapshot-bound住宿identity／route evidence sidecar、
  provider-neutral comparison candidate與offline hotel discovery normalizer。
  Route必須帶產生observation的request receipt，並確認endpoint observation/value
  未漂移；stale、conflicted、missing與缺receipt不暴露數值，只產生deduped refresh
  requests。4.5A candidate仍維持candidate + unverified；
- Phase 4.5C已建立exact composed/snapshot-bound的detached lodging-anchor
  `ScheduleProblem`與lexicographic recommendation。固定抵離／booked活動、Routes、
  current hours、逐夜coverage、換宿與明示冬季buffer共同進入評估；不完整evidence、
  reported claim、同分或mixed snapshot都不產生winner，價格在可比較evidence完成前
  明確排除。Result只表示priority review，不含decision/canonical authority。

Phase 4.5已完成：

- **4.5D — canonical lodging confirmation/apply**：住宿專用safe review、
  externally signed host authority、typed `SetLodgingSelection`與既有TripStore
  CAS／approval／receipt／rollback protection整合。Generic approval、4.5C rank、
  reported claim與可import的builder都不能取代host verifier；decision升級後
  evidence仍為`unverified`。

### Phase 5 — Product Interface and Skill

範圍：

- 單一 `tripctl` CLI；
- consistent JSON envelope；
- 私有、無副作用的 future-trip guided draft，先讓 agent 以自然語言收集最小必要資訊；
- 私有、無副作用的 candidate direction cards，保留來源 badge 與一次主觀取捨；
- 私有、無副作用的 typed direction preference handoff，只進下一輪細化；
- 私有、無副作用的 source-preserving refinement，遺漏來源時不向使用者展示；
- 私有、無副作用的 relative-day itinerary candidate，只引用 accepted refinement source；
- 私有、無副作用的 exact itinerary-candidate response，只進 evidence-requirement planning seam；
- inspect / propose / score / validate / apply；
- 重寫 trip-planner skill，讓 agent 使用 kernel，而不是把 prompt 當規則引擎；
- 以統一 envelope 呈現 Phase 4.6 readiness profiles；
- 統一 `retryable`、`pending_review_retained` 與 `next_action` result envelope；
- 一次性 review/classify migrated baseline，明確採納後才解除
  `protected_activity_ids`；不得由 scheduler 偷清 migration protection。

Exit gate：

- 一個 AI agent 可只靠 typed tool contract 完成規劃與修復；
- 多 agent 只能提交 proposal / evidence，不直接並行寫 canonical state；
- 使用者在關鍵主觀取捨、付款、取消與公開發布前才需要確認。

#### M0 — Phase 5 rebaseline 與測試收斂

Phase 5.13–5.29的安全邊界不是廢棄工作；它們自M0起凍結為
`Provider Execution Safety Reference v1`。Exact fingerprints、五分鐘expiry、trusted-clock
rollback protection、replay／tamper／drift rejection、typed contracts、transport／credential
slots、redaction與既有regressions都保留。後續可修正明確bug，但不得再因拆出一個internal
runtime gate就增加roadmap phase。

剩餘Phase 5只有四個macro phases：

- **5.30 — composed pre-execution facade**：把既有Safety Reference組成單一typed facade；只在
  fresh exact consent下，經host-only non-serializable seam完成短效credential binding、allowlisted
  request construction與最後一次執行前重核。仍不呼叫provider、不寫trip、不授予可重播authority。
- **5.31 — bounded provider execution**：加入明確request／time／cost界線、typed outcomes、
  unknown-outcome retry policy與result quarantine；只有這個macro phase可跨越provider-call boundary。
- **5.32 — evidence-to-canonical workflow**：將隔離結果轉成source-backed evidence，接回
  composition、validate、schedule／repair與使用者審閱後的canonical apply；provider結果本身不能
  直接寫canonical state。
- **5.33 — unified product interface**：完成單一`tripctl`、consistent JSON envelope、
  inspect／propose／score／validate／apply、resume／retry語義、skill改寫，以及Busan／Hokkaido canned
  E2E與另行授權的bounded live smoke。通過原Phase 5 exit gate後即凍結Phase 5。

不存在默認的Phase 5.34。若5.33後仍有工作，只能是bug／maintenance、已定義的Phase 6 scope，或經
明確產品rebaseline後的新roadmap；不得把尚未命名的小型安全seam自動轉成新phase。

M0 testing contract：

- internal gate只跑Python compile、changed focused tests與直接predecessor compatibility；
- 每個5.30–5.33 macro phase、push／PR與release才跑完整offline suite、三個real-trip validators與
  `trips/*/data` aggregate hash；
- default exact-chain fixtures可共用frozen、process-local checkpoint；任何帶explicit arguments的
  alternate branch、drift、tamper、expiry或rollback fixture一律繞過cache並獨立建立；
- 937個既有tests與assertions全部保留，且至少保留一條由真實production contracts建立的
  Phase 5.3→5.29 full-chain E2E；
- 在目前開發環境以完整gate約15分鐘內為M0效能目標。若未達標，先處理測試重建熱點，不以刪除
  safety coverage或改寫production semantics換取速度。

M0明確不新增generic workflow DSL、database／event sourcing、microservice、queue或新test dependency。
這些都不是完成原Phase 5產品exit gate所需的最小工作。

### Phase 6 — Delivery, Privacy, and Operations

範圍：

- deterministic HTML / ICS renderer；
- private / public visibility；
- secret 與個資 redaction；
- render provenance；
- publish approval gate；
- basic observability 與 recovery instructions。

Exit gate：

- source revision 相同時輸出穩定；
- ICS UID 使用 stable activity ID；
- public build 不含私人預訂資訊；
- 未明確要求時不 deploy；
- 釜山與北海道完整走過 draft → review → travel-ready → private render。

### Phase 6.0 — fail-closed public release boundary

這是 Phase 6 的小型前置切片，不代表整個 Phase 6 已完成。

範圍：

- private legacy renderer 與公開發布來源分離；公開 builder 只讀 `public/`，不讀
  `trips/`、private HTML、ICS 或 provider cache；
- `public/trips/{slug}.json` 使用嚴格 allowlist schema，只能表達公開標題、日期標籤、
  城市與逐日摘要；
- `public/release.json` 明確 allowlist 每個 public JSON、每個 trip HTML 與首頁 HTML 的
  exact SHA-256；
- 無 manifest、digest drift、unsafe regular-file／directory／template source、或非
  allowlisted artifact 時，在任何 git 或網路動作前 fail closed；
- private render 保持原樣；不自動 migration、公開既有 trip、呼叫 provider 或變更
  `gh-pages`。

Exit gate：

- 相同公開來源、模板與 manifest 產出 byte-for-byte 相同的最小 artifact tree；
- public output 沒有地圖、ICS、訂位、待辦、行李、地址、座標、外部連結或 cache；
- 使用者只需審閱是否可公開與要求發布；不需手動驗證 private JSON 或 route；
- 第一次安全替換／下架現有 Pages 仍由使用者另行明確授權。

## 驗證策略

每個 phase 都使用相同四層驗證：

1. unit：純函式、typed model、edge cases。
2. adversarial fixture：刻意漏項、衝突、過期、跨午夜及 provider failure。
3. legacy compatibility：現有 local trips 只能讀取，且不發生資料變動。
4. end-to-end acceptance：釜山與北海道真實需求。

不只檢查最後 status，也檢查：

- issue 是否指向正確 entity / evidence；
- 非目標 activity 是否保持不變；
- 沒有資料遺失；
- 同一輸入是否 deterministic；
- 缺資料是否誠實標成待驗證。

## 成熟度量測

- false-feasible rate：已知不可行 fixture 被判可行的比例，目標 0。
- must-do coverage：目標 100% 或明確 infeasible。
- unexplained mutation：目標 0。
- deterministic replay：相同 state / engine version 結果 100% 相同。
- repair convergence：在固定 iteration budget 內完成或給出 typed blocker。
- unknown transparency：缺 evidence 的 fixture 100% 顯示 `needs_verification`。
- plan stability：局部資料變動時，非必要變更數應受 change budget 約束。

## 明確暫緩，避免過度工程

在真實驗收證明需要前，不做：

- 微服務或 message queue；
- event sourcing；
- 資料庫搬遷；
- 向量資料庫；
- 通用 workflow / constraint DSL；
- 固定數量的多 agent 編排；
- 多個 production solver；
- 自動付款、訂位或取消；
- 未經批准的公開部署。

## Checkpoint log

### 2026-07-27 — Phase 0 開工

- 完成現況、AI boundary、scheduler、invariant 與 YAGNI review。
- 確認先做 correctness contract，不直接換 optimizer。

### 2026-07-27 — Phase 0 完成

- 建立 immutable domain models、legacy loader、timezone-aware timeline simulator
  與 structured `CheckReport`。
- hard constraints、missing evidence、跨午夜、DST、return-to-base 與跨日 overlap
  均有 adversarial regression。
- kernel 接入 `scripts/check.sh`；現有三趟 local trips 保持可讀且未改寫。

### 2026-07-27 — Phase 1 完成

- 建立 `trip-planner.plan/v1` canonical codec、lossless migration preview 與
  persisted stable IDs。
- 建立 typed `PlanPatch`、net travel invalidation、revision CAS、idempotency、
  exact external approval 與 request budgets。
- 建立 `TripStore`：single-document atomic replace、fsync、history、fault recovery、
  guarded rollback 與 generation-based ABA protection。
- Canonical readers 優先讀 `plan.json`；舊 direct writers 在 provider call / write
  前 fail closed。
- 多 agent adversarial review 修正了 protected ordering bypass、partial failed draft、
  false derived invalidation、whitespace/control ID、broken symlink 與 alias-dependent
  migration digest。
- Exit evidence：107 個離線測試全過；三個 real-trip validators 全過；29 個 local
  trip files 在 Phase 1 前後 byte-for-byte 相同。

### 2026-07-27 — Phase 2 完成

- 建立 `planner-snapshot/v1`：固定 evaluation time、semantic state digest、
  stable structural issue identity、55 個 issue code 與 52 個 repair fix 的
  explicit registry，以及 model-facing budget。
- 建立 `repair-proposal/v1`：嚴格 JSON decoder、每個 operation 對應 issue、
  `option_key` 與理由；trusted binder 注入 revision、deterministic audit ID、
  AI provenance 與 exact idempotency。
- 建立單一 in-memory `RepairController` 與最小 `PlanRepository` protocol；
  支援 preview/commit、iteration/provider/change budget、strict progress、
  repeated attempt、oscillation、CAS stale、outcome-unknown exact retry 與
  readable diff/reason。
- 人工權限與模型輸入完全分離：authority-creating write、刪除、constraint、
  day boundary、option/field mismatch 與 large patch 需要 exact controller
  checkpoint；Phase 1 protected diff 仍另外需要 store `ApprovalGrant`。
- 多 agent adversarial review 修正 mixed unknown-option bypass、provider false
  cache hit、transient preview poisoning、terminal latch reset、CAS race state
  accounting、volatile/colliding issue identity、operation-field overreach 與
  post-commit read ambiguity。
- Exit evidence：153 個離線測試全過，其中 38 個為 Phase 2 contract/controller
  專項；三個 real-trip validators 全過；29 個 local trip files 仍
  byte-for-byte 相同；未呼叫 provider、未 render、未 deploy。

### 2026-07-28 — Phase 3A 排程核心完成

- 建立 `schedule-problem/v2`、`schedule-candidate/v1`、canonical-plan
  problem builder、complete assignment replay、fixed-point normalization、
  transparent lexicographic score 與 typed failure。
- bounded deterministic insertion/relocate/swap 共用 materializer、kernel、
  scorer 與 128-operation projection gate；heuristic 找不到不假稱 proven
  infeasible。
- 六個 offline fixture family、釜山／北海道 composite golden、permutation、
  missing-evidence、window/buffer/return、partial-replan 與 migrated-store
  integration 均已回歸。

### 2026-07-28 — Phase 3B safe staging 完成

- 建立 schedule-specific exact review/commit seam；不偽裝 Phase 2 repair issue，
  也不抽 durable workflow framework。
- trusted replay 拒絕 candidate-owned subclass並重算 score；preview 以 pure
  mutation engine 核對完整 draft、canonical identity/state/report。
- human checkpoint 與 protected-change store approval 分離；review 公開完整
  diff、protected changes、affected/invalidated days 與 exact scope。
- commit 不信任 repository ACK；以完整 expected state digest、expected
  generation/revision、exact receipt 與 canonical reload 確認。Exact winner、
  CAS、rollback、after-write exception、fake ACK、read failure與 replay-after-
  advance 都有 deterministic regression。
- write-attempt cap 綁定整個 pending review，跨呼叫最多兩次；只有可信
  persisted `needs_verification` 會進 `WAITING_EXTERNAL`，internal mismatch
  進 `OUTCOME_UNKNOWN`。
- Exit evidence：Phase 3 專項 89 個、staging 專項 28 個、全套 243 個離線
  tests 全過；三個 real-trip validators 全過；29 個 local trip files hash
  aggregate 維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
- 多 agent contract、race、product 與 final adversarial audit 均未留下
  P0/P1；未呼叫 provider、未 render、未 deploy、未加入 OR-Tools dependency。

### 2026-07-28 — Phase 3C solver decision gate 完成

- production solver 升為 `bounded-deterministic-best-first/v2`；保留 bounded
  non-improving frontier，可解兩步／三步 plateau 與 candidate replacement，
  但明確維持 `optimality=not_claimed`。
- hard coverage 使用 deterministic candidate ordering 與 in-place promotion；
  `REQUIRES` 以 lazy transitive closure 處理，避免 dependency placement 的
  Cartesian expansion。Explicit-time repair 只由 audited time issue 觸發，
  每個 layout 最多 64 個 deterministic subset variants。
- frozen、scope 外及 `fixed_day` 的 raw day/index 在 evaluation 前即保護；
  除明確的 patch operation cap 外，materialization、projection、frozen 與
  anchor invariant failure 一律 fail closed 為 typed `ENGINE_ERROR`。
- 完整兩活動／兩天 162-case oracle corpus（其中 150 個 feasible case）、
  directed roundtrip、兩步／三步 plateau、candidate packing、large
  `REQUIRES` closure、time-clear subsets、evaluation-limit precedence 與
  invariant injection 均已成為離線回歸。
- Busan／Hokkaido composite golden 分別以 238／35 個 stageable reachable
  layout 完成，served priority 維持 140／100；共同 kernel replay、score 與
  schedule key 無 regression。
- OR-Tools pre-adoption gate 的結論是暫不安裝：目前固定 challenge corpus
  沒有 search exhaustion 或 exact objective regret 可作改善目標。只有未來
  真實／adversarial fixture 在固定 budget 下出現可重現失敗，才重啟隔離
  CP-SAT spike。
- Exit evidence：solver-selection 16 個、Phase 3 專項 105 個、全套 259 個
  離線 tests 全過；三個 real-trip validators 全過；29 個 local trip files
  hash aggregate 仍為
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫 provider、未 render、未 deploy、未改動 trips、未加入 OR-Tools。

### 2026-07-28 — Phase 4.0 facts/policy foundation 完成

- 建立四種 V1 normalized fact schema，並預留 legacy flight/hotel offer kind 的
  fail-closed reservation；
  exact kind-specific scope、request fingerprint、source/provenance binding與
  bounded result shapes均有typed validation。
- 建立static policy catalog與promotion authority boundary；即使私下偽造
  `AuthorizedProviderResult` wrapper，merge仍會依目前registry重新授權。
- LKG refresh failure不覆寫、partial只更新成功slot、exact cache hit不造新
  evidence；fresh/stale/conflicted/missing與retention-expired分開處理。
- Google place ID是唯一內建`INDEFINITE_ID`；profile/hours/routes採
  `MEMORY_ONLY`。受限制values、signed URI、dynamic attribution與secret
  sentinel不進safe repr/durable binding。
- 多agent code、test與policy audit未留下P0/P1；Phase 4專項66個、全套325個
  離線tests全過，三個real-trip validators全過，29個trip files hash
  aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫provider、未render、未deploy、未修改`trips/`。

### 2026-07-28 — Phase 4.1A trusted-clock EvidenceStore 完成

- 建立per-trip single current-state cache，與canonical store共用exclusive lock；
  disk只接受目前policy允許的`DISK_TTL` / `INDEFINITE_ID`，`MEMORY_ONLY`在任何
  lock、temp或disk access前拒絕。
- trusted host clock只在每個locked operation取樣一次；load、merge與cleanup
  皆會durably purge retention-expired records。generation到signed 64-bit上限後
  promotion停止，但不可逆compliance deletion仍能完成。
- full private codec不使用redacted `to_dict()`；trip/policy/value/observation
  binding、record order、number/timestamp normalization與canonical bytes皆
  exact驗證。Writer與reader共用4,096 records及16 MiB上限。
- same-directory temp、file fsync、atomic replace與directory fsync具備
  pre-commit failure / outcome-unknown分流；rename lost acknowledgement可用
  expected revision收斂，真實process death留下的reserved temp會在下一次持
  lock時安全刪除並fsync。
- durable store revision與semantic ledger revision明確分離；
  `EvidenceStoreResult.snapshot()`會綁exact current store revision，且不讓caller
  注入過去的purge clock。Stale suppression不再誤標exact replay。
- 多agent adversarial review找出的capacity self-corruption、non-unique codec、
  orphan TTL temp、generation overflow、size exception、false replay、rename
  lost-ack與snapshot misbinding均已加入回歸。
- Exit evidence：facts專項68個、EvidenceStore專項28個、全套355個離線tests
  全過；三個real-trip validators全過；29個local trip files hash aggregate
  維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫provider、未render、未deploy、未修改`trips/`。

### 2026-07-28 — Phase 4.1B offline composition / revision wiring 完成

- `compose_trip_state()`建立不改canonical plan的runtime sidecar，綁
  canonical/composed state digest、policy/store/evidence revision與唯一
  evaluation clock；live attribution只留在process memory。
- fresh verified route可精確解除同edge/mode的loader warning；partial、
  stale、conflict、missing與其他load issue維持fail closed。
- `TravelEstimate`支援exact departure或arrival timestamp；timeline只選符合
  目前arc clock的timed route，否則使用合法untimed fallback。
- Repair與schedule review均pin同一snapshot；stage、commit與post-commit
  reload會偵測`EVIDENCE_REVISION_CHANGED`。Canonical write已確認但evidence
  失效時回`applied=true + WAITING_EXTERNAL`，不誤報`OUTCOME_UNKNOWN`。
- Evidence-bound runtime report/score保持可比較，但safe `repr()` /
  `to_dict()`只輸出redacted identity與digest，不洩漏route value或attribution。
- 早先長時間工作階段於06:21 UTC因unattended libc upgrade觸發systemd
  re-exec並重啟Tailscale而中斷；最後一次成功repo write是06:13:42 UTC。
  恢復檢查未發現半寫檔、pending lock、舊process或trip資料變動，從該
  checkpoint續作而非重跑或覆寫。
- Exit evidence：全套400個離線tests、三個real-trip validators與Python
  compile全過；29個local trip files hash aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫provider、未render、未deploy、未修改`trips/`。

### 2026-07-28 — Phase 4.1B post-review hardening

- `ScheduleStager`的runtime guard已與公開`EvidenceSource.load()`契約一致；
  真實`EvidenceStore`不再因source本身沒有`snapshot()`而被constructor拒絕。
- EvidenceStore只在bounded probe確認oversized cache屬於目前trip時重置；
  foreign或ownership unknown保留且不消耗reset nonce。Cache必須由目前uid擁有
  且mode為`0600`，並在decode、probe或reset前以實際opened fd metadata
  fail closed。
- Oversized判斷統一由單一`O_NOFOLLOW` fd實讀最多16 MiB + 1 byte決定，
  不再受`lstat()`與`open()`間的size replacement race影響。
- Legacy validator要求完整七檔；canonical validator要求`plan.json`與五個
  sidecar。明確overnight window可接受合法跨午夜順序，但一般或window外倒序
  仍拒絕。三個常用CLI的空輸入改為stable usage + exit 2。
- README、repo skill與已安裝Codex skill已標示legacy為完整user-facing流程、
  canonical為developer preview；舊writer不得繞過`TripStore` / typed
  `PlanPatch`直接修改`plan.json`，完整`tripctl`仍留在Phase 5。
- 兩輪低成本獨立review找出的P1均已加入回歸並複驗關閉。全套413個離線tests、
  三個real-trip validators與Python compile全過；29個trip files hash
  aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫provider、未render、未deploy、未修改`trips/`。
- 已知後續hardening：same-directory temp與`os.replace()`仍以pathname操作；
  對能在不遵守advisory lock下rename data directory的同uid actor，完整防護需
  dirfd / `openat`架構調整。此項保留為Phase 6 security/operations工作，不在
  本次bounded post-review修正中擴張。

### 2026-07-28 — Phase 4.2 minimal Places identity 完成

- 新增pure/offline Places identity boundary；trusted EvidenceSnapshot、
  exact policy、minimal field mask（含ephemeral `nextPageToken`以fail closed
  偵測pagination）、`pageSize=5`、match scope與既有LKG basis共同綁入request
  fingerprint，不呼叫HTTP或付費provider。
- Candidate不取第一筆；country、locality、primary type與hard radius先
  fail closed。排序與digest不受response順序影響；pagination截斷、非exact
  token-boundary name、多候選或existing-ID rebind都要求exact human grant。
- Review grant由host authority簽發，promotion使用host-stamped clock、30分鐘expiry與current
  store/evidence revision；過期或drift不可重播。Ephemeral候選payload附Google
  Maps attribution，display name、address、coordinates、types與page token
  不進safe binding、EvidenceStore、receipt或history。Assessment與review只能
  由strict evaluator mint；generic authorization不能替代review finalizer。
- ID-only refresh必須從trusted existing LKG建立；不同ID回`PENDING_REVIEW`，
  且持鎖merge會對basis observation/value做CAS，舊refresh不會覆蓋已reviewed
  rebind。Routes前置endpoint只接受fresh、unconflicted identity，並以
  factory-only constructor與observation/value/snapshot/endpoint digest綁定，
  不在safe view輸出raw provider ID。
- 低成本獨立review指出的arbitrary refresh、expired review、existing-ID
  silent rebind、generic/review-graph promotion bypass、name/locality
  normalization collision、endpoint偽造、policy migration full reset與
  two-stage write-result錯報均已修正並加入regression。Phase 4.2專項19個、
  facts/identity共91個、全套441個offline
  tests、三個real-trip validators與Python compile全過；29個trip files hash
  aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫provider、未render、未deploy、未修改`trips/`。

### 2026-07-28 — Phase 4.3 Routes offline exit gate 完成

- 新增fixed `computeRoutes` endpoint、minimal field mask與Place ID waypoint
  request；directed arc、mode、departure、endpoint observation/value、
  evidence snapshot及durable store revision共同綁入exact fingerprint。
- Transport由caller注入且自行持有credential；核心只處理bounded bytes。
  Decoder拒絕oversize、duplicate key、NaN、過深/過多節點、unknown field與
  多路線response，raw response、provider message、Place ID及自由文字warning
  不進safe output。
- 每個實際send都使用共享thread-safe attempt budget；預設不retry，opt-in
  retry最多三次。Transit horizon與endpoint freshness在送出前重驗，
  provider error映射為typed problem。
- Transit只有在empty/not-found/unsupported時可建立exact driving fallback；
  transit outcome保留typed`TRANSIT_UNAVAILABLE`，driving fact保留
  `fallback_from_mode=transit`，timeline明確揭露且不冒充transit。
- Reloadable EvidenceSession只合併memory-only result；durable revision變更
  會rebase並拒絕舊response，retention/clock rollback fail closed，global與
  multi-key provider problem維持原始identity並進`outcome_revision`。
- 低成本獨立review指出的durable-drift stale response、problem identity改寫、
  fallback缺typed outcome與E2E disclosure缺口均已補上回歸。最終test與trip
  hash證據為472個offline tests、三個real-trip validators，以及29個trip
  files aggregate
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
- 未呼叫真實provider、未render、未deploy、未修改`trips/`；live API驗收仍需
  明確授權，Phase 5 CLI/interface不在本slice。

### 2026-07-29 — Phase 4.4 Places profile / hours offline exit gate 完成

- 新增profile、current hours、regular hours三個dedicated request factory與
  authorization gate；固定minimal field masks，並把fresh Place endpoint、
  identity observation/value、snapshot/evidence/store revision、locale與target
  dates綁入exact fingerprint。Generic provider gate與forged endpoint均在HTTP
  前拒絕。
- Transport由caller注入並自行持有credential。GET path只在runtime含Place ID；
  safe binding只保留digest與field names。Strict decoder限制64 KiB、JSON
  depth/node、duplicate key、NaN與exact response fields，HTTP、transport、
  retry及actual-send budget都映射為typed outcome。
- Current coverage綁request send instant的place-local date，不受response跨
  午夜影響。Period date/day、special-day範圍、truncation、24/7、explicit
  never-open、overnight與DST都由machine-readable Point驗證；缺periods是
  unknown，不冒充closed。Regular schedule只投影typical evidence。
- `ActivityAvailability`是runtime-only sidecar；只有fresh、unconflicted
  current facts可形成hard constraint，且完整activity duration必須落在
  manual/provider交集。Fixed-time不暗移，provider交集終點會進slack；regular、
  stale、conflict與missing只產生needs-verification並保留全部evidence refs與
  attribution。
- Legacy `check_hours.py`不再把locale-dependent `weekdayDescriptions`當資料，
  regular-hours結果永不輸出綠燈；broad/full-mask cache builder預設拒絕，需
  `--legacy-full-mask-cache`明確承認quarantine。
- 獨立review找到的send/completion跨午夜、session digest不一致、等價current
  誤判衝突與provider/manual slack邊界均已修正並加入回歸。Busan／Hokkaido
  canned E2E與全套514個offline tests、三個real-trip validators、Python
  compile全過；29個trip files hash aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
- 未呼叫真實provider、未render、未deploy、未修改`trips/`；live API驗收仍需
  明確授權，下一個 slice 是 Phase 4.5 固定交通邊界、住宿候選與共同最佳化。

### 2026-07-30 — Phase 4.6A read-only readiness projection 完成

- 新增validated `CanonicalLodgingSummary`與factory-only `TripReadiness`，將exact
  canonical revision/state、composed state、kernel report、lodging summary、
  `EvidenceBinding`及完整`EvidenceSnapshot`綁成safe digest。Assessor會從
  canonical plan + snapshot重新
  compose並比對完整runtime view，再跑timeline kernel；caller不能刪除used evidence
  或live attribution後沿用穩定binding digest，也不能提供自稱的report。
- readiness固定為`draft`／`review`／`travel_ready`，輸出bounded counts、
  machine-readable problem codes、單一`next_action`與allowlisted繁中摘要。只有
  全部used evidence仍fresh、retained、selected、可供travel-ready且live
  attribution完整時，才會給最早的freshness或retention deadline作為
  `recheck_required_at`；需要recompose、refresh或resolve時不會沿用evidence
  deadline，但仍會保留尚待確認住宿review的到期時間。
- missing、stale、conflicted、retention-expired、selection/binding drift與
  regular-hours advisory有不同結果。Canonical住宿即使decision為`booked`，
  evidence仍是`unverified`並停在`review`；lodging intake現在保留exact
  `[stay_start, stay_end)`，不同區間的`not_required`不能跨trip沿用。Safe output
  只以不可逆`trip_ref`關聯行程，不回傳caller-controlled raw trip ID。
- Canonical或完整composed view漂移會要求`recompose_trip_state`，不會誤報成
  provider refresh或行程不可行；只有仍在有效期限內的waiting review會要求
  `confirm_lodging`，過期、拒絕或binding mismatch會要求
  `restage_lodging_review`。
- 修正canonical plan缺`slug`時，`plan_to_trip_state()`誤用隨機temporary directory
  名稱的既有不確定性；fallback現在固定使用canonical `trip_id`，同一plan可重播成
  相同state digest。
- 24個4.6A專項、全套624個offline tests、三個real-trip validators與Python compile
  全過；29個trip files hash aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。未呼叫
  provider、未render、未deploy、未修改`trips/`；下一個slice是4.6B legacy evidence
  migration / cleanup preview。

**遠端 closure checkpoint：** 2026-07-30 已將經過低成本架構、安全與產品複審的
Phase 4.6A implementation commit `4368a64`推送至
`origin/feat/tainan-2026-revival`。此點視為read-only readiness projection的封版
邊界；後續工作從4.6B另起，不在4.6A內默默擴張provider、confirmation或mutation
authority。

### 2026-08-02 — Phase 4.6B legacy evidence / cleanup preview 完成

- 新增`preview_legacy_evidence()`與單一只讀CLI
  `scripts/preview_legacy_evidence.py trips/{slug}`。固定manifest只讀取legacy
  `trip.json`／`itinerary.json`與已知cache的存在／bounded bytes；不呼叫provider、
  不建立`FactObservation`、不寫plan或EvidenceStore，也沒有`--migrate`、`--apply`或
  `--delete`選項。
- 舊`source=api` travel edge固定列為expired/unknown並回
  `LEGACY_PROVIDER_EVIDENCE_REFRESH_REQUIRED`；manual與未分類edge分別要求人工
  classification。place ID與coordinate只輸出aggregate count；不把舊Places profile、
  hours、coordinates、route value、flight/hotel cache或任何raw cache bytes提升成
  canonical／evidence。
- preview私下以legacy fixed-manifest exact bytes綁source revision；legacy source
  的任何內容、optional cache presence、symlink或nonregular/oversized drift都會使
  `verify_legacy_evidence_source()` fail closed。公開JSON只含relative artifact
  metadata、count與digest，不含place ID、地址、座標、價格、URL、token、raw trip ID
  或source hash。
- `cleanup_targets`固定為空、`imports=0`。現行legacy與canonical compatibility
  都仍需要`places_cache.json`；history/current EvidenceStore與未知cache亦只標示
  review／out-of-scope，沒有任何刪除seam。真正cleanup仍必須由使用者先審閱exact
  preview後另開受控操作。
- 三個local legacy trips的只讀acceptance重現65 API／6 manual travel edges、80
  place IDs、84 coordinate pairs與81 Places cache entries；trip
  bytes保持不變。新增11個4.6B專項測試，涵蓋分類、redaction、source drift、
  symlink／oversize、hostile JSON、presence-only out-of-scope artifacts、canonical
  compatibility、CLI與real-trip inventory；全套635個offline tests、Python compile與
  三個real-trip validators全過。
- 下一個需要使用者檢查的邊界是：審閱這份legacy evidence preview，決定是否授權
  真實provider exit gate或另行定義cleanup；不能藉此preview自動migration或清理。

### 2026-08-02 — Phase 5.0 `tripctl inspect` legacy read-only entry 完成

- 新增單一 `scripts/tripctl.py inspect trips/{slug}` 與可重用的
  `trip_planner.tripctl` contract。輸出固定、去敏感化 JSON envelope，包含
  `ok`、`status`、`retryable`、`pending_review_retained`、`next_action`與 aggregate
  legacy preview；成功只表示 `review_required` 或 `repair_required`，不會宣稱
  travel-ready、可用 route evidence、migration 或 cleanup authority。
- inspect 只重用既有 fixed-manifest legacy preview；source 可驗證時才在輸出前重驗，
  drift 回
  retryable typed error且不輸出 partial result。原本已不可驗證的 source 則保留
  aggregate repair problems，回 `repair_required` 而非假 stale。它不呼叫 provider、
  不開啟會 retention 寫入的 EvidenceStore、不 migration、不 render，也不改動 trip files。
- 任何 `plan.json`（包括 broken symlink）都優先回
  `CANONICAL_INSPECT_UNAVAILABLE`；preview 前後都重查，且不 fallback 到相鄰 legacy 資料。canonical
  readiness 需要 exact trusted snapshot／runtime composition，不能由 disk-only CLI
  偽造；目前沒有 real canonical trip 作為驗收目標。
- 新增八個離線 contract tests，涵蓋 redaction、determinism、source drift、unsafe
  source repair、JSON-only help/version/error envelope、canonical refusal與三個實際
  legacy trips 的 byte-for-byte 不變性。
  完整 compile／offline suite／real-trip validators均通過；未呼叫 provider、未修改
  `trips/`。
- `inspect` 仍只處理 evidence / cleanup review；後續的 canonical interface 仍必須等
  canonical trip 與 trusted runtime snapshot 的明確需求出現後，再另行設計 injected-runtime
  readiness envelope，不在 read-only legacy command 內偷偷加入 store read、provider、
  proposal 或 mutation authority。

### 2026-08-03 — Phase 5.1 `tripctl validate` legacy timeline review 完成

- 新增 `scripts/tripctl.py validate trips/{slug}` 與可重用的
  `validate_trip()` contract。它回答的是與七檔 renderer validator 不同的問題：legacy
  `trip.json` / `itinerary.json` 目前經 deterministic timeline kernel 後有哪些 aggregate
  blocker；固定 `now=None`，不以牆上時間改變結果。
- 只讀兩個 fixed source，先以 bounded、`O_NOFOLLOW`、regular-file snapshot copy 載入
  legacy loader，再跑 pure timeline；不讀取 cache、不呼叫 provider、不開
  EvidenceStore、不 render、不 migration，也不改動 trip files。公開 envelope 只含
  timeline status、day/activity/timeline count 與 `(code, severity, affected_count)`，不含
  title、path、時間、location/activity ID、route/metric 值、evidence ref、details 或原始
  exception。
- source 在 snapshot 前後與回傳前都重新比對；任何 drift 都回
  `STALE_LEGACY_TIMELINE_SOURCE`、retryable 且沒有 partial result。`plan.json`（包括
  broken symlink）在開始與輸出前都回 `CANONICAL_VALIDATE_UNAVAILABLE`，不 fallback 到
  相鄰 legacy bytes。source/strict-JSON/loader 問題則是安全的 `repair_required`。
- 所有成功結果仍是 `review_required`：`timeline_status=feasible` 也不是
  `travel_ready`，更不能取代 full seven-file `scripts/validate_trip.py`、evidence refresh
  或 canonical readiness。
- 新增八個 Phase 5.1 regression cases，覆蓋 deterministic/redacted output、source drift、
  malformed/unsafe source、redacted loader failure、canonical early/late refusal、JSON-only
  CLI、三個 real legacy trip acceptance 與 byte-for-byte tree preservation。完整 667 個
  offline tests、Python compile 與三個 real-trip validators 均通過；未呼叫 provider、
  未修改 `trips/`。
- 下一個大段落仍應是有明確 canonical trip / trusted runtime snapshot 需求後的 interface
  design；不在這個 legacy-only validate 內加入 score、proposal、apply 或任何 mutation。

### 2026-08-03 — Phase 5.2 static browser timeline review entry 完成

- renderer 直接重用 `validate_trip()`，再經一個只出現固定繁中標籤、tone 與 bounded
  count 的 one-way adapter 傳進 template；template 不接收 raw `tripctl` payload。canonical
  refusal或驗證失敗一律降級為固定「檢查結果尚不可用」，絕不 fallback legacy。
- 既有行程頁新增第五個「檢查」tab，`#review` 可直接開啟同一個唯讀面板。它顯示目前
  status、下一步及 allowlisted aggregate 類別，並明示離線結果不確認即時交通、營業、空位
  或訂位；沒有寫入按鈕、localStorage、provider call 或新增公開檔案範圍。
- 新增三個離線 regression cases，覆蓋 hostile input redaction、rejected validation 的固定
  unavailable state，以及石垣島真實資料在 temporary copy 的 renderer integration；實際
  Ishigaki render 的 review segment 不含 raw issue token、place ID、coordinates 或 evidence
  欄位，且三個 `trips/*/data` sources hash 保持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。
- 完整 670 個 offline tests、Python compile 與三個 real-trip validators 均通過。經使用者
  明確授權後，已只發布 root index、三個 generated trip HTML 與 ICS 到既有 GitHub Pages
  `gh-pages`；不提交或修改開發分支，未呼叫 provider。

### 2026-08-03 — Phase 5.3 private guided future-trip draft 完成

- 新增純、process-local 的 `TripBriefDraft`／`assess_guided_draft()` contract。host 從
  自然語言只抽取已明確說出的目的地、日期、偏好、必去項目、限制、交通與住宿候選；不新增
  brittle parser、CLI、session persistence、canonical trip、provider call、render 或 deploy。
- 初始只產生一個最小問題：先缺目的地才問目的地，目的地有了但缺 exact
  `[start, end)` 日期才問日期。模糊日期只保留private tentative hint，絕不猜成日期；
  住宿、預算、旅伴、步調與必去項目不阻塞初始候選提案。ready 只表示可開始 proposal，
  不是 confirmation、booking、route/hours evidence 或 `travel_ready`。
- 交通只接受既有 binder 產生的 `TransportBoundary`，住宿只接受 `LodgingCandidate`。
  使用者所說 selected／fixed／booked 仍只留在 `ReportedDecisionClaim`，bound result
  固定為 `candidate + unverified`；draft review 只投影住宿 status／safe action token與
  aggregate count。目的地、日期 hint、偏好、私人住宿位置／價格／URL、transport time、
  source ref、provider ID與candidate ID不會出現在 repr 或 safe transcript。
- `GuidedDraftReview` 以 private token 拒絕一般 direct construction 或
  `dataclasses.replace` 的意外破壞，保護 safe transcript 的使用方式；它不是授權或安全
  邊界，任何未來寫入仍必須走獨立 trusted host gate。新增12個專項回歸，涵蓋最小追問、模糊日期、
  candidate/unverified claim、住宿衝突、redaction、order invariance、duplicate rejection、
  no-I/O import boundary與forged review refusal。完整691個offline tests、Python compile
  與三個real-trip validators通過；`trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。

### 2026-08-03 — Phase 5.4 private guided direction-card review 完成

- 新增純、process-local 的 `GuidedDirectionCard`／`GuidedOutlineLine`／
  `assess_guided_proposal()` contract。ready brief 可有一至三張候選方向卡，但沒有選定、
  排名、apply、canonical trip、CLI、provider、render或deploy path。
- line 的 `outline_slot` 僅表示相對構想的順序／分組，與住宿夜數或calendar day無關；
  schema 不建模或驗證日期、時間、duration、route、place identity、price或availability。
  私有自由文字仍可能提到這些未驗證 claim，因此 host 顯示卡片時必須逐行標示
  `user_stated`／`tentative`／`ai_candidate`，固定顯示「候選方向尚未確認營業、交通、空位或價格。」
  並只問一次 A／B／混合／交給我調整的主觀問題。
- AI suggestion 不可宣稱 user must-do coverage；每張卡都只能由 user-stated line 對
  user-stated must-do 進行 declared coverage，缺項時保留 agent refinement，而不是把問題
  偷渡給使用者。所有 line 固定是 `candidate + unverified`；safe review / repr不含 card
  title、rationale、reference、日期或使用者原文。
- `REVIEW_REQUIRED` 明確要求使用者回應／決定，但其結果本身不是 authorization；review 的
  private token 只防一般誤用，不構成安全邊界，未來寫入仍需獨立 trusted host gate。
- 只有 `REVIEW_REQUIRED` 可向使用者顯示 raw cards 與固定 disclosure；
  `NEEDS_REFINEMENT` 必須先在私有層補齊 user-stated must-do 的 declared coverage，不能顯示
  不完整卡片或要求 A／B 取捨。完整701個offline tests、Python compile 與三個real-trip
  validators通過；`trips/` source working tree 維持未變。

### 2026-08-03 — Phase 5.5 private guided direction-preference handoff 完成

- 新增 `capture_guided_direction_preference()`／`GuidedDirectionPreference`／
  `assess_guided_direction_preference()`：host 只在 current `REVIEW_REQUIRED` cards 上擷取
  明確的 `prefer_one`、`mix` 或 `request_refinement` 回覆。它不是 brittle NLP parser；
  含糊回覆保留既有的一題主觀問題。
- handoff 每次重新評估 brief 與 cards；未完成目的地／日期 blocker、缺 user-stated
  must-do coverage、unknown card ref 或不合法數量都 fail closed。capture 私有地綁定 exact
  brief 與 canonical card contents，之後的 content／brief drift 都拒絕舊回覆；此 binding
  只防 stale／mismatch，不構成 authorization。card refs 只在當前 process-local card set
  有效；cards 重新生成後必須重新展示與重新擷取偏好。
- safe transcript／repr只含 preference mode、aggregate card count與下一步，不含 card ref、
  title、rationale、使用者原文、日期或位置。偏好及原方向卡持續是
  `candidate + unverified`，不會建立 trip、選定景點、呼叫 provider、render、CLI、
  `PlanPatch`或任何 canonical write。
- 完整711個offline tests、Python compile與三個real-trip validators均通過；
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。
  未呼叫provider、未render、未deploy、未修改`trips/`。

### 2026-08-04 — Phase 5.6 source-preserving private direction refinement 完成

- 新增純process-local的`GuidedRefinementCandidate`、`GuidedSourceLineRef`與
  `assess_guided_refinement()`。Assessor每次先重驗exact brief、cards與current typed
  preference；stale context、unknown／out-of-range line ref、duplicate ref或output card-ref
  collision一律fail closed。
- `prefer_one`必須保留所選卡全部line；`mix`必須保留每張所選卡至少一條AI candidate
  direction line（該卡沒有AI line時至少一條原line），並保留所選卡全部user-stated line；
  `request_refinement`也必須保留目前卡片組全部user-stated line。Relative slot可重排，
  但宣告保留的exact line內容必須實際帶入新卡；未選來源不可混入單選／混合結果。
- 遺漏來源、未實際carry、must-do coverage不足只產生allowlisted aggregate problem codes與
  `needs_refinement`，raw candidate不可展示。只有無problem的`review_required`才提供固定揭露
  與單一整合方向審閱問題；safe transcript／repr不含card ref、line index、標題、理由、
  日期、位置或使用者原文。
- 整合方向固定仍是`candidate + unverified`且`supports_authoritative_use=false`；沒有CLI、
  parser、provider、schedule、trip creation、render、deploy、confirmation或apply path。
  新增14個專項回歸；完整725個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個切片才會處理使用者對整合方向的明確「接受／繼續調整」回覆；在新的exact binding與
  review contract完成前，不把`review_required`當成confirmation或建立行程的授權。

### 2026-08-04 — Phase 5.7 exact refined-direction response handoff 完成

- 新增`GuidedRefinementResponse`、`capture_guided_refinement_response()`與
  `assess_guided_refinement_response()`。Host只在current refined direction確實是
  `review_required`且已清楚理解回覆時，擷取exact `accept_direction`或
  `request_adjustment` enum；沒有NLP parser或free-text payload，含糊回覆維持原問題。
- response fingerprint綁exact brief、canonical card contents、typed preference、refinement
  candidate與derived review；capture與assess都重新評估完整context。card排序不影響binding，
  但任何需求、內容、偏好或candidate drift都拒絕舊回覆。此guard只防stale／mismatch，不是授權。
- `accept_direction`只回`prepare_private_itinerary_candidate`這個future private-only seam；
  `request_adjustment`回到`refine_private_direction`。若回覆帶新需求，host須先把明確事實重新抽取
  到current private `TripBriefDraft`，再重新細化；response schema不保存自由文字。
- handoff固定是`candidate + unverified`且`supports_authoritative_use=false`；沒有trip creation、
  schedule、provider、filesystem/store write、render、deploy、confirmation或apply path。新增7個
  專項回歸；完整732個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。

### 2026-08-04 — Phase 5.8 source-only relative-day itinerary candidate 完成

- 新增純process-local的`GuidedItineraryDay`、`GuidedItineraryCandidate`與
  `assess_guided_itinerary_candidate()`。Assessor先重驗exact Phase 5.7 context，只有目前
  `accept_direction`可進入；`request_adjustment`、stale brief／cards／preference／refinement／
  response一律fail closed。
- candidate只保存refined direction line indexes與opaque transport boundary IDs，不接受title、
  rationale、日期、時間、duration、route或其他自由文字。`relative_day_index`固定為
  `0..overnight_count`，上限由exact current brief重新計算；它只表示抵達日至離開日的相對bucket，
  不等於calendar date或可執行schedule。
- 每條refined line必須恰好放置一次；unknown、missing、duplicate line或超出trip span的day只回
  allowlisted problem codes。current brief內的transport boundary也必須exact multiset carryover；
  unknown、missing或duplicate boundary都保留private `needs_refinement`，不可顯示candidate。
- 只有`review_required`可顯示private candidate及固定unverified／non-executable disclosure；safe
  transcript與repr只含aggregate bucket／line／boundary counts。Raw source indexes與boundary IDs
  不得serialize、log或persist。結果固定為`candidate + unverified`且
  `supports_authoritative_use=false`，沒有provider、scheduler、trip creation、filesystem/store
  write、render、deploy、confirmation或apply path。
- 新增11個專項回歸；完整743個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片才處理使用者對目前private itinerary candidate的明確「接受／繼續調整」回覆；
  在新的exact response binding完成前，`review_required`不是provider、schedule、trip creation或
  canonical mutation授權。

### 2026-08-04 — Phase 5.9 exact private itinerary-response handoff 完成

- 新增`GuidedItineraryResponseKind`、`GuidedItineraryResponse`、
  `capture_guided_itinerary_response()`與`assess_guided_itinerary_response()`。Host只在目前
  Phase 5.8 candidate仍為`review_required`且已清楚理解回覆時，擷取exact
  `accept_itinerary_candidate`或`request_itinerary_adjustment`；沒有NLP parser或free-text payload。
- capture與assess都重新執行Phase 5.8 assessor；private SHA-256 fingerprint綁exact brief、
  canonical card contents、preference、refinement、Phase 5.7 response、itinerary candidate及
  derived review。card order可canonicalize，其他任一context drift都拒絕舊回覆。
- `accept_itinerary_candidate`只表示接受目前private、non-executable candidate，下一步僅為
  `prepare_private_evidence_requirements`規劃標籤；本切片不建立evidence/provider request、
  不呼叫provider，也不提供未來provider call authority。`request_itinerary_adjustment`只回到
  private candidate refinement；新事實另外回抽取至private brief並重建exact context。
- safe repr／transcript只含kind與aggregate day／line／boundary counts；不含bucket／line indexes、
  boundary ID、日期、位置、route value或使用者原文。handoff固定仍是`candidate + unverified`、
  non-executable且`supports_authoritative_use=false`；沒有scheduler、trip creation、filesystem/store
  write、render、deploy、selection、booking、confirmation或apply path。
- 新增8個專項回歸；完整751個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片才設計純offline、typed的private evidence-requirement plan；在該contract完成前，
  不從自由文字猜provider payload，也不建立request、呼叫API或取得任何外部動作授權。

### 2026-08-04 — Phase 5.10 private evidence-requirement plan 完成

- 新增`GuidedLineEvidenceRequirement`、`GuidedEvidenceRequirementPlan`與
  `assess_guided_evidence_requirement_plan()`。Assessor先重新執行exact Phase 5.9 accepted
  response contract；adjustment response或任一upstream context drift都拒絕。
- 每條refined source line必須恰好一個declaration。`requires_verification`至少列一個
  provider-neutral `place_identity`／`current_opening_hours`／`route`／`lodging`／`availability`／
  `price` topic；`no_external_evidence_identified`不得帶topic，且仍固定是unverified，不代表
  已驗證、不需驗證或可執行。unknown／missing／duplicate line只回redacted repair code。
- Raw plan只有line index、typed disposition與topic，沒有line text、bucket、boundary ID、日期、
  provider ID、query、payload、URL或free text。safe transcript只含aggregate declaration/topic
  counts，結果固定為`candidate + unverified`且`supports_authoritative_use=false`。
- 完整plan只把下一步標成`prepare_private_provider_scope_review`；這只是下一個private planning
  seam，不建立provider request、不呼叫API、不授權scope，也沒有CLI、scheduler、trip creation、
  filesystem/store write、render、deploy、confirmation或apply path。
- 新增10個專項回歸；完整761個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.11 bounded private provider-scope review：先以typed、可審閱的範圍、
  cost與資料使用邊界取得使用者明確決定；在該contract及回覆binding完成前仍不建立request、
  不呼叫provider。

### 2026-08-04 — Phase 5.11 bounded private provider-scope review 完成

- 新增`GuidedProviderScopeItem`、`GuidedProviderScopeProposal`與
  `assess_guided_provider_scope()`。Assessor先重驗exact Phase 5.10 ready plan；invalid upstream
  plan、adjustment response或任一更早context drift都拒絕。
- 每個nonzero evidence topic必須exact once映射到contract固定的Google Places identity／current
  hours、Google Routes或SerpAPI Google Hotels capability。Item只含topic、capability與正整數
  request cap；missing／extra／duplicate topic、mapping mismatch或aggregate cap超過32都只回
  redacted `needs_refinement`。
- Safe review顯示typed scope item與derived data-category disclosure，明示可能計費、current pricing
  尚未核對、request cap不是金額上限，且真正呼叫前仍須provider policy、terms／retention、
  host-managed credential及exact request gate。它不含line index、private value、provider resource
  ID、query、payload、URL或credential。
- 有效非空scope才為`review_required`並詢問接受、縮小或取消；這個主觀回覆不是provider
  authorization。零topic只能搭配空scope，回`no_provider_scope_required`而不追問，但仍固定為
  `candidate + unverified`且`supports_authoritative_use=false`，不跳成travel-ready或可執行。
- 本切片沒有response parser/capture、provider request/call、credential access、pricing lookup、CLI、
  scheduler、trip creation、filesystem/store write、render、deploy、confirmation或apply path。
- 新增11個專項回歸；完整772個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.12 exact provider-scope response handoff：只綁定使用者明確的接受、
  縮小或取消回覆；即使接受也只前往另一個private policy/request planning seam，不建立request、
  不讀credential、不呼叫provider或授權外部動作。

### 2026-08-04 — Phase 5.12 exact provider-scope response handoff 完成

- 新增獨立`guided_provider_scope_response.py`、`GuidedProviderScopeResponseKind`、
  `capture_guided_provider_scope_response()`與`assess_guided_provider_scope_response()`。只有exact
  Phase 5.11 `review_required` scope可擷取`accept_provider_scope`、
  `request_smaller_provider_scope`或`cancel_external_lookup`；沒有parser或free-text payload。
- Capture與assess都重跑Phase 5.11；SHA-256 private fingerprint綁exact brief、canonical cards、
  preference、refinement及response、itinerary及response、evidence plan、scope proposal與fresh
  derived review。除card ordering外任何upstream、plan或scope drift都拒絕舊response。
- Accept只回`prepare_private_provider_preflight_review` label，且safe output固定
  `provider_scope_authorized=false`；Reduce只回scope refinement，不自動改topic/cap；Cancel只取消
  目前lookup path並保留原evidence requirements，不把candidate升級為verified或travel-ready。
- Safe transcript只含response kind與aggregate topic／capability／request-cap counts；沒有proposal
  items、line index、private text、日期、resource ID、query、payload或credential。所有分支固定為
  `candidate + unverified`且`supports_authoritative_use=false`。
- 本切片沒有pricing／policy check、credential access、provider request/call、CLI、scheduler、trip
  creation、filesystem/store write、render、deploy、confirmation或apply path。
- 新增8個專項回歸；完整780個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- Phase 5.3–5.12純offline guided-scope section至此收束。下一個external provider preflight必須在
  執行當下重新核對current pricing、provider policy／terms／retention、credential/session與exact
  request scope，並取得使用者明確參與；任何本段label或response都不能替代該邊界。

### 2026-08-07 — Phase 5.13 exact offline provider-preflight attestation 完成

- 新增`guided_provider_preflight.py`、`GuidedProviderPreflightItem`、token-gated
  `GuidedProviderPreflight`與`assess_guided_provider_preflight()`。只有exact Phase 5.12
  `accept_provider_scope`可由trusted host準備preflight；SHA-256 private fingerprint綁完整guided
  context、accepted scope、derived review、全部attestation items及時間範圍，除card ordering外任何
  drift都拒絕舊bundle。
- 每項attestation只含topic、capability、版本化request／pricing／policy／retention profile、
  opaque billing-region分類、boolean-equivalent credential status與request cap。SerpApi另有bounded
  remaining-plan-credit（只保留到contract的32-call上限）、auto-renewal與ZeroTrace entitlement
  status；沒有query、payload、resource ID、credential、policy text、
  帳單地址或private itinerary value。
- `checked_at`／`expires_at`必須是UTC且有效期最長24小時，assessor只接受當下
  `evaluation_at`內的fresh attestation。Missing／extra／duplicate item、capability／profile／retention
  mismatch、超出accepted cap或同provider credential status矛盾回`needs_refinement`；credential不可用、
  billing region未確認、stale attestation、SerpApi plan state未確認／credit不足／auto-renewal
  啟用則fail closed為`blocked`。
- Google目前只接受non-EEA policy profile；不從旅行地點推測billing account region。
  Request profile固定為Text Search Pro、Place Details Enterprise及無advanced traffic options的
  Compute Routes Essentials。Safe output只顯示以first paid tier試算的規劃值、published monthly
  free cap與SerpApi plan-credit cap；明示monthly remaining usage未查、試算不是hard currency cap。
- 2026-08-07 host重查官方快照；Google profile日期依pricing page標示的
  2026-07-31 last update。Google [pricing list](https://developers.google.com/maps/billing-and-pricing/pricing)
  列Text Search Pro為5,000 monthly free cap、首個付費tier USD 32/1,000；Place Details
  Enterprise為1,000與USD 20/1,000；Compute Routes Essentials為10,000與USD 5/1,000。
  [Places fields/SKUs](https://developers.google.com/maps/documentation/places/web-service/data-fields)
  將identity fields對應Text Search Pro、`currentOpeningHours`對應Place Details Enterprise；
  [Routes billing](https://developers.google.com/maps/documentation/routes/usage-and-billing)說明未使用
  `TRAFFIC_AWARE`等advanced feature時為Essentials。
- Google [Places policy](https://developers.google.com/maps/documentation/places/web-service/policies)、
  [place ID policy](https://developers.google.com/maps/documentation/places/web-service/place-id)、
  [general terms](https://cloud.google.com/maps-platform/terms)及
  [service-specific terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)是本次policy／
  retention profile來源；本切片只宣告process-local result handling，不新增任何disk cache。
  SerpApi profile來源為[pricing](https://serpapi.com/pricing)、
  [Google Hotels API](https://serpapi.com/google-hotels-api)、[terms](https://serpapi.com/legal)與
  [ZeroTrace](https://serpapi.com/zero-trace-mode)；每個successful non-cached search以一個plan
  credit規劃，ZeroTrace只能在Enterprise plan中聲明。
- Profiles只是caller／host attestation，不是provider verification。結構與blocker都通過時也只回
  `review_private_provider_execution_authorization`；`provider_calls_permitted=false`、
  `explicit_execution_authorization_required=true`，仍是`candidate + unverified`，不建立request、
  不讀credential、不呼叫provider、不寫trip、不render或deploy。
- 新增10個專項回歸；完整790個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 同日secret-safe的live readiness檢查只得到Bitwarden locked、Google Maps／SerpApi key在當前
  process不可用，且Google billing region尚未由host證實；因此provider calls為0，也沒有
  建立fixture、讀取secret或改動trip data。
- 下一個最小切片是Phase 5.14 exact provider-preflight response handoff：只擷取使用者
  對當輪ready preflight的明確接受／縮小／取消；它仍不是provider call，真正request
  materialization與execution-time recheck必須留在之後的獨立boundary。

### 2026-08-07 — Phase 5.14 exact provider-preflight response handoff 完成

- 新增獨立`guided_provider_preflight_response.py`、
  `GuidedProviderPreflightResponseKind`、`capture_guided_provider_preflight_response()`與
  `assess_guided_provider_preflight_response()`。只有在trusted UTC下仍為fresh
  `review_required`的exact Phase 5.13 preflight可擷取`accept_provider_preflight`、
  `request_smaller_provider_preflight`或`cancel_external_execution`；沒有parser、free-text payload或
  任何request material。
- Response的private SHA-256 fingerprint綁response kind、完整guided context、accepted scope與response、完整
  preflight items／profiles／caps／checked-at／expiry、fresh derived review與capture time。Card ordering
  會canonicalize，其餘brief、card content、preference、itinerary、evidence、scope、profile、cap、
  preflight或time drift都fail closed。
- Assessment先以response內部的private capture time重建原review與fingerprint，再用caller提供的
  當前trusted UTC重驗preflight。Current evaluation早於capture、到期邊界或任何blocker都
  拒絕，因此舊ready response不能在24小時attestation過期後replay。
- Accept只回`ready_for_private_provider_execution_authorization` 與
  `prepare_private_provider_execution_authorization`；它只允許準備下一個exact review，不是
  authorization。Reduce只回`refine_private_provider_preflight`，不自動刪topic、改profile或降cap；
  Cancel只關閉當前external execution path並回`continue_private_evidence_review`，保留原
  evidence requirements。
- Safe handoff只有response kind、data categories、capability／scope／preflight counts、request cap、
  list-rate estimate coverage與plan-credit cap；不顯示capture time、private context、query、payload、provider ID、
  credential、billing address或policy text。所有分支固定
  `explicit_execution_authorization_required_before_any_call=true`、
  `provider_scope_authorized=false`、`provider_requests_created=false`、`provider_calls_permitted=false`、
  `candidate + unverified`與`supports_authoritative_use=false`。
- Phase 5.13 review另新增typed read-only cost properties，供response原樣繼承Google first-paid-tier
  planning estimate、SerpApi plan-credit cap與「所有provider是否都有currency list-rate estimate」；仍明示
  monthly free usage未查且試算不是hard cap。
- 新增9個專項回歸；完整799個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 本切片沒有environment／vault access、provider request／call、CLI、scheduler、trip creation、
  filesystem／store write、render、deploy、confirmation或canonical apply path。
- 下一個最小切片是Phase 5.15 private provider-execution target requirement plan：先分開
  可從目前private context綁定的target與必須等待trusted provider-result evidence的target，不從自由
  文字猜query、Place ID或route endpoints。

### 2026-08-07 — Phase 5.15 provider-execution target requirement plan 完成

- 新增`guided_provider_execution_targets.py`、`GuidedProviderExecutionTargetItem`、token-gated
  `GuidedProviderExecutionTargets`與`assess_guided_provider_execution_targets()`。只有fresh Phase 5.14
  `accept_provider_preflight`可準備plan；reduce／cancel、stale／blocked preflight、clock rollback或
  任何upstream drift都fail closed。
- Target mapping完全由accepted capability決定：Google Places identity需
  `private_place_identity_intent`；current hours需`trusted_place_endpoint`且依賴existing／future
  `trusted_place_identity_evidence`；Routes需`trusted_route_endpoint_pair`且依賴
  `trusted_route_endpoint_evidence`；SerpApi Google Hotels需`private_serpapi_hotel_search_intent`。
  Topic／capability／request-profile／target-kind／dependency是contract-fixed exact mapping，不可由caller修改。
- Plan不接受任何target value或digest。Private fingerprint綁完整guided context、scope／response、
  preflight／response及derived review、deterministic target items與private preparation time。Assessment先重建
  preparation-time review，再用當前trusted UTC重驗；preflight到期後不能replay舊plan。
- 因本切片尚未做exact target binding，所有item都回deferred、
  `eligible_for_execution_authorization_item_count=0`、`all_execution_targets_bound=false`與
  `partial_execution_authorization_permitted=false`。Existing或future provider evidence只能滿足對應typed
  dependency，`provider_result_dependency_auto_authorizes_followup=false`，不能授權另一provider或call。
- Safe output只顯示topic／capability／request profile／target kind／dependency／cap、aggregate
  counts、data categories、Google list-rate planning estimate與SerpApi credit cap。它不讀或顯示line
  text／index、query、payload、Place ID、provider resource ID、地址／座標、private date／time、credential、
  billing data或caller-supplied digest。
- 結果固定`needs_private_execution_targets`與`prepare_private_provider_execution_targets`；
  `provider_scope_authorized=false`、`provider_requests_created=false`、`provider_calls_permitted=false`、
  `candidate + unverified`與`supports_authoritative_use=false`。沒有target binding、HTTP request、credential／
  env／vault access、provider call、store／trip write、scheduler、render、deploy或canonical apply path。
- 新增8個專項回歸；完整807個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.16 exact private execution-target binding：只接受canonical
  private preimage或現有trusted evidence／request contracts，由contract自行計算digest並綁policy／snapshot／
  evidence revision；不接受caller單獨提供的digest。直到後續exact authorization response與
  execution-time gate完成前都不建立HTTP request或呼叫provider。

### 2026-08-07 — Phase 5.16 exact private execution-target binding 完成

- 新增獨立`guided_provider_execution_target_bindings.py`、
  `GuidedProviderExecutionTargetPreimage`、token-gated
  `GuidedProviderExecutionTargetBindings`與
  `assess_guided_provider_execution_target_bindings()`。只有fresh、exact Phase 5.15 plan可進入；
  preflight expiry、clock rollback或任何guided／scope／preflight／target-plan drift都fail closed。
- 每個preimage都必須標出它服務的private source-line indexes；binder依Phase 5.10 plan驗證每個
  required topic的line coverage恰好一次，並限制實際target數不超過該topic已接受的request cap。
  缺漏、重複、額外topic、錯誤target type或超過cap都拒絕，不允許partial binding。
- Exact type mapping固定為：Places identity接受canonical `PlaceIdentityIntent`；current hours只接受
  `PlaceDetailsKind.CURRENT_HOURS`的token-gated `GooglePlaceDetailsRequest`；Routes只接受
  token-gated `GoogleRouteRequest`；SerpApi Hotels把process-local `LodgingDiscoveryRequest`當作
  private search intent，而不是provider provenance。Hours／hotel dates及route departure另須落在
  exact trip span內。
- Contract從exact typed preimage自行計算domain-separated SHA-256 fingerprint，caller沒有提供
  digest的欄位。Google request contracts還會重驗non-EEA static policy、非未來snapshot／purge time、
  endpoint freshness，並綁policy-registry、snapshot、store與evidence revisions；assessment要求caller
  重新提供exact preimages，任何preimage／revision／freshness drift都拒絕。
- 回傳binding只保留private fingerprints、source-line refs與revision bindings，不保留raw query、
  Place ID、payload或target object。Safe output只揭露topic／capability／request profile／target kind、
  bound target／line-reference counts、source-contract kind與revision-binding counts；fingerprint與line index
  本身不序列化。
- 完整binding只回`ready_for_private_provider_execution_authorization_review`與
  `prepare_private_provider_execution_authorization_review`。它不建立provider／HTTP request、不執行
  已傳入的request contract、不讀credential、不呼叫provider，且固定`provider_scope_authorized=false`、
  禁止partial authorization／provider-result自動授權follow-up，維持`candidate + unverified`。
- Correctness、product-contract與security agents均無剩餘actionable finding。新增9個專項回歸；完整
  816個offline tests、Python compile與三個real-trip validators均通過。`trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.17 private execution-authorization review：以本binding與重新提供的exact
  preimages產生可讓使用者理解的bounded target／資料傳送／exact-bound request-count review，並綁目前
  仍fresh的pricing／policy／retention／credential attestations；本階段仍不擷取authorization response、
  不建立HTTP request或呼叫provider。

### 2026-08-07 — Phase 5.17 private execution-authorization review 完成

- 新增獨立`guided_provider_execution_authorization_review.py`、token-gated
  `GuidedProviderExecutionAuthorizationReview`與prepare／assess API。Preparation先重驗完整Phase 5.16
  binding chain；assessment會在原preparation time重建exact review，再以當前trusted UTC重驗preflight、
  endpoint freshness、preimages與所有guided／scope／target／binding context。Expiry、clock rollback或任一
  context／profile／target drift都fail closed。
- 每個review item都從exact typed preimage衍生provider-transmitted field names／values與local review context；
  raw target object不保留。預設`to_dict()`只顯示欄位名稱、target／source-contract kind與aggregate counts；
  只有明確呼叫`to_ephemeral_private_review_payload()`才會顯示query、日期、旅客數與stable local labels，且
  payload明示只能process-local direct-human review、回覆擷取前必須重新assessment，不可持久化。
- Stable local labels改以`stable_local_*`命名並明示不是provider Place IDs；Google Place IDs只顯示redacted
  欄位名稱／數量，不顯示值。Safe與private projections均不含target fingerprint、provider request
  fingerprint、credential、billing address、policy-registry／snapshot／store／evidence revision。Billing只保留
  已驗證的non-EEA classification，不保留地址或其他身分資料。
- 每個item會由exact evidence declaration與refined line重建`source_state_counts`，分開列出user-stated、
  tentative與AI-candidate line counts；三者總和必須等於該target的source-line refs。所有來源行均明示
  `requires_verification`且不是authoritative，line text與indexes不輸出。
- `bound_request_count`是「若後續接受時」已exact-bound的請求數，不是已建立或已送出的request；它和
  accepted cap、Google／SerpApi provider counts分開。Google只提供目前versioned pricing profile的第一付費
  級距bound estimate與accepted-max estimate；SerpApi只列bound plan-credit count／cap，沒有currency list-rate
  時明示。月免費額度未查、試算不是hard currency cap，且execution前仍須重驗pricing／policy／retention／
  credential。
- Review固定`review_required`，只提供下一個exact response gate的`accept`／`request_smaller`／`cancel`
  選項。本切片不擷取response、不允許partial authorization、不授權scope；review所建立的provider request
  contract、HTTP request與觀察到的provider call counts都為0，沒有env／vault／credential access、trip write、
  scheduler、render、deploy或canonical apply path，維持`candidate + unverified`。
- Correctness、product-contract與security agents的初審findings已修正，複核均無剩餘actionable finding。
  新增10個專項回歸；完整826個offline tests、Python compile與三個real-trip validators均通過。
  `trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.18 exact execution-authorization response gate：只接受綁定同一份仍fresh私人
  review的`accept`／`request_smaller`／`cancel`；accept也只前進到獨立execution-time recheck／request-
  materialization gate，不能在capture或assessment內建立HTTP request、讀credential或呼叫provider。

### 2026-08-08 — Phase 5.18 exact execution-authorization response gate 完成

- 新增獨立`guided_provider_execution_authorization_response.py`、token-gated
  `GuidedProviderExecutionAuthorizationResponse`、typed `accept`／`request_smaller`／`cancel` enum與
  capture／assess API。它不做自然語言parser、不接受free text、target subset、cap修改或caller-supplied
  authorization digest；response只保存kind、private capture time與contract自行計算的context fingerprint。
- Capture會用exact preimages與trusted UTC重驗同一份Phase 5.17 visible private review；assessment先在原
  capture time重建review與response fingerprint，再以目前trusted UTC重驗完整guided／scope／preflight／
  targets／bindings／review chain。Clock rollback、preflight expiry、endpoint staleness、preimage／review／
  response-kind或任一context drift都fail closed。
- Safe handoff只顯示response kind、accepted／bound request caps、Google／SerpApi與capability counts、
  user-stated／tentative／AI-candidate source-reference counts、versioned Google bound／accepted-max list-rate
  estimate、SerpApi bound credit／cap及attestation flags。不顯示query、stable local label、provider Place ID、
  target／request fingerprint、credential、billing address、policy-registry／snapshot／store／evidence revision、
  raw preimage、private review values或capture／expiry time。
- `accept`只回`ready_for_private_provider_execution_time_recheck`與
  `prepare_private_provider_execution_time_recheck`；它只記錄exact review acceptance，不啟用immediate
  execution authority，且要求下一關重新提供preimages並重驗pricing／policy／retention／credential。
  Eligible count只是可進入recheck的exact-bound count，不代表已建立或已送出request。
- `request_smaller`只回`refine_private_provider_execution_targets`，不修改任何target、binding、cap、scope或
  evidence declaration；後續變更必須重建exact binding與新review。`cancel`只回
  `continue_private_evidence_review`並關閉目前external-execution path；若未來重開也必須產生新review。
  三個分支均保留evidence requirements且禁止partial authorization。
- Response建立的provider request contract、HTTP request與觀察到的provider call counts皆為0；沒有env／
  vault／credential access、network、trip／store write、scheduler、render、deploy、confirmation、canonical
  apply或authoritative-use path，維持`candidate + unverified`。
- Correctness、product-contract與security agents均無actionable finding。新增9個專項回歸；Phase 5.17相容
  tests與完整835個offline tests、Python compile、三個real-trip validators均通過。`trips/*/data` aggregate
  hash維持`91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.19 private execution-time recheck：只消費fresh Phase 5.18 accept response與同一
  批exact preimages，接受trusted host重新提供的current pricing／policy／retention／billing classification／
  boolean credential availability attestations並重驗endpoint freshness。它最多只前進到另一個exact request-
  materialization review，仍不讀取或保存credential、不建立HTTP request、不呼叫provider。

### 2026-08-08 — Phase 5.19 private execution-time recheck 完成

- 新增獨立`guided_provider_execution_time_recheck.py`、token-gated
  `GuidedProviderExecutionTimeRecheck`／review與prepare／assess API。Preparation只接受fresh Phase 5.18 exact
  `accept` response、同一批exact target preimages與trusted host新提供的typed attestations；topic、capability、
  request profile與request cap必須和已接受preflight完全相同。Recheck固定最多五分鐘，且不超過原preflight
  expiry。
- Assessment先在原checked time重建response、current preflight與recheck fingerprint，再以目前trusted UTC
  重驗完整guided／scope／preflight／targets／bindings／authorization chain、preimages與endpoint freshness。
  Clock rollback、任一context／preimage drift或舊accept不再current都fail closed。
- Current pricing、policy、retention、billing-region classification或SerpApi ZeroTrace狀態若與已接受review不同，
  只回`needs_new_private_provider_preflight_review`，必須重走preflight與authorization；台灣使用者的既有
  non-EEA分類不會被推測改寫。Credential unavailable、同provider credential狀態不一致、SerpApi plan credit
  不足／未確認、自動續費開啟或短期attestation過期則只回可重試的blocked recheck。
- Safe handoff保留既有accepted／bound request caps、Google／SerpApi與capability counts、source-state counts、
  versioned Google bound／accepted-max list-rate estimate、SerpApi bound credit／cap與current typed profile／boolean
  availability flags。不顯示private target、query、stable local label、provider identifier、target／request
  fingerprint、credential、billing address、exact SerpApi balance／renewal值、raw preimage、revision或時間。
- Ready只回`prepare_private_provider_request_materialization_review`；recheck建立的provider request contract、
  HTTP request與觀察到的provider call counts皆為0，沒有env／vault／credential access、network、trip／store
  write、scheduler、render、deploy、confirmation、canonical apply或authoritative-use path，維持
  `candidate + unverified`。
- Correctness、product-contract與security agents均無actionable finding。新增10個專項回歸；Phase 5.18相容
  tests與完整845個offline tests、Python compile、三個real-trip validators均通過。`trips/*/data` aggregate
  hash維持`91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.20 exact private request-materialization review：只消費fresh ready Phase 5.19與同一
  批exact preimages，產生可讓使用者再次審閱的typed provider-request contract candidate與exact transmitted
  fields／bound count；本切片仍不得建立HTTP request、讀取credential、授權或呼叫provider，且任一recheck
  expiry／context drift都必須重做Phase 5.19。

### 2026-08-08 — Phase 5.20 exact private request-materialization review 完成

- 新增獨立`guided_provider_request_materialization_review.py`、token-gated
  `GuidedProviderRequestContractCandidate`／`GuidedProviderRequestMaterializationReview`與prepare／assess API。
  Preparation只接受fresh、ready Phase 5.19 recheck及同一批exact preimages；assessment先在原prepared time
  重建recheck、authorization review與candidate fingerprint，再用目前trusted UTC重驗完整chain。Review
  expiry不超過原五分鐘recheck；clock rollback、expiry或任一context／preimage／recheck drift都fail closed。
- 從已重驗的exact Phase 5.17 private disclosures衍生四種provider-neutral materialization kind：Google Places
  Text Search、Place Details、Routes Compute Routes與SerpApi Google Hotels。每個exact-bound preimage各產生一個
  non-executable candidate；candidate count等於bound request count，允許同一scope topic在accepted cap內有
  多個requests，不把scope-item count誤當request count。
- Safe view只保留topic／capability／request profile／materialization kind／target kind、transmitted與local field
  names、source provenance、accepted／bound caps、Google／SerpApi counts、list-rate estimate／plan-credit cap及
  fresh pricing／policy／retention／billing／credential／plan-state recheck flags。不顯示query、日期、stable local
  label、provider identifier、fingerprint、credential、exact SerpApi plan state、raw preimage、revision或時間。
- 只有明確呼叫`to_ephemeral_private_review_payload()`才會顯示非identifier的exact transmitted values與local
  review context，並標成process-local direct-human review only；provider Place IDs仍只顯示redacted field names。
  Review本身不保留raw preimages，只保留derived private disclosure，capture前必須再次assessment。
- 下一個typed response options是`prepare_materialization`／`request_smaller`／`cancel`；命名與disclosure明示
  prepare只代表準備下一階段，不是立即materialize或execute。Candidate不可執行；executable provider request
  contract、HTTP request與觀察到的provider call counts皆為0，沒有transport endpoint／method selection、env／
  vault／credential access、network、trip／store write、scheduler、render、deploy、confirmation、canonical
  apply或authoritative-use path，維持`candidate + unverified`。
- Correctness、product-contract與security agents的初審與delta複核均無剩餘actionable finding；產品命名建議
  已採納。新增10個專項回歸，含四種materialization surfaces與1 topic／2 requests edge；完整855個offline
  tests、Python compile、三個real-trip validators均通過。`trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.21 exact request-materialization response gate：只擷取同一份仍fresh私人review的
  typed `prepare_materialization`／`request_smaller`／`cancel`。Prepare也只前進到另一個exact provider-request
  contract materialization gate；capture／assessment內仍不得建立executable request／HTTP request、讀取
  credential、授權provider call或執行network side effect。

### 2026-08-08 — Phase 5.21 exact request-materialization response gate 完成

- 新增獨立`guided_provider_request_materialization_response.py`、token-gated
  `GuidedProviderRequestMaterializationResponse`、typed `prepare_materialization`／`request_smaller`／`cancel`
  enum與capture／assess API。它不做自然語言parser、不接受free text、target subset、cap修改或caller-supplied
  authorization digest；response只保存kind、private capture time與contract自行計算的context fingerprint。
- Capture用同一批exact preimages與trusted UTC重驗Phase 5.20 visible private review；assessment先在原capture
  time重建review與response fingerprint，再以目前trusted UTC重驗完整guided／scope／preflight／targets／
  bindings／authorization／execution-time recheck／materialization-review chain。Clock rollback、五分鐘recheck
  expiry、endpoint staleness、preimage／review／response-kind或任一context drift都fail closed。
- Safe handoff保留response kind、scope-topic／candidate／bound request counts、accepted cap、Google／SerpApi與
  capability counts、source-state provenance、list-rate estimate／plan-credit cap及fresh pricing／policy／retention／
  billing／credential／plan-state flags。不顯示query、stable local label、provider identifier、fingerprint、
  credential、exact plan state、private candidate values、raw preimage、revision或capture／expiry time。
- `prepare_materialization`只表示接受exact private candidate review，回
  `ready_for_private_provider_request_contract_materialization`與
  `prepare_private_provider_request_contract_materialization`；它仍只是準備下一個獨立gate，不立即materialize、
  execute或啟用authority。Eligible count只是可進下一關的exact-bound candidate數，不代表request已建立。
- `request_smaller`只回`refine_private_provider_execution_targets`，不修改target、binding、candidate、cap、scope
  或evidence declaration，後續必須重建整條exact chain。`cancel`只回`continue_private_evidence_review`並關閉
  目前materialization path；若重開也必須產生新review。三個分支均保留evidence requirements且禁止partial
  materialization authorization。
- Response建立的新candidate、executable provider request contract、HTTP request與觀察到的provider call
  counts皆為0；沒有env／vault／credential access、network、trip／store write、scheduler、render、deploy、
  confirmation、canonical apply或authoritative-use path，維持`candidate + unverified`。
- Correctness、product-contract與security agents均無actionable finding。新增9個專項回歸，含三分支、
  1 topic／2 requests與SerpApi credit；Phase 5.20相容tests與完整864個offline tests、Python compile、三個
  real-trip validators均通過。`trips/*/data` aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.22 exact private provider-request contract materialization：只消費fresh
  `prepare_materialization` response與同一批exact preimages，再準備token-gated、不可送出的provider request
  contracts；仍不建立HTTP transport、注入／讀取credential或呼叫provider，且必須保留另一個explicit send
  authorization gate。

### 2026-08-08 — Phase 5.22 exact private provider-request contract materialization 完成

- 新增獨立`guided_provider_request_contract_materialization.py`、token-gated
  `GuidedProviderRequestContract`／`GuidedProviderRequestContractMaterialization`／review與materialize／assess API。
  Materialize只接受fresh Phase 5.21 exact `prepare_materialization` response與同一批exact preimages；
  `request_smaller`／`cancel`不能進入此路徑。
- 每個已reviewed exact-bound preimage各產生一個process-local contract，支援Google Places Text Search、Place
  Details、Routes Compute Routes與SerpApi Google Hotels四種typed surface。Contract保留exact provider-transmitted
  values；Place Details／Routes的provider Place IDs、stable local result binding與source-binding fingerprint均為
  private fields，且bundle不保留raw target preimage。Candidate／contract／bound-request counts維持相等；同一個
  scope topic可在accepted cap內保有多個不同requests。
- Assessment先在原materialization time重建Phase 5.21 response、四種contracts與bundle fingerprint，再以目前
  trusted UTC重驗完整guided／scope／preflight／targets／bindings／authorization／recheck／materialization
  chain。Bundle expiry沿用且不超過Phase 5.19五分鐘recheck；clock rollback、expiry、preimage／contract／
  response-kind或任一context drift都fail closed。
- Safe view只顯示topic／capability／request／pricing／policy／retention／target profiles、private field names、
  source-state provenance、accepted／bound caps、Google／SerpApi counts、list-rate estimate／plan-credit cap與fresh
  attestation flags。不顯示exact query／日期、stable local label、provider identifier、source／context fingerprint、
  credential、exact SerpApi plan state、raw preimage、revision或materialization／expiry time。
- Contracts明確non-executable且non-sendable；沒有transport endpoint、HTTP method、credential slot/value、HTTP
  request、send／execution authority、env／vault access、network、provider call、trip／store write、scheduler、
  render、deploy、confirmation、canonical apply或authoritative-use path，維持`candidate + unverified`。Ready只回
  `prepare_private_provider_request_send_authorization_review`。
- Correctness、product-contract與security agents均無actionable finding。新增10個Phase 5.22專項回歸，含四種
  typed request surfaces、1 topic／2 requests、SerpApi credit、expiry／drift／redaction／token-gating；Phase 5.21
  相容tests與完整874個offline tests、Python compile、三個real-trip validators均通過。`trips/*/data`的23個
  files aggregate hash維持`91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改
  `trips/`。
- 下一個最小切片是Phase 5.23 exact private provider-request send-authorization review：只消費fresh Phase 5.22
  materialization與同一批exact preimages，讓使用者在任何transport／credential binding前再次審閱將送出的
  exact contracts。此review本身仍不得選endpoint／HTTP method、建立HTTP request、讀取credential、授權或
  呼叫provider；真正send response與execution仍保留在後續獨立gate。

### 2026-08-08 — Phase 5.23 exact private provider-request send-authorization review 完成

- 新增獨立`guided_provider_request_send_authorization_review.py`、token-gated
  `GuidedProviderRequestSendAuthorizationReview`與prepare／assess API。Preparation只接受fresh Phase 5.22 exact
  materialization及同一批exact preimages；review綁完整materialization assessment、private contracts、原
  prepared time、沿用的五分鐘expiry與自行計算的context fingerprint，不保留raw preimages。
- Assessment先在原prepared time重建Phase 5.22 materialization assessment與Phase 5.23 review fingerprint，再以
  目前trusted UTC重驗完整guided／scope／preflight／targets／bindings／authorization／recheck／materialization
  chain。Clock rollback、expiry、preimage／contract／review或任一context drift都fail closed；同一scope topic
  的多個materialized requests仍各自保留且不得超過accepted cap。
- Safe view只顯示contract shapes／field names、source-state provenance、accepted／bound caps、Google／SerpApi
  counts、list-rate estimate／plan-credit cap、fresh attestation flags與typed `accept_send`／`request_smaller`／
  `cancel` options。不顯示query、日期、stable local label、provider identifier、source／context fingerprint、
  credential、exact SerpApi plan state、revision或review／expiry time。
- 只有明確呼叫`to_ephemeral_private_review_payload()`才顯示exact non-identifier provider-transmitted values與
  exact local-result-binding values，並標成process-local direct-human review only。Provider Place IDs仍只顯示
  redacted field names；`exact_private_values_included=false`明示完整private set並未外露。Review只回
  `capture_private_provider_request_send_authorization_response`，尚未擷取任何option。
- `accept_send`只是下一個typed response choice，不是目前已存在的send authority。Review不選transport endpoint／
  HTTP method、不綁或讀credential、不建立HTTP request，也沒有send／execution authority、env／vault access、
  network、provider call、trip／store write、scheduler、render、deploy、confirmation、canonical apply或
  authoritative-use path，維持`candidate + unverified`。
- Correctness與security agents無actionable finding；product-contract agent提出的P2 metadata命名一致性已修正並
  複核clean。新增9個Phase 5.23專項回歸，含四種typed private surfaces、1 topic／2 requests、SerpApi credit、
  expiry／drift／redaction／token-gating；Phase 5.22相容tests與完整883個offline tests、Python compile、三個
  real-trip validators均通過。`trips/*/data`的23個files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.24 exact provider-request send-authorization response gate：只擷取同一份仍fresh
  private review的typed `accept_send`／`request_smaller`／`cancel`，不解析free text、不接受caller digest或
  target／cap mutation。Response capture與assessment本身仍不得選transport、綁credential、建立HTTP request或
  呼叫provider；任何實際send preparation繼續留在後續獨立gate。

### 2026-08-08 — Phase 5.24 exact provider-request send-authorization response gate 完成

- 新增獨立`guided_provider_request_send_authorization_response.py`、token-gated
  `GuidedProviderRequestSendAuthorizationResponse`、typed `accept_send`／`request_smaller`／`cancel` enum與
  capture／assess API。它不做自然語言parser、不接受free text、target subset、cap mutation或caller-supplied
  authorization digest；response只保存kind、private capture time與contract自行計算的context fingerprint。
- Capture用同一批exact preimages與trusted UTC重驗Phase 5.23 visible private review；assessment先在原capture
  time重建review與response fingerprint，再以目前trusted UTC重驗完整guided／scope／preflight／targets／
  bindings／authorization／execution-time recheck／materialization／send-review chain。Clock rollback、五分鐘
  recheck expiry、endpoint staleness、preimage／contract／review／response-kind或任一context drift都fail closed。
- Safe handoff只保留response kind、materialized-contract／bound-request counts、accepted cap、Google／SerpApi與
  capability counts、source-state provenance、list-rate estimate／plan-credit cap及fresh pricing／policy／retention／
  billing／credential／plan-state flags。不顯示exact request values、stable local label、provider identifier、
  source／context fingerprint、credential、exact SerpApi plan state、raw preimage、revision或capture／expiry time。
- `accept_send`只表示接受exact private send review，回
  `ready_for_private_provider_request_send_preparation`與`prepare_private_provider_request_send_preparation`；它只
  前進到後續獨立send-preparation gate，不使contract可送出、不選transport／HTTP method、不綁credential，也不
  啟用immediate send authority。Eligible count只是可進下一關的exact-bound contract數，不代表request已送出。
- `request_smaller`只回`refine_private_provider_execution_targets`，不修改target、binding、contract、cap、scope或
  evidence declaration，後續必須重建整條exact chain。`cancel`只回`continue_private_evidence_review`並關閉目前
  send path；若重開也必須產生新review。三個分支均保留evidence requirements且禁止partial send authorization。
- Response建立的executable provider request contract與HTTP request counts皆為0；沒有env／vault／credential
  access、transport selection、network、provider call、trip／store write、scheduler、render、deploy、confirmation、
  canonical apply或authoritative-use path，維持`candidate + unverified`。
- 新增9個Phase 5.24專項回歸，含三分支、exact enum、1 topic／2 requests、SerpApi credit、expiry／drift／
  redaction／token-gating與module isolation；Phase 5.23相容test已窄化為允許intentional typed response API但仍
  禁止review module直接capture、send或network。完整892個offline tests、Python compile、三個real-trip
  validators均通過。`trips/*/data`的23個files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是獨立private provider-request send-preparation contract：只消費fresh Phase 5.24
  `accept_send` response、同一批exact preimages與materialized contracts，將它們綁到allowlisted transport
  profile、endpoint／HTTP method與credential slot。該切片仍不得讀取credential value、建立credential-bearing
  HTTP request或呼叫provider；任何真實credential binding與send都必須保留在另一個明確live gate。

### 2026-08-08 — Phase 5.25 exact private provider-request send preparation 完成

- 新增獨立`guided_provider_request_send_preparation.py`、token-gated
  `GuidedProviderRequestTransportBinding`／`GuidedProviderRequestSendPreparation`與prepare／assess API。Preparation
  只接受fresh Phase 5.24 exact `accept_send` response、完整guided context、同一批exact preimages與同一份
  materialized contracts；`request_smaller`／`cancel`、generic continue或任何非exact response均不能前進。
- 四種materialization surface綁到2026-08-08重新核對的official transport allowlist：Google Places Text Search
  v1為`POST https://places.googleapis.com/v1/places:searchText`、Place Details v1為
  `GET https://places.googleapis.com/v1/places/{provider_place_id}`、Routes Compute Routes v2為
  `POST https://routes.googleapis.com/directions/v2:computeRoutes`，SerpApi Google Hotels為
  `GET https://serpapi.com/search.json`並固定query parameter `engine=google_hotels`。Google credential slot只記
  `X-Goog-Api-Key` header，SerpApi只記`api_key` query parameter；provider-transmitted／identifier field也只綁
  公開provider field name與header／JSON body／query／URL path placement。
- Prepare在trusted UTC重驗Phase 5.24 response與Phase 5.22 exact contract assessment；assessment再於原prepared time
  重建完整bundle fingerprint，並於目前trusted UTC重驗整條Phase 5.11–5.24 chain。Bundle不延長Phase 5.19原五
  分鐘expiry；clock rollback、expiry、preimage／context／response／contract／transport binding drift或cross-context
  replay均fail closed，bundle不保留raw preimage。
- Safe view可顯示上述公開endpoint template、method、field placement、credential slot、profile counts及既有
  request／cost／credit／provenance aggregates，但不顯示exact query／date、stable local label、provider identifier
  value、source／context fingerprint、credential value、SerpApi exact plan state或時間。Exact contract只在
  process-local private binding內保留，結果仍固定`candidate + unverified`。
- Module不import OS／filesystem／HTTP／socket／subprocess surface，沒有env／vault／credential value access，也不
  展開Place ID path、不建立query／headers／JSON或HTTP request、不啟用send／execution authority、不呼叫provider，
  且不做trip／store write、schedule、render、deploy、confirmation或canonical mutation。Next action只到
  `prepare_private_provider_request_credential_binding_review`，不是credential binding或live send。
- 新增9個Phase 5.25專項回歸，涵蓋四種official transport profile、exact `accept_send`、1 topic／2 requests、
  SerpApi plan credit、expiry／drift／redaction／token-gating與module isolation；Phase 5.24相容9 tests亦全過。
  完整901個offline tests（934.691秒）、Python compile與三個real-trip validators均通過。
  `trips/*/data`的23個files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.26 exact private provider-request credential-binding review：只消費fresh Phase 5.25
  transport-bound bundle、同一批exact preimages與完整context，產生本次current-user可審閱的transport／credential
  slot handoff及typed response options。該review仍不得讀取credential value或env／vault、建立HTTP request或
  呼叫provider；任何credential value binding與live send仍保留在之後另一個明確授權gate。

### 2026-08-08 — Phase 5.26 exact private provider-request credential-binding review 完成

- 新增獨立`guided_provider_request_credential_binding_review.py`、token-gated
  `GuidedProviderRequestCredentialBindingReview`與prepare／assess API。Review只接受fresh Phase 5.25
  `GuidedProviderRequestSendPreparation`、完整guided context與同一批exact preimages，回`review_required`及
  `capture_private_provider_request_credential_binding_response`；本階段沒有response capture API。
- Review只提供typed `accept_credential_binding`／`request_smaller`／`cancel` options，不解析自然語言、不接受
  free text、caller digest、target subset、cap mutation或partial response。這些options只屬下一個response gate；
  `accept_credential_binding`本身不授予credential access、binding、HTTP construction、send或execution authority。
- Prepare先在trusted UTC重驗Phase 5.25 send preparation；assessment再於原prepared time重建完整review與
  fingerprint，並於目前trusted UTC重驗Phase 5.11–5.25 chain。Review繼承原Phase 5.19五分鐘expiry且不延長；
  clock rollback、expiry、preimage／context／response／contract／transport profile／credential-slot drift、tamper或
  cross-context replay均fail closed，review不保留raw preimage。
- Safe view只顯示公開endpoint template、HTTP method、provider field placement、credential slot、profile與既有
  request／cost／credit／provenance aggregates。只有明確呼叫`to_ephemeral_private_review_payload()`才顯示exact
  non-identifier provider-transmitted values；provider identifier values、local-result values、source／context
  fingerprints、credential values、SerpApi exact plan state及private times在兩種view都不顯示，Place ID path不展開。
- Module沒有OS／filesystem／HTTP／socket／subprocess import，不讀env／vault、不取得或保存credential value、不
  建立headers／query／JSON／URL或HTTP request、不使用network、不呼叫provider，也不做trip／store write、
  schedule、render、deploy、confirmation或canonical mutation，結果維持`candidate + unverified`。
- 新增8個Phase 5.26專項回歸，涵蓋四transport profiles、ephemeral exact non-identifier payload、1 topic／2
  requests、expiry／drift／redaction／token-gating與module isolation；Phase 5.25相容9 tests亦全過。完整909個
  offline tests（1226.797秒）、Python compile與三個real-trip validators均通過。`trips/*/data`的23個files
  aggregate hash維持`91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.27 exact credential-binding response-only gate：只擷取同一份仍fresh Phase 5.26 review
  的typed `accept_credential_binding`／`request_smaller`／`cancel` enum，並在original／current trusted UTC以同一批
  preimages重驗完整chain。Acceptance仍只能準備另一個明確live credential-binding gate，不得讀key value、
  建立HTTP request或呼叫provider。

### 2026-08-08 — Phase 5.27 exact credential-binding response-only gate 完成

- 新增獨立`guided_provider_request_credential_binding_response.py`、token-gated
  `GuidedProviderRequestCredentialBindingResponse`／`Review`與capture／assess API。Capture只接受exact typed
  `accept_credential_binding`／`request_smaller`／`cancel` enum；不解析自然語言、不保存free text，也不接受
  caller digest、target subset、cap mutation或partial response。一般進度指令或「繼續」不等於
  `accept_credential_binding`。
- Capture在trusted UTC重驗同一份visible Phase 5.26 review；assessment先於原capture time重建review與response
  fingerprint，再於目前trusted UTC重驗完整Phase 5.11–5.26 chain。Response沿用Phase 5.19五分鐘expiry且不延長；
  clock rollback、expiry、preimage／context／review／contract／transport drift、tamper或cross-context replay均
  fail closed，response與review都不保留raw preimage。
- `accept_credential_binding`只回
  `prepare_private_provider_request_live_credential_binding_gate`，代表可準備另一個獨立live gate，不授予目前的
  credential access／binding、HTTP construction、send或execution authority。`request_smaller`只回
  execution-target refinement；`cancel`關閉目前credential-binding path。三個分支都不改scope／cap、target／
  binding、materialized contract或evidence requirements。
- Safe view只顯示response kind、transport profile與既有request／cost／credit／provenance aggregates；不保留
  private transport metadata，也不顯示exact request values、provider／local IDs、source／context fingerprints、
  credential values、SerpApi exact plan state或private times，結果維持`candidate + unverified`。
- Module沒有OS／filesystem／HTTP／socket／subprocess import，不讀env／vault、不取得、保存或綁定credential
  value、不展開Place ID path、不建立headers／query／JSON／URL或HTTP request、不使用network、不呼叫provider，
  也不做trip／store write、schedule、render、deploy、confirmation或canonical mutation。
- 新增9個Phase 5.27專項回歸；一條完整exact-chain E2E搭配enum／三分支、original／current recheck、上游失效
  傳遞、rollback／tamper、redaction、token-gating、aggregate preservation與module isolation測試，9 tests均通過
  （217.595秒）；Phase 5.26相容8 tests亦全過（289.957秒）。完整918個offline tests（1438.298秒）、Python
  compile與三個real-trip validators均通過。`trips/*/data`的23個files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.28 explicit live credential-binding gate preparation／review：只消費fresh Phase 5.27
  exact accept response、同一批exact preimages與完整context，先以host提供的boolean credential／session
  availability attestation fail closed，再產生本次current-user可審閱的單次live binding handoff。該切片不得在
  safe output顯示key value，也不得把一般「繼續」當作live授權；HTTP construction與provider call仍須留在後續
  另一個明確bounded gate。

### 2026-08-08 — Phase 5.28 explicit live credential-binding preparation／review 完成

- 新增獨立`guided_provider_request_live_credential_binding_review.py`、typed
  `GuidedProviderRequestCredentialAvailabilityAttestation`、token-gated
  `GuidedProviderRequestLiveCredentialBindingReview`與prepare／assess API。Preparation只接受fresh Phase 5.27
  exact `accept_credential_binding` response、完整guided context、同一批exact preimages及每個實際required
  credential slot的一筆boolean-equivalent `available`／`unavailable` host attestation。
- Attestation只含public credential-slot enum與availability enum，不含credential value、secret identifier、env name、
  vault path或session token。Slot缺漏、重複、多餘、非exact enum或與Phase 5.25 transport bundle不符一律在上游
  assessment前fail closed；同一slot可覆蓋多個bindings，但不能省略任何不同slot。
- 任一slot unavailable只回`blocked`及
  `refresh_private_provider_request_credential_availability_attestation`，不顯示response options。全部available才回
  `review_required`及`accept_live_credential_binding`／`request_smaller`／`cancel`；一般進度指令或「繼續」不等於
  `accept_live_credential_binding`，本階段也尚未提供response capture API。
- Prepare在trusted UTC重驗Phase 5.27 response；assessment先於原prepared time重建response review、attestations與
  fingerprint，再於目前trusted UTC重驗完整Phase 5.11–5.27 chain。Review沿用Phase 5.19 expiry且不延長；clock
  rollback、expiry、preimage／context／response／transport／slot／attestation drift、tamper或replay均fail closed，
  review不保留raw preimage。
- Safe view只顯示slot名稱、availability boolean、transport-profile counts與既有request／cost／credit／provenance
  aggregates；不顯示exact request values、provider／local IDs、source／context fingerprints、credential values、
  SerpApi exact plan state或private times，結果維持`candidate + unverified`。
- Module沒有OS／filesystem／HTTP／socket／subprocess import，不讀env／vault、不取得、保存或綁定credential
  value、不展開Place ID path、不建立headers／query／JSON／URL或HTTP request、不使用network、不呼叫provider，
  也不做trip／store write、schedule、render、deploy、confirmation或canonical mutation。
- 新增9個Phase 5.28專項回歸；一條完整Phase 5.27 accept→Phase 5.28 preparation E2E搭配available／unavailable、
  slot coverage、exact accepted branch、original／current recheck、upstream failure propagation、rollback／expiry／
  tamper、redaction、token-gating與module isolation測試，9 tests均通過（222.966秒）；Phase 5.27相容9 tests
  亦全過（220.130秒）。完整927個offline tests（1673.606秒）、Python compile與三個real-trip validators均
  通過。`trips/*/data`的23個files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 下一個最小切片是Phase 5.29 exact live credential-binding response-only gate：只擷取同一份仍fresh Phase 5.28
  review的typed `accept_live_credential_binding`／`request_smaller`／`cancel` enum，並在original／current trusted
  UTC以同一批preimages與availability attestations重驗完整chain。Acceptance仍只能準備另一個明確ephemeral
  credential-value binding gate，不得在response capture中讀key、建立HTTP request或呼叫provider。

### 2026-08-08 — Phase 5.29 exact live credential-binding response-only gate 完成

- 新增獨立`guided_provider_request_live_credential_binding_response.py`、token-gated
  `GuidedProviderRequestLiveCredentialBindingResponse`／`Review`與capture／assess API。Capture只接受exact typed
  `accept_live_credential_binding`／`request_smaller`／`cancel` enum；不解析自然語言、不保存free text，也不接受
  caller digest、target subset、cap mutation或partial response。一般進度指令或「繼續」不等於
  `accept_live_credential_binding`。
- Capture只接受仍fresh、`review_required`且所有required credential slots仍available的Phase 5.28 review；
  `blocked`／unavailable review無法擷取response。Capture先於trusted UTC重驗目前review，assessment再於原
  capture time及目前trusted UTC兩次呼叫Phase 5.28 assessor，以同一批exact preimages、transport bindings、
  slot coverage與原availability attestations重驗完整Phase 5.11–5.28 chain。
- Response沿用Phase 5.19五分鐘expiry且不延長；clock rollback、expiry、preimage／context／review／transport／
  slot／attestation drift、fingerprint tamper或replay均fail closed，response不保留raw preimage或transport metadata。
- `accept_live_credential_binding`只回
  `prepare_private_provider_request_ephemeral_credential_value_binding_gate`；它只能準備後續獨立短效gate，不能讀取
  或綁定credential value。`request_smaller`只回`refine_private_provider_execution_targets`並要求重建後續chain；
  `cancel`回`continue_private_evidence_review`並關閉目前live-binding path。三個分支都不修改scope、caps、targets、
  bindings、contracts、availability attestations或evidence requirements。
- Safe view只顯示response kind、public slot名稱與availability boolean、transport-profile counts及既有request／cost／
  credit／provenance aggregates；不顯示exact request values、provider／local IDs、source／context fingerprints、
  credential values、SerpApi exact plan state或private times，結果維持`candidate + unverified`。
- Module沒有OS／filesystem／HTTP／socket／subprocess import，不讀env／vault、不取得、保存或綁定credential
  value、不展開Place ID path、不建立headers／query／JSON／URL或HTTP request、不使用network、不呼叫provider，
  也不做trip／store write、schedule、render、deploy、confirmation或canonical mutation，且不授予send／execution
  authority。
- 新增10個Phase 5.29專項回歸；一條完整Phase 5.28 review→Phase 5.29 capture E2E搭配三分支、exact enum、
  blocked review、original／current recheck、rollback／expiry／tamper、redaction、aggregate preservation、token-gating與
  module isolation，10 tests均通過（461.605秒）；Phase 5.28相容9 tests亦全過（225.245秒）。完整937個offline
  tests（2162.881秒）、Python compile與三個real-trip validators均通過。`trips/*/data`的23個files aggregate hash
  維持`91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`，未修改`trips/`。
- 此checkpoint後先執行M0 rebaseline與測試收斂，不再逐一新增runtime-gate phase。下一個產品切片是上方
  有限計畫中的Phase 5.30 composed pre-execution facade；Phase 5.13–5.29則凍結為
  `Provider Execution Safety Reference v1`，不得刪除、弱化或以新抽象重寫。

### 2026-08-08 — M0 Phase 5 rebaseline／test fixture convergence 完成

- Roadmap現只保留Phase 5.30–5.33四個macro phases，並明訂沒有默認Phase 5.34；Phase 5.13–5.29
  凍結為`Provider Execution Safety Reference v1`。Runtime safety gates、roadmap delivery phases與
  full-test cadence不再混用同一套編號。
- 新增test-only `phase5_fixture_cache.py`。無參數default builders只共用frozen process-local graph；
  explicit builder arguments仍建立獨立fixture。Guided module內匯入的pure upstream assessors另以完整
  positional／keyword input作checkpoint key；unhashable values以保留物件的identity key隔離。每個module
  自己正在受測的public assessor不包cache，因此first traversal仍執行真實production contract，任何context、
  time、preimage、branch或attestation變化也會產生新key並重新驗證。
- Phase 5.10–5.29 default fixture helpers已接上共享checkpoint；Phase 5.28／5.29另外共用同一份實際建立的
  live credential review／response graph。沒有刪除或改寫任一既有test method、assertion、production module或
  Safety Reference contract。
- Phase 5 focused 277 tests全過（6.735秒）；最深的Phase 5.28–5.29 compatibility 19 tests全過
  （0.314秒）。完整937個offline tests全過（27.656秒），完整`bash scripts/check.sh`含Python compile與三個
  real-trip validators共30.07秒，低於M0約15分鐘目標；相較Phase 5.29 checkpoint的2162.881秒test baseline，
  test runtime約縮短78倍。
- `trips/*/data`仍為23個files，aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。本M0沒有修改`trip_planner/`、
  `scripts/`、`trips/`或使用者既有`.envrc.example`變更，沒有provider call、credential access、render、
  deploy或canonical mutation。

### 2026-08-08 — Phase 5.30 composed private pre-execution facade 完成

- 新增`guided_provider_pre_execution.py`，以token-gated、sealed且不可序列化的
  `GuidedProviderPreExecutionContext`／`GuidedProviderPreparedRequest`／ephemeral credential lease／single-use
  execution claim，把fresh Phase 5.29 exact accept及既有完整guided chain收斂成一個bounded handoff。Raw
  preimages仍只在prepare／assess時以keyword-only重新提供，不會被context或bundle保留。
- Prepare先重驗Phase 5.29，再由host-injected resolver為每個distinct public slot取值恰好一次；resolver後以新的
  trusted UTC重驗Phase 5.29與Phase 5.25 exact transport bundle。Lease自身檢查context、rollback與expiry；所有
  sealed metadata、request endpoint及claim state都不能由普通assignment延長、重置或變造。Consent與execution
  claim皆single-use，過期registry entries會清理，任何失敗都以sanitized exception及清空暫存credential fail closed。
- 四個profile-specific builders物化exact private wire descriptors：Places Text Search固定`pageSize=5`，Details
  percent-encode Place ID，Routes使用provider mode mapping並固定`computeAlternativeRoutes=false`，SerpAPI Hotels
  固定engine。兩個provider-visible fixed fields也加入Phase 5.25 reviewed allowlist；不同consent context產生不同
  request fingerprint。Safe review只保留public slot／profile／request／cost／credit aggregates與immutable
  assessment-time state，不顯示credential、query、provider／local ID、private time或fingerprint。
- 兩位獨立agent完成correctness／security／integration重驗，沒有blocking finding。新增10個Phase 5.30專項回歸；
  Phase 5.25、5.29、5.30共29個focused tests全過。完整947個offline tests（27.862秒）、Python compile與三個
  real-trip validators均通過；23個trip data files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。
- 本phase沒有內建transport、network或provider call，也沒有EvidenceStore／TripStore、schedule、render、deploy或
  canonical mutation；沒有讀取真實credential或修改`trips/`。下一個macro phase是5.31 bounded injected-transport
  execution與private quarantine；不為非阻斷hardening另增phase。

### 2026-08-08 — Phase 5.31 bounded provider execution／private quarantine 完成

- 新增`guided_provider_execution.py`，以唯一的host-injected transport seam消耗Phase 5.30 single-use claim。
  Executor先複製caller limits與每筆prepared request的exact wire／cost／credit資料；每次send前原子保留attempt、
  Google list cost／SerpAPI credit及timeout／absolute deadline，先完成全request initial pass，再只對
  `outcome_unknown`使用同一credential-bearing wire最多重試一次。Transport沒有內建live client，並收到1 MiB body、
  32 headers及每個header name／value的streaming上限；所有terminal path都重新取trusted UTC。
- Delivery只接受typed `known_not_sent`證明未送；invalid return、malformed exception certainty、buffer cap違反、
  post-send clock失效或deadline越界都保守視為unknown並停止自動前進。Claim後任何普通／`BaseException`失敗都會
  清除已建立wire與claim前捕捉的原始credential lease；caller在send期間替換bundle欄位不能略過cleanup。
- 每筆prepared request與Places Identity／Details／Routes／SerpAPI Hotels native target都在claim前建立private exact
  fingerprint，並在每次send前後重算。Binding涵蓋完整contract／wire／cost、snapshot／policy、private Place ID、
  hotel query及lease metadata，但safe output只顯示public aggregate；request kind／cost、nested policy／purge、endpoint、
  query或caller caps的TOCTOU drift一律fail closed。
- Valid raw HTTP response只進sealed、不可序列化的`GuidedProviderQuarantinedResponse`，保留exact prepared request、
  native adapter target與context／source／request／target bindings供Phase 5.32重驗。Quarantine及aggregate仍為
  `candidate + unverified`；本phase沒有generic normalizer、AuthorizedProviderResult、EvidenceStore／EvidenceSession、
  TripStore／PlanPatch、schedule、render、deploy或canonical write。
- 兩位獨立agent完成architecture/security與Phase 5.32 integration重驗。新增22個Phase 5.31專項回歸；Phase 5.25、
  5.29、5.30、5.31共51個focused tests全過。完整969個offline tests（29.365秒）、Python compile與三個real-trip
  validators均通過；23個trip data files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。全程沒有讀取真實credential、呼叫live
  provider、修改`trips/`或deploy；下一個macro phase是5.32 provider-specific evidence-to-canonical workflow。

### 2026-08-08 — Phase 5.32 provider evidence-to-canonical workflow 完成

- 新增`guided_provider_evidence_workflow.py`，先重驗Phase 5.31 quarantine的exact context／prepared request／source／
  native target／response bindings，再分派到Places Identity、Place Details、Routes或SerpAPI Hotels既有typed adapter。
  每個profile都重新套用HTTP status、send-time precondition、strict JSON tree及adapter自己的response byte bounds；沒有
  generic raw-result normalizer，invalid／oversized／late結果不能進evidence。
- Identity仍經既有candidate evaluator與host review finalizer，durable merge以review basis store revision在EvidenceStore
  lock內做CAS；retention purge先於CAS，exact lost-ACK observation replay先於stale拒絕。Details／Routes只路由到
  process-local EvidenceSession；Hotels維持non-provenance discovery DTO，不能升格為AuthorizedProviderResult。
  Production-contract E2E證明Routes observation確實改變composition digest及scheduler input，另有直接Identity durable
  merge regression；所有safe output持續隱藏raw response、query、provider ID與private fingerprints。
- 新增token-gated`PlanCreateRequest`及TripStore create preview／commit。Initial candidate只能由完整、已接受的Phase 5.9
  guided source投影，使用internally computed source binding並在review顯示exact canonical candidate；非空transport
  boundaries及lodging／hotel／Airbnb aliases在尚未有安全投影前fail closed。Create與migration使用atomic no-replace
  install、exact target binding與receipt-first lost-ACK replay；legacy source或canonical任一已存在時，generic create不會
  覆寫或繞過migration。
- 新增`guided_canonical_apply.py`的exact enum response gate。Create／migration使用原preview；repair／schedule／lodging
  只能包裝既有controller或stager的同一份pending review，沒有raw `PlanPatch`直通路徑。Process-wide bounded registry
  阻止相同logical review取得衝突decision；`accept_apply`可作同review的人類checkpoint，但不自動mint protected
  `ApprovalGrant`，也不能取代externally signed lodging grant。Store target、trusted clock、evidence／approval binding、
  post-commit waiting state及exact replay output都保持truthful；replay不宣稱本次又寫入。
- 三個Phase 5.32 test modules共38個專項回歸，涵蓋四provider profiles、evidence routing／CAS、composition、create／
  migration no-replace、informed review、controller/stager authority、conflicting response、clock rollback、wrong-root replay、
  post-commit evidence drift及domain replay output。獨立integration review在final snapshot無blocking finding；
  architecture/security review提出的5.32 lost-ACK與domain composition缺口已補回，migrated baseline明確保留給5.33，
  最終本地架構／安全快驗無剩餘5.32 blocking finding。完整1007個offline tests（29.550秒）、Python compile與三個
  real-trip validators均通過；23個
  trip data files aggregate hash維持
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。
- 本phase全程使用temporary stores與injected canned responses；沒有讀取真實credential、呼叫live provider、修改
  `trips/`、render或deploy。下一個且最後一個Phase 5 macro phase是5.33 unified `tripctl`、resume／retry、migrated
  baseline adoption、skill及Busan／Hokkaido canned product acceptance。

### 2026-08-03 — Phase 6.0 fail-closed public release boundary 完成

- 新增獨立的 `trip_planner.public_release`、`public_*.html` templates 與 public-only
  build / prepare scripts。公開來源只能放在 `public/trips/{slug}.json`；strict schema
  不接受地址、座標、URL、地圖、reservation、price、cache 或任意欄位。private legacy
  renderer、`build_index.py` 與 `trips/` 流程保持私有，公開 builder 不讀它們。
- `prepare_public_release.py` 對一個以上 slug 只印出 no-write 的完整候選 manifest；
  `release.json` 綁 exact public JSON、trip HTML 與首頁 HTML digest。build 會拒絕
  source／template／排序漂移、symbolic link、hard link、unsafe source、空白或額外 artifact，
  並只生成首頁、每趟摘要頁與 artifact manifest。
- `deploy.sh` 不再 render private trips、複製 ICS 或重建 private index。沒有
  `public/release.json` 時，在 temporary build、git init 或任何網路動作前拒絕；它仍只在
  使用者明確要求發布時可執行。
- 新增九個 offline regression cases，覆蓋 deterministic exact artifacts、private sentinel
  exclusion、HTML escaping、strict JSON、source／trip-template／index-template／ordering drift、
  source/template symlink、source hard link、兩個 directory-swap race 與 deploy early refusal。
  完整 679 個 offline tests、Python compile 與三個 real-trip validators 均通過；
  `trips/*/data` aggregate hash 仍是
  `91288401c83f2b5e1d30bcfa6b739dfc1c51e06130a3e44f82ab143ec06fb760`。
- 此變更沒有建立任何真實公開 trip 或 manifest、沒有呼叫 provider、沒有 deploy、沒有修改
  `trips/`。它不會回溯替換或移除既有 `gh-pages`；第一次安全發布仍需使用者審閱公開內容
  並另行要求外部動作。

### 2026-07-30 — Phase 4.5 product acceptance gate 完成

- 新增`phase45_acceptance.py`，只用temporary `TripStore`、canned
  Busan／Hokkaido itinerary fixtures與public confirmation APIs輸出繁中safe
  transcript；不讀寫`trips/`、不呼叫provider、不render或deploy。
- Busan實際走過直接指定住宿、未確認不寫入、一次host確認、canonical apply與
  receipt replay，並保留10:00 booked抵達及18:00 booked晚餐。
- Hokkaido實際走過兩段split stay及換宿日不同start/end anchors，並保留16:00
  booked ryokan check-in。既有對抗性tests繼續覆蓋同分不自選、evidence缺口、
  expired review、forged grant與lost ACK。
- 2個walkthrough tests加入離線suite；74個Phase 4.5專項、全套600個offline tests、
  三個real-trip validators及Python compile全過。29個trip files hash aggregate
  維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。

### 2026-07-30 — Phase 4.5D canonical lodging confirmation / apply 完成

- 新增`LodgingConfirmationRequest`、30分鐘safe review、externally signed host
  authority與one-pending-review stager。Direct manual choice與4.5C option projection
  共用同一條路徑；4.5C的assessment／option／comparison／snapshot／endpoint與
  schedule binding只以opaque digest進入typed patch，不把process-local candidate
  ID寫入durable state。
- 新增`SetLodgingSelection`，以一次operation替換contiguous lodging segments與每日
  start/end anchors。Codec要求每個住宿夜晚有exact end anchor、次日在行程內時有
  preceding-stay start anchor，並支援Hokkaido換宿日start A／end B。Generic day
  location update與lodging-shaped activity add/update均不能繞過typed operation。
- Canonical住宿只保存host配置、不可由原始位置推導的隨機opaque location ID、
  日期、kind、selected/fixed/booked decision與固定`evidence_state=unverified`。
  Raw label、地址、
  座標、價格、booking link、provider token及candidate ID不進request safe view、
  plan、receipt、history或error。
- `TripStore`在寫入與rollback前驗證host注入的external signature／registry
  verifier；未配置verifier預設拒絕，module-level builder或generic approval都不構成
  authority。Host的一次exact住宿確認會衍生同scope protected approval，避免重複詢問，
  但兩項store policy仍分別驗證。Grant完整綁trip、base revision、patch、lodging
  diff、review lifetime與issuer；receipt-first exact replay仍可安全恢復lost ACK。
- Busan驗收保留10:00 booked抵達與18:00 booked晚餐；Hokkaido驗收保留split stay及
  16:00 booked ryokan check-in。同分可由使用者明選但仍需grant；非confirmation
  evidence缺口會阻止4.5C projection。
- 9個4.5D專項、72個Phase 4.5專項與全套598個offline tests、三個real-trip
  validators及Python compile全過；29個trip files hash aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫provider、未render、未deploy、未修改`trips/`；下一個slice是4.6
  readiness / compliance preview。

### 2026-07-30 — Phase 4.5C joint lodging / itinerary recommendation 完成

- 新增純runtime `LodgingItineraryOption`／`LodgingItineraryAssessment` sidecar；
  每個option只能由同一`ComposedTripState`加宣告的住宿錨點建立，solver結果在比較前
  重新replay。未宣告day/location mutation、mixed snapshot、route slot語意不等價或
  偽造hours sidecar一律fail closed。
- 共同score採固定lexicographic vector，先守hard／required／protected／evidence，
  再比較換宿、已接受行程變動、slack、最長住宿leg與總travel。只有fresh identity、
  exact route receipt、current snapshot observation與所有solver required arcs完整
  時才可排序；unknown/stale/conflicted/missing值不轉成0。價格目前明確不評分。
- `schedule-problem/v3`把`ActivityAvailability`納入problem identity、solver、
  replay、preview與post-commit recomposition；evidence-bound hard hours refs必須出現
  在binding，post-commit hours drift使用current report並保留
  `EVIDENCE_REVISION_CHANGED`。
- Busan canned acceptance包含固定10:00抵達邊界、每日住宿錨點與18:00 booked晚餐；
  Hokkaido包含A→B split stay、180分鐘冬季跨城leg、45分鐘明示buffer與16:00 booked
  ryokan check-in。同分不選winner，reported booking claim仍等待4.5D。
- 8個4.5C專項、63個Phase 4.5 A/B/C專項與全套589個offline tests、三個real-trip
  validators及Python compile全過；29個trip files hash aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
  未呼叫provider、未render、未deploy、未修改`trips/`；下一個slice是4.5D
  canonical lodging confirmation / apply。

### 2026-07-29 — Phase 4.5B lodging evidence / comparison candidate 完成

- 新增runtime-only `LodgingComparisonCandidate` sidecar；它重申原始
  `LodgingCandidate`必須維持candidate + unverified + empty evidence refs，並以
  `basic_only`、`identity_bound`、`route_bound`、`blocked`表達目前能安全比較到哪一層，
  不產生最佳住宿、decision promotion或canonical mutation。
- Location ID只有經exact `EvidenceSnapshot` fresh identity resolution後才可成為
  Routes endpoint；provider place ID、地址、座標與概略區域仍須identity review。Safe
  view只含opaque IDs、state、coverage／known-field flags與attribution labels。
- Route evidence除了current snapshot resolution，還必須帶產生該observation的exact
  `GoogleRouteRequest` receipt，並核對原／現endpoint observation與value digest。
  missing、stale、conflicted、缺receipt或endpoint drift不輸出duration/distance，
  而是建立綁current snapshot的refresh request；相同semantic request會先去重，可直接
  交既有Routes batch/session gate。
- 新增process-local `normalize_serpapi_hotel_discovery()`，無HTTP或cache；query scope、
  dates、occupancy、currency/minor unit、region/language與trusted completion time完整
  綁定。Metadata error、top-level error、empty success、partial與invalid response
  分開，provider search ID/property只留HMAC ref，所有輸出仍為provider-discovered
  candidate + unverified。Result明確是non-provenance DTO；status與diagnostic ref不能
  進authorization、cache、evidence、receipt或decision；沒有`HOTEL_OFFER`
  promotion、availability或booking語意。
- 55個Phase 4.5專項與全套569個offline tests、三個real-trip validators、Python
  compile全過；29個trip files hash aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
- 未呼叫provider、未render、未deploy、未修改`trips/`；下一個slice是4.5C joint
  lodging / itinerary scoring與Busan／Hokkaido canned acceptance。

### 2026-07-29 — Phase 4.5A natural-language lodging intake 完成

- 新增provider-neutral `LocationHint`、exact／window transport draft、
  lodging intent draft、decision/evidence binding與逐夜assessment；自然語言之外
  沒有user-facing表單，unknown lodging不會生成假candidate。
- 所有4.5A binder只能建立candidate + unverified，module內也沒有promotion seam。
  selected／fixed／booked自然語言只保存為附opaque source ref的
  `ReportedDecisionClaim`，assessment回`awaiting_confirmation`；真正host-owned
  authority留到4.5D。
- Private label、address、coordinate、provider place ID、transport time與price不進
  repr／safe serialization／issue；process-secret keyed digest避免低熵位置被公開
  digest離線猜測。這些ID刻意不作cross-process replay；determinism只涵蓋同一intake
  session的結構結果、coverage與permutation invariance。
- `[check_in, check_out)`使用local date；逐夜判定missing options、undecided、
  split-stay coverage與overlap conflict。單次上限366晚、256個候選，避免極端日期
  或candidate fan-out耗盡資源。
- README、facts contract、repo skill與已安裝Codex skill已移除正常航班搜尋流程；
  hotel search只屬candidate discovery，住宿decision與evidence分軸，legacy Build
  不得把AI/provider candidate寫成booking。
- 22個4.5A專項與全套536個offline tests、三個real-trip validators、Python compile
  全過；29個trip files hash aggregate維持
  `8a3773ba04c97c199a522378341835fd1b775093700e48f55f66b4ce8213b514`。
- 未呼叫provider、未render、未deploy、未修改`trips/`；下一個slice是4.5B
  snapshot-bound lodging evidence與comparison-ready candidate。

### 2026-07-27 — Phase 0 exit gate 達成

- 建立 immutable `TripState`、typed constraints、read-only legacy loader、
  timezone-aware timeline simulator 與 structured `CheckReport`。
- 43 個 adversarial offline tests 通過，涵蓋跨午夜、DST、完整 time
  window、travel / buffer / return、跨日重疊、mode 與 evidence uncertainty。
- `scripts/check.sh` 已納入 kernel compile 與 tests；三個既有 local trip
  仍通過原 validator。
- 三個既有 trip 均可唯讀載入並誠實回報 `needs_verification`、0 errors；
  驗證前後 29 個 trip files 內容 hash 完全相同。
- 獨立 adversarial review 的最終結論為無剩餘 P0 / P1；本階段未呼叫
  provider API、未部署，也未修改 `trips/`。
- 下一個 implementation slice 是 Phase 1 的 stable ID、schema version、
  `PlanPatch`、revision、idempotency、dry-run、atomic apply 與 rollback。
- 開始建立 models、legacy loader、timeline simulator、`CheckReport` 與 adversarial tests。
- 保留 `.envrc.example` 的既有使用者變更；本階段不呼叫付費 API、不 render、不 deploy。
