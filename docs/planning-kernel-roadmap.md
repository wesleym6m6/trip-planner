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
中運作。Phase 5.5再把明確方向偏好安全交給下一輪private refinement；三者都沒有CLI、
parser、provider、render或mutation。Phase 6.0 fail-closed public release boundary亦已完成；
真實provider驗收仍只在明確授權範圍內進行，完整canonical CLI/interface仍待後續切片。

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
