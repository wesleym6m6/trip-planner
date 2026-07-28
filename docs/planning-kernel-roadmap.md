# Trip Planner 成熟化 Roadmap

狀態：進行中
啟動日期：2026-07-27
目前 checkpoint：Phase 3A 排程核心、Phase 3B safe staging、Phase 3C
solver decision gate、Phase 4.0 facts/policy foundation、Phase 4.1A
trusted-clock EvidenceStore 與 Phase 4.1B offline composition / evidence
revision wiring、Phase 4.2 minimal Places identity 已完成；下一個 bounded
slice 是 Phase 4.3 Routes end-to-end

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

目前進度（2026-07-28）：

- `schedule-problem/v2`、`schedule-candidate/v1`、完整 assignment replay、
  per-day summary、lexicographic scorer 與 typed failures 已落地；
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
- Places identity優先，再接Routes、Places profile/hours、flight、hotel；
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

目前進度（2026-07-28）：

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
  可進EvidenceStore。下一個slice不跨越Phase 4.3 Routes end-to-end邊界。

### Phase 5 — Product Interface and Skill

範圍：

- 單一 `tripctl` CLI；
- consistent JSON envelope；
- inspect / propose / score / validate / apply；
- 重寫 trip-planner skill，讓 agent 使用 kernel，而不是把 prompt 當規則引擎；
- draft / review / travel-ready readiness profiles；
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

- 建立四種V1 normalized fact schema與flight/hotel fail-closed reservation；
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
  exact policy、minimal field mask、`pageSize=5`、match scope與既有LKG basis
  共同綁入request fingerprint，不呼叫HTTP或付費provider。
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
