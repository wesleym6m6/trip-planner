# Phase 3 Scheduling Contract

狀態：Phase 3A、Phase 3B、Phase 3C、Phase 4.1B offline evidence wiring 與
Phase 4.5C activity-availability replay complete；真實 provider exit gate仍需授權
Contract version：`schedule-problem/v3`、`schedule-candidate/v1`
Production solver：`bounded-deterministic-best-first/v2`；只可經 staging gate commit

## 目的

Phase 3 把「有哪些活動」轉成一份完整、可驗證、可比較且可安全 staged
的時間表。Scheduler 是純函式候選產生器，不是資料來源、規則裁決者或
canonical writer。

權責固定為：

- AI 理解自然語言、整理候選與說明主觀取捨。
- scheduler 產生日、順序與開始時間的候選。
- planning kernel 以完整 travel、buffer、wait、service、window 與返程
  裁決候選。
- mutation/store 層負責 revision、approval、preview、atomic commit 與
  recovery。

任何 solver 自稱的 feasible、score 或 diff 都不具權威性；host 必須以同一
input 重新 materialize、跑 kernel 並重算 score。

## V1 範圍

Scheduler 可以：

- 排列已存在且在 scope 內的 activity；
- 將明確列入候選池的 `candidate` 升為 `selected`；
- 在既有 day bounds、base locations、allowed modes 與 constraints 下選日、
  順序與開始時間；
- 產生 stable-ID assignment、可讀 score breakdown 與 semantic patch draft；
- 進行明確邊界內的 partial replan。

Scheduler 不可以：

- 新增、刪除或猜測 activity；
- 修改 day metadata 或 constraint；
- 猜 duration、travel、opening hours、cost 或 evidence；
- 呼叫 provider、讀寫檔案、commit、render 或 deploy；
- 自行擴張 replan scope；
- 把 missing travel 或 duration 當成 0；
- 建立另一套 constraint DSL。

V1 candidate-to-patch 只允許：

- `UpdateActivity(fields={"decision_state": "selected"})`；
- `PlaceActivity`。

其他語意變更留在 Phase 2 repair 或 human checkpoint path。

## Input contract

`ScheduleProblem` 至少包含：

```text
contract_version = "schedule-problem/v3"
problem_id
trip_id                    canonical top-level plan identity, not slug
base_revision
base_state_digest             exact composed runtime state
canonical_state_digest        exact durable plan state
evidence_binding              optional redacted EvidenceBinding
activity_availability         runtime-only exact ActivityAvailability tuple
evaluation_at              aware datetime
state                      immutable TripState
scope                      ReplanScope
preferences                SchedulePreferences
limits                     SearchLimits
```

`problem_id` 由完整 semantic input 計算，不能由 solver 自填。Hash material
包含 canonical `trip_id`、composed state、canonical state digest、stable evidence
binding、activity availability、固定 `evaluation_at`、scope、preferences、limits
與 contract version，但不含 solver 名稱或 wall clock。`schedule-problem/v2` 曾加入
canonical / evidence 雙重身分；v3再把opening-hours sidecar納入replay identity。
`ScheduleProblem`沒有codec或durable store path，刻意是process-local contract；
舊v1/v2 problem一律`UNSUPPORTED_VERSION`，不能冒充或自動migration成v3。

Canonical caller 必須用 `schedule_problem_from_plan()` 從嚴格驗證後的
top-level `plan.trip_id` 與 `plan_to_trip_state()` 建立 problem，不可從 slug
猜 identity。Digest encoding 保留 scalar type；例如 numeric `10.0` 與文字
`"10.0"` 必須有不同 state digest/problem ID，避免 replay 身分碰撞。

### ActivityAvailability

- `activity_availability`只接受exact、unique且指向已知activity的sidecar，並以
  `activity_id`穩定排序；
- `HARD_CURRENT`必須帶`fact:<sha256>` refs與freshness；evidence-bound problem還要求
  每個hard ref出現在`EvidenceBinding.used_observation_ids`。完整snapshot語意由
  `compose_trip_state()`／`project_activity_availability()`建立，4.5C joint scorer會
  再以exact snapshot重投影驗證；
- solver、candidate build、replay、preview與post-commit validation一律呼叫
  `evaluate_schedule_state(problem, state)`，不得繞過problem sidecar直接呼叫
  `evaluate_timeline()`；
- post-commit若current hours改變，report使用重新compose後的current sidecar，
  同時回`EVIDENCE_REVISION_CHANGED`並撤銷`candidate_is_current`；已知完成的canonical
  write不會被誤報成未套用；
- 無evidence binding的手工problem可承載caller-owned runtime constraint，但不構成
  provider provenance或canonical authority。需要evidence-backed決策時必須從
  `ComposedTripState`建立。

### ReplanScope

```text
day_ids
mutable_activity_ids
eligible_candidate_ids
max_accepted_changes
```

規則：

- scope 外的 placement、time 與 decision state 必須不變；
- 非 `mutable_activity_ids` 一律 frozen；
- eligible candidate 必須是 scope 內、已存在且 decision 為 `candidate`；
- `fixed`、`booked` 與 `fixed_time` 永遠 frozen；
- `fixed_day` 的既有位置與 persisted time 在 V1 auto path 視為 protected；
  eligible candidate 只可原地升為 `selected`，重排須走 human approval；
- 既有 `selected` 不得被 scheduler 捨棄或降級；
- 超過 change budget 或 scope 太窄時回 typed failure，不自行擴權。

### SchedulePreferences

第一版只接受具體、可解釋的值：

```text
evidence_policy            verified_only
minimum_end_slack_min
max_activities_by_day
max_service_min_by_day
```

`allow_unverified_draft` 保留為後續 contract value，但 V1 會明確回
`UNSUPPORTED_EVIDENCE_POLICY`，不形成看似可用、實際仍 fail-closed 的假能力。
Hard daily capacity 仍以 day availability 與 `DAILY_LIMIT` constraint 為準。
Pace 的第一版由 activity count、service time、travel、buffer、wait 與 day-end
slack 表達。沒有完整、typed energy 資料前不從 activity kind 猜體力；只有
真實釜山或北海道驗收證明需要時，才新增單一 request overlay。

### SearchLimits

```text
max_candidates
max_evaluations
```

V1 `max_candidates` 必須為 1。主要 bound 是完整 assignment variant 的
evaluation count：一個 unit 內含 materialize、provisional kernel simulation、
canonical assignment normalization 與 final kernel replay；cache hit 不計數。
Candidate 建立時的最後 trusted replay 不屬於 search exploration。
Wall-clock deadline 只可作 process safety fuse；若觸發，不可採用被中斷的
candidate。

## Candidate contract

`ScheduleAssignment`：

```text
activity_id
day_id
order
scheduled_start             local time or null
```

`ScheduleCandidate`：

```text
contract_version = "schedule-candidate/v1"
candidate_id
problem_id
base_revision
base_state_digest
assignments                 complete active schedule
promoted_activity_ids
required_arc_keys
report                      kernel CheckReport
score                       common ScheduleScore
schedule_key
changed_activity_ids
```

Assignments 必須包含完整 active schedule，而不是只有 diff。Host 必須能據此
證明：

- 每個 active activity 恰出現一次；
- day/order 沒有重複；
- promoted IDs 原本確為 eligible candidates；
- frozen 與 scope 外資料完全不變；
- assignment 先正規化為 persisted local starts，再重新 materialize、跑 kernel
  與 score，且第二次正規化必須到達 fixed point；
- 未被 assignment／patch 直接移動的 stationary entities 相對順序不變；
- candidate 重新 materialize 後的 report、score、ID 與 solver 回傳一致。

`candidate_id` 只 hash `problem_id + assignments + promoted IDs`。兩個 solver
產生相同行程時，必須得到同一 candidate ID。

共同 builder 也會以 V1 projection 預演 exact `PlanPatch` operations。最多
128 operations；超過時回 typed `PATCH_OPERATION_LIMIT_EXCEEDED`，solver
不得回 `SOLVED`，也不得讓 `PlanPatch` constructor 的原生 exception 洩出。
零 operations 可作為 replayable「目前狀態已相同」candidate，但
candidate-to-patch 會明確回 `EMPTY_SCHEDULE_PATCH`，不製造假 mutation。

## Lexicographic objective

不使用一個難以解釋的加權總分。所有 solver 共用以下由小到大比較的 key：

```text
(
  hard_violation_count,
  missing_required_count,
  verification_risk_count,

  protected_change_count,
  accepted_activity_change_count,
  accepted_day_move_count,
  accepted_order_inversion_count,
  accepted_time_shift_deci_min,

  -served_priority_points,
  scheduled_optional_count,

  soft_constraint_violation_count,
  tight_slack_count,
  slack_deficit_deci_min,
  activity_count_overage,
  service_overage_deci_min,
  wait_deci_min,

  travel_deci_min,
  stable_schedule_key,
)
```

時間與距離先以 decimal 字串量化；objective 不直接比較 binary float。

定義：

- hard issue 優先於所有品質取捨；
- required coverage 包含既有 selected/fixed/booked 與 hard
  `MUST_INCLUDE`，group constraints 另依其精確 cardinality 計算；
- successful auto candidate 的 protected change 必須為 0；
- accepted stability 先比較 changed activity，再比較換日、既有 accepted
  相對順序與明確開始時間位移；
- accepted placement 比較會先從 base/final 同時移除本次 promoted IDs；
  候選插入不誤算 churn，但 accepted 相對 inactive/frozen anchor 的 raw move
  仍必須計入 change budget；
- 插入新 candidate 不算既有 accepted 彼此的 order inversion；
- priority 使用原始整數，不偷做 clamp 或 hidden weight；
- priority 相同時先選較少 optional，避免零優先候選只因 stable ID 被加入；
- buffer 是 elapsed time，不是可犧牲的 soft penalty；
- risk 包含 verification、tight slack、day density 與 wait；
- travel 只在更高層完全相同後比較。

Breakdown 另外公開：

- `travel_min`、`buffer_min`、`service_min`、`wait_min`；
- 每日 completion、return-to-base 後的 end slack 與 timing evidence；
- scheduled optional IDs；
- changed/protected IDs；
- required travel arcs。

## Evidence policy

`VERIFIED_ONLY`：

- missing duration、missing arc、stale、unverified 或 conflicted used evidence
  均不可回 `SOLVED`；
- 同一合法 mode/endpoints 有多筆 travel evidence 時，先選 verified 且在固定
  `evaluation_at` 仍 fresh 的集合，再比較 activity/day specificity、
  recommended 與 duration；
- 不得使用 haversine、mode threshold 或 0 分鐘 fallback。

`ALLOW_UNVERIFIED_DRAFT` 尚未啟用；輸入會回 `INVALID_INPUT` 與
`UNSUPPORTED_EVIDENCE_POLICY`。Phase 4 若加入 draft path，仍只能回
needs-evidence artifact、不得自動 commit。

## Result and failure semantics

Result status：

| Status | 意義 |
|---|---|
| `SOLVED` | candidate 已由共同 kernel 驗證；不宣稱全域 optimal |
| `NEEDS_EVIDENCE` | 缺少或不可信的 duration/travel/window evidence |
| `PROVEN_INFEASIBLE` | deterministic precheck 或 complete solver 已證明無解 |
| `SEARCH_EXHAUSTED` | evaluation budget 用完但未找到解，不能假稱 infeasible |
| `INVALID_INPUT` | contract、reference、scope 或 constraint 無效 |
| `ENGINE_ERROR` | solver exception、crash 或非法內部狀態 |

非 `SOLVED` 結果不得附一份可 commit 的殘缺 candidate。Failure 至少包含：

```text
kind
code
problem_id
message
activity_ids
day_ids
constraint_ids
kernel_issues
missing_arc_keys
evaluations_used
evaluation_limit
```

常見 code：

- `INVALID_REFERENCE`
- `INVALID_CONSTRAINT`
- `INVALID_SCOPE`
- `UNSUPPORTED_EVIDENCE_POLICY`
- `MISSING_DURATION`
- `MISSING_TRAVEL_ARC`
- `UNTRUSTED_EVIDENCE`
- `CONFLICTED_EVIDENCE`
- `FIXED_BASELINE_INFEASIBLE`
- `CAPACITY_INFEASIBLE`
- `TIME_WINDOWS_INFEASIBLE`
- `SCOPE_TOO_NARROW`
- `CHANGE_BUDGET_EXCEEDED`
- `PATCH_OPERATION_LIMIT_EXCEEDED`
- `UNEXPECTED_ENGINE_ERROR`
- `NO_SOLUTION_WITHIN_EVALUATION_LIMIT`
- `NO_SOLUTION_WITHIN_BUDGET`

Heuristic 「找不到」只能是 `SEARCH_EXHAUSTED`；不能自行升格為
`PROVEN_INFEASIBLE`。觸及 evaluation limit 時必須優先回
`NO_SOLUTION_WITHIN_EVALUATION_LIMIT`；issue 提及 frozen ID 本身不是
`SCOPE_TOO_NARROW` 的證明。

## Partial replan invariants

- scope 外 semantic state 完全不變；
- fixed/booked 的 day、raw position、time、decision 與 evidence 完全不變；
- fixed-day auto path 不改 day/raw position/time，只允許 eligible candidate
  原地 promotion；fixed-time 不換 day/time；
- selected 不移除、不降級；
- accepted changes 不超過 request budget；
- 先最小化是否變更，再比較換日、relative-order inversion 與 time shift；
- 新 candidate 的插入不算 accepted mutual-order change；
- 若 frozen activity 或 scope boundary 阻擋解，回 `SCOPE_TOO_NARROW` 與
  blocking IDs。

## Kernel integration gaps

共同 scorer 需要 kernel 提供 per-day summary：

```text
DayTimelineSummary
day_id
starts_at
completes_at                 includes return-to-base
available_end_at
end_slack_min
activity_count
service_min
travel_min
buffer_min
wait_min
timing_verified
```

Activity `slack_min` 只描述該 activity 對當前 window/day end 的餘裕，不能替代
包含後續活動與返程後的真正 day-end slack。

Clock shift 與 legacy delivery 均使用 planning-day rollover semantics：
`23:50 → 00:10` 在 22:00–02:00 的 day 是 20 分鐘，不是 23 小時 40 分；
ICS、Routes departure 與 opening-hours weekday 共用 ordered local datetime
resolver。Persisted local time 可保留 seconds/microseconds，不再假設只有
`HH:MM`。

另有兩個明確邊界：

- canonical `day.travel` 目前是 order-derived edge；reorder patch 會正確使它
  失效，因此 schedule commit 後可能進入 `WAITING_EXTERNAL`，不可偽稱
  travel-ready；
- transit estimate 尚無 departure-time bucket/cost。Phase 3 fixture 使用人工
  directed minutes 與 buffer，只驗證 solver semantics，不宣稱真實時刻交通。

跨城市火車、航班或渡輪在 V1 建模為普通 fixed-time transport activity；
不新增 interday solver abstraction。

## Staging seam

Scheduler candidate 不可偽裝成 Phase 2 `ProposalIntent`：

- Phase 2 operation 必須引用 current repair issue；
- 已 feasible、只是 soft score 更好的 schedule 可能沒有 issue；
- repair progress vector 不包含 priority、stability、slack 或 travel quality。

`ScheduleStager` 是一條獨立、in-memory、單一 pending review seam：

1. `expected_solver` 只作 supported-version compatibility gate，不是來源證明；
   production caller 只能傳入 host-owned runner 的輸出，真正的信任來自後續
   replay，不做 solver 簽章或 attestation；
2. reload strict canonical plan，用原 scope/preferences/limits 與固定
   `evaluation_at` 重建 problem；
3. exact 比對 canonical `trip_id`、revision、state digest 與 problem ID，
   禁止 implicit rebase；
4. trusted replay candidate，拒絕 candidate-owned subclass，並從 materialized
   final state 重跑 report 與 score；
5. trusted candidate `ScheduleScore.objective_key()` 必須嚴格小於 baseline。
   `schedule_key` 只作同分 deterministic tie-break，不構成品質改善；
6. 投影同一份 `PlanPatch`，以 host 的 pure mutation engine 驗證 preview
   action、trip/base/current identity、patch digest、完整 draft、完整 canonical
   candidate bytes 與 report；只比 placement/time 的局部 projection 不足以通過；
7. preview 完整 store diff；full change cap 包含 derived travel/evidence
   invalidation，不只 scheduler 的 accepted-change count；
8. diff 超過 auto threshold 需要 exact `HumanCheckpointGrant`；migration/fixed
   protected diff 另外需要 exact store `ApprovalGrant`，兩者互不替代；
9. commit 前 reload、trusted replay、strict improvement 與 re-preview 全部
   重做，changes、protected changes、affected/invalidated days、risk、approval
   scope 與完整 projection 必須和 review 完全一致；
10. `commit_outcome_unknown` 或 raw adapter exception 只能重試同一個
    `PlanPatch` object；write-attempt cap 綁在整個 pending review，跨多次
    `commit()` 呼叫合計最多兩次，不會每次呼叫重新計數；
11. repository 的 `applied` ACK 本身不構成成功。Commit 後必須再讀
    canonical plan，核對 exact receipt request digest、完整 expected semantic
    state digest、expected `generation + 1` revision 與 persisted schedule；
    只有完整 effect 相符才可宣稱 candidate current；
12. 以 canonical persisted state 重跑 report；只有可信 report 的
    `needs_verification` 才回 `WAITING_EXTERNAL`。Internal reconciliation
    mismatch 回 `OUTCOME_UNKNOWN`，不可誤導 agent 去重抓 provider。

Review 的 runtime object保留 baseline/candidate score供同一process比較，並
公開第一個 decisive objective、完整 `ChangeRecord`、獨立的
`protected_changes`、affected/invalidated days、risk 與 approval scope。
Evidence-bound review 的安全 `repr()` / `to_dict()`只輸出score digest與
redacted marker，不輸出provider-derived route totals；commit report同樣只
序列化status。`PatchDraft.protected_changes` 是 approval-policy
正規化後的 semantic net records，不要求與 audit `changes` 的 op ID/kind
物件相等。

Receipt observation 發生在新寫入的 grant gate 之前：已由其他 actor 完成的
exact patch 可以直接確認，不要求呼叫者重新提供舊 grant。`replayed_rolled_back`
永遠不是成功。若 receipt 證明曾套用但 current revision 已前進，只回
`REPLAY_CONFIRMED`，不宣稱 candidate 仍是目前狀態。

三個 change limit 的單位不同：

- `ReplanScope.max_accepted_changes`：單一 candidate 內既有 accepted activity
  發生 semantic change 的 ID 數；
- `ScheduleStager.max_changes`：單一 review 的完整 persistent diff hard cap；
- `ScheduleStager.max_auto_changes`：單一 review 超過後需要 human checkpoint
  的 persistent diff threshold。

`40/12` 是等待釜山與北海道真實驗收校準的 provisional defaults，不是
run-level monotonic budget。跨多次 review 的 remaining budget 留給 Phase 5
orchestrator。

### Staging state truth table

| State | `applied` | `candidate_is_current` | 意義 |
|---|---:|---:|---|
| `READY` | N/A | N/A | review 可 commit；不表示 travel-ready |
| `WAITING_APPROVAL` | false | false | exact review 保留，缺 human/store grant |
| `APPLIED` | true | true | canonical receipt、current revision、schedule 與 feasible report 全部相符 |
| `WAITING_EXTERNAL` | true | true/false | canonical schedule 已套用；可能是目前狀態仍需驗證，或 post-commit evidence refresh 已使舊 candidate失效 |
| `REPLAY_CONFIRMED` | true | false | exact receipt 證明歷史套用，canonical state 已前進 |
| `OUTCOME_UNKNOWN` | false/true | false | 尚未確認 durable write，或已確認 write 但 semantic reconciliation 失敗 |
| `REJECTED` | false | false | 確定不能套用；不得攜帶 durable confirmation |

目前 pending retry policy：

- missing grant、read/preview exception，或未用完 write-attempt cap 時 raw commit
  exception／ACK 後 receipt 暫時不可觀察：保留 exact pending review；
- stale review、preview drift/contract mismatch、hard diff cap、明確 CAS reject、
  rollback：清除 pending，必須重新 stage；
- 同一 review 累計兩次 write attempt 仍無法確認（包含 fake ACK、exception 或
  typed unknown）：停止 retry 並清除 pending；
- receipt 已確認 write、但 canonical semantic reconciliation 失敗：回
  `applied=true + OUTCOME_UNKNOWN` 並清除 pending；
- receipt 與 canonical effect 已確認、但 post-commit evidence changed /
  unreadable（包含新 evidence 令 composed report infeasible）：回
  `applied=true + WAITING_EXTERNAL`，明確保留
  `EVIDENCE_REVISION_CHANGED` / `EVIDENCE_READ_FAILED`，不把已知 durable
  outcome 誤稱 unknown。

Serialized `retryable`／`pending_review_retained`／`next_action` envelope 留在
Phase 5 `tripctl`，本階段不抽通用 workflow engine。Commit result 的
`required_arc_keys` 只保留本次 invalidated days 所需的 arcs，避免重抓未受
影響日期。

Schedule reason 不偽造 Phase 2 issue ownership，也不放寬
`OperationReason.issue_ids` 非空的 invariant。V1 不建立 durable review DB、
queue 或通用 workflow engine。

## Offline benchmark

第一批 benchmark 固定為 6 個 family：

1. `core_priority_choice`：must-do、optional priority、capacity、stable tie。
2. `core_fixed_window`：full-duration window 與 booked/fixed preservation。
3. `core_buffer_return_capacity`：travel、buffer、return anchor 與 daily slack。
4. `busan_transit_anchors`：抽象兩日 transit、每日住宿 anchor、partial replan。
5. `hokkaido_winter_transfer`：抽象冬季 buffer、跨城市、fixed reservation。
6. `core_missing_travel`：unknown-as-zero fail-safe。

Phase 3C 另固定 solver-selection gates：

- `core_two_move_plateau`：必須跨過暫時不改善的 relocation state；
- `core_candidate_replacement`：不能因先看到單體高分候選而漏掉較佳組合；
- `core_directed_roundtrip`：比較完整 directed outbound/return，不受單向短程誤導；
- `core_time_clear_subsets`：在 change budget 內保留 anchor，支援非連續及跨日
  explicit-time clear；
- `core_requires_scaling`：hard `REQUIRES` 使用 lazy transitive closure，不建立
  Cartesian placement product；
- 兩活動／兩天的 162-case complete enumeration corpus，涵蓋 ordered
  partitions、precedence 與 allowed-day 組合。

所有 fixture：

- 使用 typed Python builders，不建立 fixture DSL；
- 固定 evaluation time；
- 使用人工 minutes，不含真實座標、班次、票價或 provider snapshot；
- 不讀寫 `trips/`、不呼叫 provider；
- 成功結果必須再送入 `evaluate_timeline()`；
- 重跑及輸入 container permutation 後 canonical candidate bytes 必須相同。

## Solver selection gate

Phase 3A 先實作 bounded deterministic insertion/relocate/swap。現有
`scripts/plan_route.py` 只作 legacy reference；它只最佳化 haversine distance，
且缺少 kernel invariants，不能成為 production baseline。

Phase 3C 的 adversarial oracle 證明原本只接受立即改善的 hill climb 會漏掉
multi-step plateau 與 candidate replacement。Production 因此升為
`bounded-deterministic-best-first/v2`：

- 保留 non-improving frontier，但每個 distinct layout 最多評估一次；
- hard coverage candidate 先作 in-place promotion，再列 placement variants；
- hard `REQUIRES` 以 lazy adjacency traversal 產生任意大小的完整 closure，
  closure 先原地 promotion，絕不展開 dependency Cartesian placement product；
- frozen、scope 外與 `fixed_day` 的 baseline raw day/index 在 evaluation 前即
  fail-closed，避免把不可 stage neighbor 誤報為 engine failure；
- time repair 只接受 audited time-related errors；blocking 為空時不產生 clear
  variant，每個 layout 最多 64 個 deterministic clear subsets；
- prefix、suffix、非連續及跨日 clear choice 共用相同 assignment-variant budget；
- limit 觸及時優先回 `NO_SOLUTION_WITHIN_EVALUATION_LIMIT`，不把 tentative
  protected/change/stageability diagnosis 偽裝成完整證明；
- internal materialization/replay invariant failure 保留 typed `ENGINE_ERROR`，
  不偽裝成 search exhaustion。

所有 layout 仍經共同 materializer/kernel/scorer/projection；不可 stage 的
frozen／fixed-day raw-position neighbor 會在 trusted evaluation 前排除。找到可用
candidate 也只回 `optimality=not_claimed`。目前 Busan/Hokkaido composite
分別完整評估 238／35 個 stageable reachable layouts，objective key 與 schedule
key 均維持 golden；tiny structural oracles 也精確通過。

已知且接受的 bounded-search 限制：time-clear variants 先測 singleton，再測
compound subsets；大量 blockers 可能在 64-variant cap 內較晚看到必要組合。
此情況只能保守回 `SEARCH_EXHAUSTED`，不得回 false feasible 或
`PROVEN_INFEASIBLE`。若真實 fixture 在固定 budget 下重現，應以該 fixture
比較 clear-set ordering，而不是預先擴大無界搜尋。

Phase 3C 完成 OR-Tools pre-adoption gate，但沒有安裝或加入 dependency。修正
上述通用搜尋缺口後，固定 challenge corpus 已沒有 OR-Tools 可量測的 failing
target；此時加入 compiled optimizer 只會增加安裝、platform、timeout 與第二套
model semantics 成本。日後只有真實行程或新 adversarial fixture 在固定 budget
下出現 `SEARCH_EXHAUSTED` 或 exact objective regret，才重啟隔離 CP-SAT spike；
仍不先加入預設 `requirements.txt`。

依序淘汰：

1. 任一 false-feasible、must-do 遺漏、protected mutation 或 unknown-as-zero；
2. 任一 fixture 未達共同 golden lexicographic outcome；
3. deterministic replay 不是 100%；
4. partial-replan churn 較差；
5. installation、runtime、timeout 或 platform cost 不可接受。

若兩者 correctness 與 quality 同分，選較小的 deterministic heuristic。
只有 OR-Tools 在相同 budget 下明顯提高必要 fixture solved rate，或在更早的
lexicographic 層產生實質改善且無 regression，才值得承擔新 compiled
dependency。正式產品只保留一個 production solver，不做 runtime fallback。
