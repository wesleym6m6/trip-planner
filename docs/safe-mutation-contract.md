# Safe Mutation Contract

狀態：Phase 1 contract
適用 schema：`trip-planner.plan/v1`
適用 patch：`plan-patch/v1`

這份文件定義 AI、deterministic kernel 與本機檔案之間的寫入邊界。核心原則是：

> AI 只能提出具名的 semantic operation；只有 store 能在驗證、授權與
> compare-and-swap 全部通過後改寫 canonical plan。

## Canonical document

每趟已遷移旅行只有一個 authoritative planning document：

```json
{
  "schema_version": "trip-planner.plan/v1",
  "trip_id": "trip-...",
  "generation": 1,
  "revision": "<sha256>",
  "state": {
    "trip": {},
    "itinerary": {}
  },
  "receipts": {}
}
```

- `state` 保留舊 `trip.json`、`itinerary.json` 的未知欄位，不做有損轉換。
- day、activity、location、constraint 與 travel endpoint 使用 persisted stable ID。
- `revision` hash 包含 schema、trip identity、generation 與完整 semantic state。
- `receipts` 不屬於 semantic revision；它記錄已完成 request，供安全重試。
- `generation` 每次有效 commit 單調遞增，因此 rollback 回到相同 semantic state
  仍會得到不同 revision，避免 ABA。

Legacy JSON 在 migration 後只作 compatibility source / recovery baseline，不再是
authoritative writer target。Canonical readers 不依賴 legacy files 繼續存在。

## Mutation boundary

`PlanPatch` 必須包含：

- exact `trip_id`；
- exact `base_revision`；
- caller-stable `idempotency_key`；
- 一組以 stable ID 定位的 typed operations；
- 可選、只供人讀的 intent。

Operations 不接受 array index 或 JSON path 作 identity。支援的最小集合是：

- add / update / place / remove activity；
- update day；
- add / update / remove constraint；
- `SetLodgingSelection`：一次替換canonical lodging segments及其day
  start/end anchors，並綁一個safe selection-manifest digest。

Mutation engine 是純函式：不讀檔、不寫檔、不呼叫 provider，也不增加 generation。
它只產生 detached `PatchDraft`、machine-readable problems、net changes、travel
invalidation 與精確 approval scope。

## Commit protocol

`TripStore.apply_patch()` 的順序不可交換：

1. 驗證 request bounds 與 digest。
2. 取得該 trip 的 exclusive lock。
3. 重新讀取並嚴格 decode canonical plan。
4. **先查 receipt**：同 key、同 digest 回 replay；同 key、不同 digest 拒絕。
5. 驗證 `trip_id` 與 `base_revision`。
6. 在 detached copy 套用完整 patch；任何 operation 失敗則整包拒絕。
7. 若patch改動canonical lodging，先要求exact `LodgingConfirmationGrant`通過
   `TripStore`注入的external host verifier；未配置verifier預設拒絕。Preview可顯示
   candidate與required scope，但不構成寫入授權。
8. 對 fixed、booked、migration-unclassified activity 或既有 hard constraint
   的破壞性 net change 驗證外部 `ApprovalGrant`。AI-authored patch 本身不能
   攜帶或升級 approval。
9. 清除因 net topology / time / location 變動而失效的 derived travel。
10. 用 planning kernel 檢查 candidate：
   - `infeasible`：拒絕；
   - `needs_verification`：可保存，但不能宣稱 travel-ready；
   - `feasible`：可保存。
11. 增加 generation、計算 revision，將 receipt 與 semantic state 放入同一份
    candidate bytes。
12. fsync 同目錄 temporary file。
13. fsync exact before-snapshot 至 hidden history。
14. `os.replace()` 原子替換 `plan.json`，再 fsync data directory。

Dry-run 執行相同 semantic validation，但不鎖定、不建立 receipt/history、不寫檔。
No-op 不增加 generation，也不建立 transaction。

## Approval contract

Approval scope hash 綁定：

- trip ID；
- base revision；
- 完整 patch digest；
- protected net diff。

因此 approval 不能跨 revision、跨旅行、跨 patch 或跨不同 protected change 重用。
`evidence_state` 完全不屬於 AI mutation 權限：existing activity 不接受直接升級、
降級或覆寫；new activity 只能省略或初始化成 `unverified`。若 AI 改變已驗證的
duration、opening window 或 location identity，mutation engine 會用 derived
change 將 evidence 降為 `unverified`，避免新值沿用舊 verification。只有後續
trusted provider/evidence boundary 可以再次標成 verified。

既有 hard constraint 的更新、弱化或刪除同樣進入 exact approval scope，避免 AI
藉由刪規則讓 infeasible plan 看似恢復正常。

## Lodging confirmation contract

`SetLodgingSelection`是唯一可改`state.trip.lodgings`與day lodging reference的
operation。Generic `UpdateDay`不能改住宿anchor；lodging／hotel／Airbnb等
case-insensitive activity alias也不能建立第二條canonical lodging路徑。

Confirmation分成兩個互不取代的層次：

- `LodgingConfirmationReview`綁trip、base revision、exact patch、canonical state
  digest、day anchors、selection-manifest digest與30分鐘expiry；
- `LodgingConfirmationGrant`是externally signed envelope。Signing key或host-side
  opaque grant registry必須位於AI可執行process之外；`TripStore`只持有／呼叫
  verifier。Module-level builder只建立待驗證資料，不能產生authority；
- verifier必須驗issuer、signature、`confirmed_at <= now <= expires_at`、revocation
  與需要的one-time policy。沒有verifier、錯issuer／signature或verifier exception
  一律`UNTRUSTED_LODGING_CONFIRMATION`；
- grant及scope完整綁trip ID、base revision、patch digest與exact lodging net diff，
  因此不能跨trip、revision、option、anchor或idempotency request重用；
- 同一host lodging confirmation可在stager內衍生同effect的普通protected approval，
  避免要求使用者重複確認；store仍分別驗證lodging與generic兩道policy。Generic
  `ApprovalGrant`單獨永遠不足。

Canonical lodging只允許host配置、不可由原始位置推導的隨機opaque location ID、
住宿日期、kind、selected／fixed／booked decision與固定
`evidence_state=unverified`。已選或已訂是
human decision，不代表location／route／price／availability已驗證。Raw label、地址、
座標、價格、booking link、provider token與process-local candidate ID不得進patch
safe view、plan、receipt、history或error；4.5C來源只以opaque selection binding
digest進patch。

需要 freshness 判斷時，preview、apply 與 rollback 共用 caller 提供的 timezone-aware
`evaluation_at`；store 會正規化成 UTC、傳給 kernel，並記入成功 receipt。Naive
datetime 一律拒絕，避免 snapshot 與 commit 使用不同時間語境。

## Fault and retry semantics

| 結果 | Canonical state | Caller action |
|---|---|---|
| `rejected` | 未改變 | 修正 proposal / approval / base revision |
| `write_failed` | 未替換 | 可以用同一 idempotency key 重試 |
| `commit_outcome_unknown` | 可能已替換 | 必須用同一 key 重試；receipt 會回覆真實結果 |
| `applied` | 已替換 | 保存 transaction ID |
| `replayed` | 先前已完成 | 視為同一 request 的成功回覆 |
| `replayed_rolled_back` | 原 transaction 已被 rollback | 不得重新套用舊 patch |

History 在 replace 前寫入；replace 後發生的例外不能靠猜測回滾。Receipt-first replay
是唯一恢復流程。

## Rollback contract

Rollback 是新的 transaction，不是覆寫舊 revision：

- 只接受 store 產生的 transaction ID；
- caller 必須提供目前 exact base revision 與新的 idempotency key；
- 目前只允許 rollback 最新且仍為 `applied` 的 patch；
- before-snapshot hash 必須和原 receipt 相符；
- 恢復 snapshot 的 semantic `state`，保留所有 current receipts；
- 原 patch receipt 改成 `rolled_back`，避免舊 request replay 後復活；
- rollback 自己增加 generation、取得新 revision 與新 receipt；
- 若 rollback 會改動 protected state，仍需 exact external approval。
- 若 rollback 會改動canonical lodging，還需要針對rollback request與反向exact
  lodging diff重新簽發的host grant；原apply grant不能重用。

## Migration contract

Migration 永遠分兩步：

1. `preview_legacy_migration()`：
   - strict-read `trip.json` 與 `itinerary.json`；
   - deterministic stable-ID assignment；
   - lossless candidate 與 typed issues；
   - zero write。
2. `TripStore.commit_migration(preview)`：
   - 在 lock 內重讀 legacy bytes；
   - source revision 與 deterministic preview 必須相同；
   - 若 canonical 已存在，只有 exact candidate replay 可以成功；
   - kernel 若判定 `infeasible` 則拒絕；
   - 一次 atomic replace 建立 `plan.json`。

所有 legacy activity 初次 migration 都標成 protected/unclassified，避免把舊資料缺少
decision/flexibility 欄位誤解為「AI 可以自由移動」。

## Filesystem and trust boundary

- `trips_root` 是 caller 指定的 trusted root，trip 只能用嚴格 slug 選擇。
- trip/data/plan/lock/history target 不接受 symlink 或非 regular file。
- Canonical file malformed、future schema、duplicate key、non-finite number、broken
  reference 或 revision mismatch 一律 fail closed，不 fallback 至 legacy。
- Filesystem本身仍是本機correctness boundary，不防同帳號操作者直接改檔。住宿
  human-confirmation另外支援external signature／host registry verifier，使AI tool
  contract不能只靠import builder取得寫入authority；signer不可與AI process共置。
  本階段仍不加入database、journal或event sourcing。

## Phase 1 exit evidence

離線測試至少要覆蓋：

- deterministic / lossless migration preview；
- stale revision 與 stale preview；
- idempotent replay、key collision、concurrent same-base writers；
- multi-operation all-or-nothing；
- protected approval exact binding；
- infeasible rejection 與 needs-verification acceptance；
- no-op；
- pre/post-replace fault injection；
- rollback integrity、generation/ABA 與 old-receipt replay；
- malformed canonical / symlink fail-closed；
- 現有 local trips 保持 byte-for-byte 不變。
