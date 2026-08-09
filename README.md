# Trip Planner

用 Claude Code + Google Maps API 規劃旅行。完整行程可在本機產生私有預覽；若要
發布到 GitHub Pages，則只會發布經明確審閱的精簡公開摘要。

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

- **自然語言互動式規劃** — AI 先把已知需求形成私有、無副作用草稿，不需手寫 JSON，再提出可比較的行程
- **真實資料驅動** — Google Places API 營業時間 + Routes API 交通時間
- **私有本機預覽** — 行程表、地圖、行事曆下載、訂位清單、行李清單
- **受控公開發布** — 明確審閱後才發布精簡行程摘要到 GitHub Pages

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

### 公開發布護欄

`trips/{slug}/data/` 是 private/local-only。`render_trip.py` 與 `build_index.py` 仍可用於
完整的**本機私有預覽**，但公開 builder 永遠不讀取它們，也不會從既有行程自動搬資料。

要公開某趟旅行時，先由使用者明確說明哪些摘要可以公開；agent 才建立最小的
`public/trips/{slug}.json`。使用者不需要手寫 JSON，也不應把訂位、待辦、行李、地址、
座標、地圖連結、provider cache 或 ICS 塞進公開摘要。接著：

1. `python3 scripts/prepare_public_release.py {slug} [...]` 只印出完整候選
   `release.json`（包含來源與每個 HTML、首頁的 digest），不寫入任何檔案。
2. 審閱公開內容與候選 manifest 後，才建立精確的 `public/release.json`。
3. 使用者明確要求發布時才執行 `bash scripts/deploy.sh`。沒有 manifest、來源／模板／
   HTML digest 漂移或不安全檔案時，腳本會在 render、git 或網路動作前拒絕。

公開輸出只有首頁、每趟公開摘要頁與 artifact manifest；不含地圖、ICS、訂位、待辦、
行李、地址、座標、外部連結或 provider cache。digest 只能綁定已審閱的內容，不能判斷
文字本身是否適合公開，因此公開內容仍需要使用者確認。這個新邊界不會追溯清除目前
已部署的 Pages；第一次安全替換或下架仍是另一個明確的發布操作。

## 使用

在 Claude Code 裡輸入 `/trip-planner` 開始規劃，或直接描述你的旅行需求。

### 資料模式護欄

目前完整、可直接給使用者與 agent 操作的流程是 **legacy 模式**（七個核心檔案：
`trip.json`、`itinerary.json`、`reservations.json`、`todo.json`、`info.json`、
`packing.json`、`places_cache.json`）。`plan.json` canonical kernel 是 developer
preview：它以 `plan.json` 加五個 sidecar（後五個檔案）取代前兩個檔案；現有
renderer、validator 與部分讀取工具可相容讀取，但 legacy writer 會刻意拒絕修改
已 migration 的 trip。不要直接編輯 canonical JSON；只能經已支援的 `TripStore` /
`PlanPatch` 路徑操作。目前 Phase 5 的 `tripctl inspect` 與 `tripctl validate` 是
storage-dispatch、唯讀入口：既有 legacy 行為不變，安全且有效的 canonical `plan.json`
可輸出 aggregate inspection 與 deterministic timeline review。它們不載入 runtime
evidence、不能宣稱 `travel_ready`。Canonical-only `tripctl propose`／`score`另提供
provisional deterministic schedule 與 trusted replay score，但沒有 apply authority；私有
developer host可用exact process-local evidence把有實際canonical變更的score轉成30分鐘有效的
typed apply review，但該review本身仍沒有apply authority，也不擷取回覆或寫入trip。私有導引草稿
不是 `tripctl` CLI，也不建立或修改 trip。完整 canonical workflow 尚未提供。除非
已明確接受 developer workflow，否則不要 migration 真實 trip。

Phase 5已在M0重新收斂：Phase 5.13–5.29保留為`Provider Execution Safety Reference v1`，
不再讓每個internal runtime gate各占一個roadmap phase；剩餘產品交付固定為5.30 composed
pre-execution、5.31 bounded provider execution、5.32 evidence-to-canonical與5.33 unified
`tripctl`／skill驗收。5.33通過後Phase 5即凍結，沒有默認的5.34。完整定義與測試節奏見
[`docs/planning-kernel-roadmap.md`](docs/planning-kernel-roadmap.md)。

Phase 4.4 的 Places profile / opening-hours runtime目前也是developer
boundary：核心只有injected transport，沒有內建credential或live CLI。
只有fresh、無衝突的`currentOpeningHours`可限制完整活動時段；
`regularOpeningHours`、stale、conflicted或missing資料只會要求重新確認。
舊`check_hours.py`使用regular cache，因此永遠只輸出advisory，不代表當日
確定營業。

Phase 4.5 的產品方向是「固定／暫定交通 + 住宿錨點 + 每日行程共同最佳化」。使用者
只要自然描述已知資訊，不需要填規格表；AI 會把明確內容寫入 process-local typed
draft，沒有提到的內容保持 unknown。機票、渡輪、鐵路等只接受使用者輸入的抵達／
離開時間或時間範圍與地點，不提供航班搜尋。住宿可以是飯店、民宿、Airbnb、地址、
座標或概略區域；沒有住宿資料時，AI 只能追問或提出候選，不能捏造住宿。

Phase 4.5A 的公開 binding 一律是 candidate + unverified。使用者若清楚說「選這間」
或「已訂」，AI 會保留成附 opaque source reference 的 reported decision claim，
不會遺失語意，也不會把 claim 冒充成權威 selected / fixed / booked。

Phase 5.3 的 `TripBriefDraft` 是進入新旅行的私有、process-local 導引草稿：agent 從
自然語言只抽取明確說過的需求，未知保持 unknown；不寫入 `trips/`、不要求使用者處理
JSON、不呼叫 API、不 render 或 deploy。初始只在缺目的地、或缺足以排每日的 exact 日期
時各問一題；其他偏好與住宿可留到真的需要決策時。使用者所稱「固定」只保留為其陳述，
仍不構成 verified evidence 或權威 booking／decision。

Phase 5.4 在草稿 ready 後只建立一至三張私有候選方向卡，讓使用者在真正需要時選擇
「某一方向／混合／交給我調整」。每一 line 是相對的構想順序，不會映射成日期或時間表；
卡片的自由文字也不會被當作已驗證的交通、營業、空位或價格。Host 必須逐行標示
`user_stated`、`tentative` 或 `ai_candidate`，並顯示「候選方向尚未確認營業、交通、空位或價格。」
再提出這唯一的主觀問題。這仍不會選定、套用、建立行程、呼叫 API、render 或 deploy。
若任一方向未涵蓋使用者明確必去項目，Agent 會先在私有層補強，不能顯示卡片或把補強工作
交給使用者。

Phase 5.5 把使用者的明確回覆接回下一輪私有細化：選一張是 `prefer_one`、混合是 `mix`、
「交給我調整」是 `request_refinement`。它只接受目前已完成審閱的卡片組；不清楚的回覆就
維持原本的主觀問題，不用脆弱 parser 猜答案。卡片重新生成後必須重新展示、重新收集偏好。
擷取時會私有地綁定 exact brief 與 card contents，換卡或需求變動會拒絕舊回覆；這只是
staleness guard，不是授權。這份偏好仍只是 `candidate + unverified` 的工作方向，不能建立
trip、選定景點、套用變更或取代既有的受控 canonical mutation／確認流程。

Phase 5.6 用 `GuidedRefinementCandidate` 將上述 exact brief、cards 與 preference 整理成
一張新的私有方向卡。`prefer_one` 必須保留所選卡的全部 line；`mix` 必須保留每張所選卡
至少一條真正的 candidate direction line（若該卡沒有 AI line，則至少一條原 line），並保留
所選卡全部 user-stated lines；`request_refinement` 也不能丟掉目前卡片組的 user-stated lines。
來源 ref 與文字只留在 process memory；相對 slot 可重排，但宣告保留的內容必須確實出現在
新卡。遺漏時只回 redacted `needs_refinement` problem codes，不能展示新卡；完整時才回
`review_required`，固定揭露尚未驗證營業、交通、空位或價格，再請使用者審閱整合方向。
這仍是 `candidate + unverified`，不排日期、不呼叫 provider、不建立 trip、不 render、
不 deploy，也不提供 apply／confirmation authority。

Phase 5.7 只在 host 已清楚理解使用者對目前 `review_required` 整合方向的回覆後，才用
`capture_guided_refinement_response()` 擷取 exact typed response：接受目前方向是
`accept_direction`，繼續調整是 `request_adjustment`。它不做自然語言 parser；含糊回覆保留
原本的審閱問題。response 私有地綁定 exact brief、cards、preference、refinement candidate
與 derived review，任何內容漂移都會拒絕舊回覆。接受只把下一步送到
`prepare_private_itinerary_candidate` 這個 private-only seam；結果仍是
`candidate + unverified`，不建立 trip、不排程、不呼叫 provider、不寫入、不 render、
不 deploy，也不構成 confirmation／apply authority。若使用者提出新的調整內容，host 必須先
把其中明確事實重新抽取到目前的 private `TripBriefDraft`，再回到 `refine_private_direction`。

Phase 5.8 用 `GuidedItineraryCandidate` 把已接受整合方向的每一條 refined line，恰好一次放進
`0..overnight_count` 的相對 day bucket。bucket index 不是 calendar date、time、duration、route
或可執行 schedule；candidate 只保存 refined line index 與 opaque transport-boundary ID，不接受
新的自由文字。assessor 會先重驗 Phase 5.7 exact `accept_direction` context，再檢查 line 沒有
未知、遺漏或重複，並要求 transport boundary exact multiset carryover。只有完整 candidate 才能
在私有層顯示並請使用者審閱；safe transcript 只能使用 `GuidedItineraryReview.to_dict()`，不能
序列化 raw candidate。結果仍是 `candidate + unverified`，沒有 provider、scheduler、trip
creation、filesystem/store write、render、deploy、confirmation 或 apply authority。

Phase 5.9 只在 host 已清楚理解使用者對目前 `review_required` private itinerary candidate 的回覆後，
才用 `capture_guided_itinerary_response()` 擷取 exact `accept_itinerary_candidate` 或
`request_itinerary_adjustment`；不做自然語言 parser，也不保存 free text。capture 與 assess 都會
重驗 Phase 5.8，並將 exact brief、cards、preference、refinement、Phase 5.7 response、itinerary
candidate 與 derived review 綁進 private fingerprint。接受只回
`prepare_private_evidence_requirements` 這個後續規劃標籤；本切片不建立 provider request、不呼叫
provider、不排程、不建立或寫入 trip。要求調整則回 `refine_private_itinerary_candidate`；新事實
另外重新抽取進 private brief，不塞進 response。結果持續是 `candidate + unverified`，不構成
selection、booking、confirmation、apply 或任何 provider authority。

Phase 5.10 用 `GuidedEvidenceRequirementPlan` 為已接受 itinerary 的每條 refined source line
恰好建立一個 provider-neutral typed declaration。`requires_verification` 必須列出至少一個
`place_identity`、`current_opening_hours`、`route`、`lodging`、`availability` 或 `price` topic；
`no_external_evidence_identified` 不帶 topic，但仍只代表目前尚未識別外部證據需求，絕不表示
已驗證或可執行。`assess_guided_evidence_requirement_plan()` 會先重驗 exact Phase 5.9 accepted
context；未知、遺漏或重複 line 只回 redacted `needs_refinement`。完整計畫也只回
`prepare_private_provider_scope_review` planning label；safe transcript 只用 aggregate review，
不序列化 raw line indexes，且不建立 provider request、不呼叫 API、不授權 provider scope、
不排程、不建立或寫入 trip。結果持續是 `candidate + unverified`，不構成 selection、booking、
confirmation、apply 或 authoritative use。

Phase 5.11 用 `GuidedProviderScopeProposal` 將 Phase 5.10 的 nonzero evidence topics 映射成
固定、可稽核的 provider capability 與每項 request-count cap：Google Places identity／current
hours、Google Routes，以及 SerpAPI Google Hotels。每個 required topic 必須恰好一項，總 cap
最多 32；cap 只是請求次數邊界，不是金額上限。`assess_guided_provider_scope()` 會重驗完整
Phase 5.10 context，並在 safe review 中顯示 capability、可能需要送出的資料類別、可能計費、
價格尚未核對及 provider policy／credentials 仍待後續檢查。有效非空 scope 才回
`review_required` 並請使用者接受、縮小或取消；零 topic 只回 `no_provider_scope_required`，但仍
是 `candidate + unverified`。本切片不保存 query／payload／place ID／credential、不建立 request、
不呼叫 API，也不授權未來 provider call；scope 回覆與 exact policy/request gate 都是後續獨立邊界。

Phase 5.12 只在目前 Phase 5.11 scope 仍為 `review_required`、且 host 已清楚理解使用者回覆時，
用 `capture_guided_provider_scope_response()` 擷取 exact `accept_provider_scope`、
`request_smaller_provider_scope` 或 `cancel_external_lookup`。它沒有自然語言 parser 或 free-text
payload；private fingerprint 綁完整 guided context、evidence plan、scope proposal 與 fresh derived
review，除 card ordering 外任何 drift 都拒絕。接受只回
`prepare_private_provider_preflight_review` planning label，不是 provider authorization；縮小只回
scope refinement，不自動移除 topic 或調低 cap；取消只關閉這次 external lookup path，不刪除
evidence requirements，也不把 itinerary 升級為 verified／travel-ready。Safe handoff 只含 response
kind 與 aggregate topic／capability／request-cap counts；所有結果仍是 `candidate + unverified`，
不檢查 pricing／policy、不讀 credential、不建立 request、不呼叫 API、不寫入或套用任何變更。

Phase 5.3–5.12 至此完成純 offline guided-scope section。下一個 provider preflight 必須在執行當下
重新核對 current pricing、provider policy／terms／retention、credential/session availability 與 exact
request scope，並取得使用者明確參與；上述任何 planning label 或 response 都不能替代該邊界。

Phase 5.13 用 `GuidedProviderPreflight` 將 trusted host 針對已接受 Phase 5.12 scope 所做的
短效、typed preflight attestation 綁回完整 private context。每項只保留 capability、版本化
request／pricing／policy／retention profile、billing-region 分類、boolean credential status 與
request cap；SerpApi 項目另保留最多 32 的 capped plan-credit、auto-renewal status 與
ZeroTrace entitlement status。Attestation 必須是
UTC、最長 24 小時，且 exact context 或 profile 漂移都 fail closed。這些 profile 是 host 對
官方文件與本機狀態的聲明，不是 provider 自行驗證；list-rate 試算不是實際帳單或
金額上限，monthly free usage 也仍須執行時重查。

目前 contract 只支援 Google Maps non-EEA profile，並固定對應 Text Search Pro、Place
Details Enterprise 與 Compute Routes Essentials；EEA 或不明 billing region 會阻擋而不會
從旅行地點推測。SerpApi 則區分 standard provider storage 與 Enterprise-only ZeroTrace。
Safe review 只顯示 aggregate 與 typed profiles，不含 query、payload、credential、私人日期或地點；
`review_required` 也只允許進入下一個當輪 execution-authorization review，仍不建立 request、
不讀 credential、不呼叫 provider，且結果持續是 `candidate + unverified`。官方價格、欄位
與政策快照來源記錄在 roadmap 的 Phase 5.13 checkpoint。

Phase 5.14 只在 Phase 5.13 preflight 仍為 fresh `review_required` 時，用
`capture_guided_provider_preflight_response()` 擷取 exact `accept_provider_preflight`、
`request_smaller_provider_preflight` 或 `cancel_external_execution`。Response 沒有自由文字，
private fingerprint 綁完整 guided context、scope response、preflight bundle、derived review 與
不對外顯示的 capture time。Assessment 會同時重建原 review 與以當前 trusted UTC 重驗
freshness；過期、時鐘倒退或任何 context／profile／cap drift 都拒絕。接受只回
`prepare_private_provider_execution_authorization`，讓 host 準備下一個 exact execution
authorization review；縮小不會自動修改 scope，取消也保留 evidence requirements。三種
分支都不授權 provider、不建立 request、不讀 credential、不呼叫 API，並維持
`candidate + unverified`。

Phase 5.15 不猜測或接收真實 request target，而是由fresh accepted Phase 5.14 context
自動產生 `GuidedProviderExecutionTargets`：Places identity 需要 private typed identity intent，
current hours 需要既有或未來的 trusted place-identity evidence，Routes 需要兩個有順序的
trusted endpoints，SerpApi Hotels 需要 private typed hotel-search intent。本切片不讀 line text、
query、payload、Place ID、位址、座標或 target digest，因此所有 item 都明示為 deferred；
partial execution authorization 與 provider-result 自動授權 follow-up 一律禁止。Plan 綁完整
Phase 5.14 context 與 private preparation time，assessment 會用當前 trusted UTC 重驗過期與漂移。
它只回 `prepare_private_provider_execution_targets`，不是 execution review、request 或授權；
後續必須以 canonical private preimage 或現有 trusted evidence contract 做 exact binding，不接受
caller 單獨提供的 digest。

Phase 5.16 用 `GuidedProviderExecutionTargetPreimage` 將每個 Phase 5.15 target requirement
綁到 exact typed private preimage 與其實際服務的 source-line indexes。Places identity 直接接受
canonical `PlaceIdentityIntent`；current hours／Routes 只接受現有 token-gated、snapshot-bound 的
`GooglePlaceDetailsRequest`／`GoogleRouteRequest`；SerpApi Hotels 則把 process-local
`LodgingDiscoveryRequest` 當作 private search intent，而不是 provider provenance。Binder 會自行計算
domain-separated fingerprint，並對 trusted Google contracts 綁定 policy registry、snapshot、store 與
evidence revisions；caller 不能只交 digest，缺漏／重複 line coverage、錯誤 contract type、日期越界、
stale endpoint 或任一 context drift 都會拒絕。回傳物不保留 raw query、Place ID 或 target preimage，
也不建立 HTTP request、不呼叫 provider、不授權 scope；完整 binding 只前進到另一個 private
execution-authorization review preparation，結果仍是 `candidate + unverified`。

Phase 5.17 重新提供同一批 exact preimages，並以完整 Phase 5.16 binding chain 準備
`GuidedProviderExecutionAuthorizationReview`。預設 `to_dict()` 只列出 target kind、將傳送與僅供
本機審閱的欄位名稱、user-stated／tentative／AI-candidate 來源數量、exact-bound request count、
核准上限、Google 第一付費級距試算與 SerpApi plan-credit 數，不包含 query、日期、stable local label
或任何 provider Place ID。只有明確呼叫 `to_ephemeral_private_review_payload()` 才會產生供目前使用者
直接審閱的 process-local 私人值；stable local labels 會清楚標示為非 provider Place IDs，真正的
provider identifier、fingerprint、credential 與 snapshot／evidence revision 仍不顯示。所有來源行都
保持 needs-verification，safe 與 private view 都明示它們不是 authoritative。Review 綁目前 fresh 的
pricing／policy／retention／credential attestations，且把「若接受時的精確綁定請求數」和 cap 分開；
本階段建立的 request contract、HTTP request 與觀察到的 provider call 數都是 0。它只要求下一個
exact accept／request-smaller／cancel response gate，不擷取回覆、不授權 provider，結果仍是
`candidate + unverified`。

Phase 5.18 用 `GuidedProviderExecutionAuthorizationResponse` 擷取使用者對同一份仍 fresh 私人
review 的 exact `accept`、`request_smaller` 或 `cancel`；capture API 只接受 typed enum，不做自然
語言 parser、不保留 free text 或 caller-supplied authorization digest。Capture 與 assessment 都會重新
提供 exact preimages，並分別在 private capture time 與目前 trusted UTC 重驗完整 guided context、
preflight、endpoint freshness、targets、bindings 與 Phase 5.17 review。Safe handoff 只保留 response
kind、bound request／cost／credit／capability counts、source-state counts 與 attestation flags，不顯示 query、
stable local label、provider identifier、fingerprint、credential、revision 或時間。接受只回
`prepare_private_provider_execution_time_recheck`，不會立即啟用 execution authority；縮小只回 target
refinement，不會自動修改 targets 或 caps，且必須重建新的 exact review；取消只關閉目前 execution path，
保留 evidence requirements。三種分支都不建立 provider request contract／HTTP request、不讀 credential、
不呼叫 provider，也維持 `candidate + unverified`。

Phase 5.19 用 `GuidedProviderExecutionTimeRecheck` 消費 fresh Phase 5.18 `accept`、同一批 exact
target preimages 與 trusted host 新提供的 current attestations。它固定只有五分鐘有效，且不超過原
preflight expiry；prepare／assess 會在 capture time 與目前 trusted UTC 重驗完整 chain、preimages 與
endpoint freshness。Topic、capability、request profile 或 request cap 不能在這一關改變；pricing、policy、
retention、billing-region classification（台灣使用 non-EEA）或 SerpApi ZeroTrace 狀態若與使用者接受的
review 不同，必須重走新的 preflight／authorization chain。Credential 不可用、SerpApi plan credit 不足、
自動續費開啟或短期 attestation 過期則維持 blocked，可在 host state 修正後重新 recheck。Safe output 只保留
typed profiles、boolean availability 與既有 aggregate request／cost／credit／provenance counts，不顯示 private
target、provider identifier、fingerprint、credential、exact plan balance 或時間。Ready 結果也只回
`prepare_private_provider_request_materialization_review`；本階段不建立 provider request contract／HTTP
request、不啟用 execution authority、不呼叫 provider，仍是 `candidate + unverified`。

Phase 5.20 用 `GuidedProviderRequestMaterializationReview` 把仍 fresh、ready 的 Phase 5.19 recheck
與同一批 exact preimages 轉成四種 non-executable `GuidedProviderRequestContractCandidate`：Google
Places Text Search、Place Details、Routes Compute Routes 與 SerpApi Google Hotels。Candidate 數等於
exact-bound request 數，不會誤用 scope topic 數；多個 request 仍必須落在 accepted cap 內。Safe view
只列 request profile／materialization kind、field names、aggregate request／cost／credit／provenance counts
與 fresh recheck flags；明確呼叫 `to_ephemeral_private_review_payload()` 才會顯示 query、日期、語系與
stable local labels，provider Place IDs、fingerprints、credentials 與 SerpApi exact plan state 在 private view
也不顯示。Review 不得超過五分鐘 recheck expiry，capture response 前還要以目前 trusted UTC 重驗完整
chain。下一關只接受 `prepare_materialization`、`request_smaller` 或 `cancel`；前者明示只是準備下一階段，
不是立即執行。Phase 5.20 不建立 executable provider contract／HTTP request、不選 transport endpoint／
method、不讀 credential、不啟用 authority、不呼叫 provider，維持 `candidate + unverified`。

Phase 5.21 用 `GuidedProviderRequestMaterializationResponse` 擷取對同一份仍 fresh Phase 5.20 私人
review 的 exact `prepare_materialization`、`request_smaller` 或 `cancel`。Capture API 只接受 typed enum，
不解析自然語言、不保存 free text 或 caller digest；capture／assess 都會用同一批 exact preimages 與目前
trusted UTC 重驗完整 authorization、execution-time recheck 與 candidate-review chain，而且 response 不能
活得比原五分鐘 recheck 更久。`prepare_materialization` 只接受該份 review 並回
`prepare_private_provider_request_contract_materialization`，不是立即建立或執行 request；`request_smaller`
只回 targets refinement，所有 binding／review 都必須重建；`cancel` 關閉目前 materialization path 並保留
evidence requirements。三個分支都不修改 targets／caps／candidates、不建立新 candidate、executable provider
contract 或 HTTP request、不讀 credential、不啟用 authority、不呼叫 provider，仍是
`candidate + unverified`。

Phase 5.22 用 `GuidedProviderRequestContractMaterialization` 消費 fresh Phase 5.21
`prepare_materialization` response 與同一批 exact preimages，建立 process-local、token-gated 的
`GuidedProviderRequestContract`。四種 typed surface 會保留真正要傳給 provider 的 exact values；Place
Details／Routes 的 provider Place IDs、stable local result binding 與 source-binding fingerprint 也只留在
private fields，bundle 不保留 raw preimage。Materialize／assess 都會在原 materialization time 與目前
trusted UTC 重建完整 chain，而且 bundle expiry 不得超過 Phase 5.19 的五分鐘 execution recheck；過期、
clock rollback、response kind、context、preimage 或 contract drift 都會 fail closed。Safe view 只顯示 field
names、counts、profiles、cost／credit／provenance aggregates 與 freshness flags，不顯示 exact query、日期、
provider／local IDs、fingerprints、credentials、SerpApi exact plan state 或時間。這些 contracts 明確不可執行、
不可送出，也沒有 endpoint、HTTP method、credential slot、HTTP request、send／execution authority 或 provider
call；結果仍是 `candidate + unverified`，只前進到另一個獨立的
`prepare_private_provider_request_send_authorization_review` gate。

Phase 5.23 用 `GuidedProviderRequestSendAuthorizationReview` 把同一份仍 fresh Phase 5.22
materialization 轉成最後一道 private human review。預設 safe view 只列 contract shapes、field names、
request／cost／credit／provenance aggregates 與 typed `accept_send`、`request_smaller`、`cancel` options；只有
明確呼叫 `to_ephemeral_private_review_payload()` 才會顯示 exact non-identifier provider-transmitted values
與 local result binding values。Provider Place IDs 仍只顯示 redacted field names，source／context fingerprints、
credentials、SerpApi exact plan state 與時間也不顯示。Prepare／assess 會在原 review time 與目前 trusted UTC
重驗完整 Phase 5.22 chain、同一批 preimages 與五分鐘 expiry；review 不保留 raw preimage。它只回
`capture_private_provider_request_send_authorization_response`，尚未擷取選項；三個 options 都不等於目前已有
send authority。本階段仍沒有 endpoint、HTTP method、credential binding、HTTP request、network 或 provider
call，結果維持 `candidate + unverified`。

Phase 5.24 用 `GuidedProviderRequestSendAuthorizationResponse` 擷取對同一份仍 fresh Phase 5.23
private review 的 exact `accept_send`、`request_smaller` 或 `cancel`。Capture API 只接受 typed enum，
不解析自然語言、不保存 free text，也不接受 caller-supplied digest、target subset 或 cap mutation；
capture／assess 都會用同一批 exact preimages，在原 capture time 與目前 trusted UTC 重驗完整
authorization、execution-time recheck、materialization 與 send-review chain，而且 response 不能活得比
原五分鐘 execution recheck 更久。Safe handoff 只保留 response kind、contract／request／cost／credit／
provenance aggregates 與 fresh attestation flags，不顯示 exact request values、provider／local IDs、
fingerprints、credentials、SerpApi exact plan state 或時間。`accept_send` 只接受該份 review 並回
`prepare_private_provider_request_send_preparation`，讓後續獨立 gate 重新準備 send；它不會使 contract
可送出或啟用 send authority。`request_smaller` 只回 execution-target refinement，所有 bindings、contracts
與 reviews 都必須重建；`cancel` 關閉目前 send path 並保留 evidence requirements。三個分支都不選
endpoint／HTTP method、不綁 credential、不建立 HTTP request、不呼叫 provider，結果仍是
`candidate + unverified`。

Phase 5.25 用 `GuidedProviderRequestSendPreparation` 消費同一份仍 fresh Phase 5.24
`accept_send` response、完整 exact context、同一批 preimages 與 materialized contracts，並將每個 contract
綁到 versioned allowlist：Google [Places Text Search (New)](https://developers.google.com/maps/documentation/places/web-service/text-search)
使用 `POST https://places.googleapis.com/v1/places:searchText`、[Place Details (New)](https://developers.google.com/maps/documentation/places/web-service/place-details)
使用 `GET https://places.googleapis.com/v1/places/{provider_place_id}`、[Routes Compute Routes](https://developers.google.com/maps/documentation/routes/compute_route_directions)
使用 `POST https://routes.googleapis.com/directions/v2:computeRoutes`，而 [SerpApi Google Hotels](https://serpapi.com/google-hotels-api)
使用 `GET https://serpapi.com/search.json` 與固定 `engine=google_hotels`。Binding 只記錄公開 endpoint
template、HTTP method、provider field placement 與 credential slot：Google Maps 為 `X-Goog-Api-Key` header，
SerpApi 為 `api_key` query parameter；它不讀取或保存 credential value，也不展開 Place ID path、不組 query／
JSON／headers 或 HTTP request。Prepare／assess 會在原 preparation time 與目前 trusted UTC 重建完整 chain，
保留原五分鐘 expiry，clock rollback、expiry、response kind、preimage、contract、transport allowlist 或 context
drift 一律 fail closed。Safe view 可顯示上述公開 transport shape 與 aggregate counts，但不顯示 exact request
values、provider／local IDs、fingerprints、credentials、SerpApi exact plan state 或時間。Bundle 仍不可執行、
不可送出，沒有 network／provider call 或 send／execution authority，維持 `candidate + unverified`；它只前進到
獨立 `prepare_private_provider_request_credential_binding_review` gate，任何 credential value binding 與 live send
仍須另行明確授權。

Phase 5.26 用 `GuidedProviderRequestCredentialBindingReview` 將同一份仍 fresh Phase 5.25 transport-bound
bundle 轉成 current-user private review。Prepare／assess 都必須重新提供同一批 exact preimages，並在原 review
time 與目前 trusted UTC 重驗完整 Phase 5.11–5.25 chain；review expiry 仍沿用 Phase 5.19 原五分鐘邊界，
clock rollback、expiry、preimage、context、response、contract、transport profile 或 credential-slot drift 全部
fail closed。預設 safe view 只顯示公開 endpoint template、HTTP method、provider field placement、credential
slot、aggregate request／cost／credit／provenance counts及 `accept_credential_binding`、`request_smaller`、`cancel`
三個 typed options；只有明確呼叫 `to_ephemeral_private_review_payload()` 才會顯示 exact non-identifier query、
日期與其他 provider-transmitted values。Provider identifier values、local-result values、fingerprints、credential
values、SerpApi exact plan state與private times在兩種 view 都不顯示，URL path也不展開。Review只回
`capture_private_provider_request_credential_binding_response`，本階段尚未擷取選項；`accept_credential_binding`
只是下一個 response gate 的選項，不代表目前已有 credential access、binding 或 send authority。Module不讀
env／vault、不取得或保存 key、不建立 headers／query／JSON／HTTP request、不使用network、不呼叫provider，
也不寫trip／store、不schedule／render／deploy，結果維持 `candidate + unverified`。

Phase 5.27 用 `GuidedProviderRequestCredentialBindingResponse` 擷取對同一份仍 fresh Phase 5.26
private review 的 exact `accept_credential_binding`、`request_smaller` 或 `cancel`。Capture API 只接受 typed
enum；自然語言、free text、一般的「繼續」、caller-supplied digest、target subset、cap mutation 或 partial
response 都不算授權。Capture 先在 trusted UTC 重驗 visible review；assess 再於原 capture time 與目前 trusted
UTC 以同一批 exact preimages 重驗完整 chain，沿用 Phase 5.19 的五分鐘 expiry，並對 rollback、expiry、
preimage／context／review／transport drift、tamper 或 replay fail closed。Safe handoff 只保留 response kind、
transport profile 與既有 request／cost／credit／provenance aggregates，不保留 transport metadata、raw preimage，
也不顯示 exact request values、provider／local IDs、fingerprints、credential values、SerpApi exact plan state 或時間。
`accept_credential_binding` 只回 `prepare_private_provider_request_live_credential_binding_gate`，允許準備另一個
獨立 live gate；它本身不讀 env／vault／key、不綁 credential、不建立 HTTP request，也不授予 send／execution
authority。`request_smaller` 回 execution-target refinement；`cancel` 關閉目前 credential-binding path。三個分支
都不使用 network、不呼叫 provider、不寫 trip／store、不 schedule／render／deploy，結果仍是
`candidate + unverified`。

Phase 5.28 用 `GuidedProviderRequestLiveCredentialBindingReview` 消費同一份仍 fresh Phase 5.27 exact
`accept_credential_binding` response，並要求 host 為 Phase 5.25 bundle 實際需要的每個 public credential slot
提供一筆 typed `available`／`unavailable` boolean-equivalent attestation。Slot 缺漏、重複、多餘或類型不符直接
拒絕；任一 unavailable 只回 `blocked` 與 refresh attestation，不顯示 response options。全部 available 才回
`review_required`，並只提供 `accept_live_credential_binding`、`request_smaller`、`cancel` 三個下一階段 exact
options；一般「繼續」不算 live credential-binding 授權。Prepare 在 trusted UTC 重驗 Phase 5.27 response，
assess 再於原 preparation time 與目前 trusted UTC 重驗完整 chain、同一批 preimages、slot coverage 與
fingerprint，且沿用 Phase 5.19 expiry、不延長安全窗。Safe view 只顯示 credential slot 名稱、availability
boolean 與既有 request／cost／credit／provenance aggregates，不顯示 credential value、private request values、
provider／local IDs、fingerprints、SerpApi exact plan state 或時間。Module 不讀 env／vault、不取得或綁定 key、
不展開 URL、不建立 HTTP request、不使用 network、不呼叫 provider，也不授予 send／execution authority；結果
仍是 `candidate + unverified`。

Phase 5.29 用 `GuidedProviderRequestLiveCredentialBindingResponse` 擷取對同一份仍 fresh Phase 5.28
`review_required` private review 的 exact `accept_live_credential_binding`、`request_smaller` 或 `cancel`。Capture
API 只接受 typed enum；自然語言、free text、一般「繼續」、blocked／unavailable review、caller digest、target
subset、cap mutation 或 partial response 都不能前進。Capture 先在 trusted UTC 重驗 Phase 5.28 review；assess
再於原 capture time 與目前 trusted UTC 以同一批 exact preimages、transport bindings 及原 slot-level availability
attestations 重驗完整 chain，沿用 Phase 5.19 五分鐘 expiry，rollback、expiry、context／review／attestation drift、
tamper 或 replay 一律 fail closed。Safe handoff 只保留 response kind、public credential-slot availability booleans 與
既有 request／cost／credit／provenance aggregates，不顯示 private request values、provider／local IDs、fingerprints、
credential values、SerpApi exact plan state 或時間。`accept_live_credential_binding` 只回
`prepare_private_provider_request_ephemeral_credential_value_binding_gate`，允許準備另一個獨立短效 gate；它本身
不讀 env／vault／key、不綁 credential value、不建 HTTP request，也不授予 send／execution authority。
`request_smaller` 回 execution-target refinement；`cancel` 關閉目前 live binding path。三個分支都不使用 network、
不呼叫 provider、不寫 trip／store、不 schedule／render／deploy，結果仍是 `candidate + unverified`。

Phase 5.30 以 `GuidedProviderPreExecutionContext` 把同一份 fresh Phase 5.29 exact accept、完整 guided
context、transport bindings、availability attestations 與 exact preimages 組成單次、五分鐘內有效的 private
pre-execution facade。只有在上游重驗通過後，host-injected resolver 才會為每個 distinct public credential slot
取值恰好一次；短效 lease、prepared requests 與 execution claim 都是 sealed、不可序列化且 fail closed。四種
provider profile 使用各自的固定 builder，包含已審閱的 `pageSize=5`、
`computeAlternativeRoutes=false`、Routes mode mapping 與 Place ID path encoding；resolver 前後都會以新的 trusted
UTC 重驗，而且不延長原 expiry。Safe review 只顯示 public profile／slot／count／cost／credit aggregates，不保留
raw preimages、credential value、private request values、provider／local IDs 或 fingerprints。此 facade 仍沒有
transport、network、provider call、evidence promotion 或 canonical write；唯一下一步是 Phase 5.31 的 bounded
injected-transport execution。

Phase 5.31 以 `execute_guided_provider_requests()` 消耗一次 Phase 5.30 claim，且只接受 host-injected
transport。每次 send 前會保留 request／Google list cost／SerpAPI credit／timeout／absolute deadline budget；
所有 request 先各得一次 initial chance，只有 delivery outcome unknown 可在同一份 exact wire 上重試一次。
Caller limits、prepared request、credential lease 與四種 provider-specific normalization target 都以 executor-owned
snapshot 及 send 前後 private exact fingerprint 防止 TOCTOU；response stream、header 與 body 有固定上限，late、
malformed 或 delivery certainty 不明一律轉入 manual reconciliation。HTTP response 只會進 sealed、不可序列化的
private quarantine，保留 Places Identity／Details／Routes／SerpAPI Hotels 各自的 typed adapter target；safe output
仍是 `candidate + unverified`，不會直接 normalize、寫 evidence、修改 canonical plan 或把 hotel DTO 當 provenance。
唯一下一步是 Phase 5.32 的 provider-specific assessment。

Phase 5.32 以 `assess_guided_provider_quarantined_responses()` 在不再呼叫provider的前提下重驗每筆
quarantine的context／request／source／target／response binding，並依Places Identity、Place Details、Routes與
SerpAPI Hotels各自既有adapter及其response bounds解碼；non-2xx、malformed、過大或send-time語意失效的結果都只會
拒絕。Identity candidate仍需原本的host review，之後以EvidenceStore revision CAS寫入durable evidence；Details／
Routes只進process-local EvidenceSession，Hotels維持non-provenance candidate DTO。只有由current EvidenceSnapshot
重新composition、validate及既有ScheduleStager／RepairController／LodgingConfirmationStager產生的exact domain review
才可進`GuidedCanonicalApplyReview`；它不接受caller提供的raw `PlanPatch`。Create只從完整、已接受的guided itinerary
投影candidate + unverified baseline，review會顯示exact candidate；尚未支援的transport boundaries與任何lodging alias
都fail closed。`accept_apply`不能取代protected `ApprovalGrant`或externally signed lodging confirmation，exact replay
只確認既有receipt而不宣稱本次又寫入。下一個且最後一個Phase 5 macro phase是5.33 unified product interface。

Phase 4.5B 以獨立 runtime sidecar 將候選投影成 comparison-ready view：位置 identity
與 route observation 綁 exact `EvidenceSnapshot`；route 另須保留原 request receipt，
且 receipt 的 endpoint observation/value 仍與目前 snapshot 相同，才可使用
duration/distance。missing、stale、conflicted、缺 receipt 或 endpoint drift 都只回
`needs_verification` 與去重後的 refresh request，不會改寫 4.5A candidate。比較欄位
只表達涵蓋晚數、住宿類型、位置精度、價格是否已知與 evidence readiness；「哪間最
好」不在 4.5B 裡決定。

Phase 4.5C 新增純 runtime 的 joint recommendation sidecar。Caller 先把已知的固定
抵離交通、已訂活動、住宿候選錨點、Routes 與 current opening hours 組成同一
`ComposedTripState`／exact `EvidenceSnapshot`，再為各住宿配置建立 detached
`ScheduleProblem`。只有共同 route slots、solver required arcs 與 hours 都可由同一
snapshot 重播時，才依固定 lexicographic vector 排出「優先 review」；missing、stale、
conflicted、缺 receipt、reported booking claim、snapshot drift 或同分都不會產生
winner。價格在有可比較且 scope 一致的 price evidence 前明確排除。結果永遠
`supports_authoritative_use=false`，不含 `PlanPatch`、decision promotion 或 canonical
writer。

Phase 4.5D 新增住宿專用的 review／confirmation／canonical apply seam。使用者可直接
指定住宿，或從4.5C option明選；兩者都會先轉成只含日期、住宿類型、opaque location
與每日anchor的`LodgingConfirmationRequest`。只有trusted host在AI process外簽發、
且`TripStore`以注入的verifier驗證通過的exact grant，才能套用
`SetLodgingSelection`；generic `ApprovalGrant`、4.5C排名、reported claim或可import
的內部builder都不能取代。一次明確住宿確認可同時滿足同一exact effect的既有
protected-change gate，不要求使用者重複確認。

Canonical lodging的decision可為`selected`／`fixed`／`booked`，但evidence固定仍是
`unverified`；已決定不等於資料已驗證。Raw地址、座標、label、價格、booking link、
provider token與process-local candidate ID不進plan、receipt、history或safe output；
住宿位置會先轉成不可逆的canonical opaque ID。Lost-ACK保留pending review並以同一
idempotency request重播；不同trip、revision、patch、anchor、selection binding或
簽章一律fail closed。

可用純離線 walkthrough 查看這條路徑的實際結果；它只建立temporary plans，不讀寫
`trips/`、不呼叫provider，也不render或deploy：

```bash
.venv/bin/python scripts/phase45_acceptance.py
```

Phase 4.6A 提供純唯讀的 `assess_trip_readiness()`。Caller 必須同時提供 canonical
plan、exact `ComposedTripState`、同一份 `EvidenceSnapshot`與原 composition 使用的
opening-hours keys；assessor 會從 plan + snapshot 重新 compose、比對完整 runtime
view、衍生住宿摘要，再執行 deterministic timeline kernel，不接受 caller 自稱的
binding或檢查結果。輸出固定為 `draft`、`review` 或
`travel_ready`，只含 safe digests、counts、machine-readable problems、單一
`next_action` 與可用時的 `recheck_required_at`。missing、stale、conflicted、
retention-expired、binding drift、缺 live attribution、regular hours 與待確認住宿
各有保守分流；canonical/composed不一致會要求重新組裝，且需要 recompose、
refresh 或 resolve 時不會捏造 evidence 有效期限。有效期限會取 freshness 與
retention 兩者較早者；尚待確認住宿會另以 review 到期時間要求重新評估。沒有
期限時，繁中摘要也不會暗示存在指定時間。
只有仍有效的 waiting lodging review 會要求 `confirm_lodging`；過期、被拒絕或
綁錯行程版本的 review 會要求 `restage_lodging_review`。

Phase 4.6B 提供 legacy evidence / cleanup 的**只讀 preview**，用於正式migration或
合規清理前的使用者審閱：

```bash
.venv/bin/python scripts/preview_legacy_evidence.py trips/{slug}
```

它只輸出去敏感化的aggregate分類：舊 `source=api` route 一律是需刷新、manual
route待使用者分類、Places／flight／hotel cache一律quarantine。輸出固定
`imports=0`、`cleanup_targets=[]`；沒有`--migrate`、`--apply`或`--delete`，不會
呼叫provider、寫入plan/EvidenceStore、render或修改`trips/`。目前legacy與canonical
相容層仍需要`places_cache.json`，所以即使preview存在也不可自行清理cache。任何真實
provider驗收或破壞性cleanup都必須在你審閱exact preview後另行明確授權。

Phase 5 目前有四個共用同一 JSON envelope 的唯讀入口：

```bash
.venv/bin/python scripts/tripctl.py inspect trips/{slug}
.venv/bin/python scripts/tripctl.py validate trips/{slug}
.venv/bin/python scripts/tripctl.py propose trips/{slug} \
  --evaluation-at 2026-08-09T12:00:00+00:00
.venv/bin/python scripts/tripctl.py score trips/{slug} \
  --proposal-ref sha256:{propose 回傳的 64 位 digest} \
  --evaluation-at 2026-08-09T12:00:00+00:00
```

`inspect` 輸出固定、去敏感化的 JSON envelope。legacy 模式包住同一份 evidence preview；
成功只表示「可供審閱」，絕不代表 `travel_ready`、可信 route evidence、migration 或 cleanup
授權。若 legacy source 本身缺檔或不安全，inspect 仍會安全輸出既有 aggregate repair
problems，但狀態會是 `repair_required`，不會把它誤報成可重試的新 stale preview。

若存在安全且有效的 `plan.json`，inspect 會選 canonical 模式，只輸出 revision／source digest、
generation 與 aggregate counts，不輸出 trip ID、標題、地點、時間或 receipt 內容。因本入口
刻意不開啟可能因 retention 寫入的 EvidenceStore，也沒有 exact runtime evidence snapshot，
結果固定為 `waiting_external`、`next_action=refresh_evidence`，不能升級成 `travel_ready`。

`validate` 是另一個問題：legacy 模式只將 `trip.json` 與 `itinerary.json` 以 bounded、
no-follow 的 temporary snapshot 載入 deterministic timeline kernel（固定 `now=None`）；
canonical 模式則讀取同樣 bounded、no-follow 且重驗來源的 `plan.json` snapshot。兩者都只
輸出 timeline status、issue token/severity/count 與安全 aggregate count，不讀取 cache、
不呼叫 provider、不寫入 trip，也不取代 `scripts/validate_trip.py` 對完整 renderer 契約的
結構檢查。canonical timeline 若 infeasible 會要求修復；其餘結果在 runtime evidence 未載入時
保持 `waiting_external`，不是 `travel_ready`。

兩個入口遇到任何 `plan.json` 都不會 fallback 到相鄰 legacy 檔案。broken symlink、directory、
FIFO、oversized 或 malformed canonical marker 仍以既有 `CANONICAL_INSPECT_UNAVAILABLE`／
`CANONICAL_VALIDATE_UNAVAILABLE` 安全拒絕；讀取或評估期間的 exact source drift 會回可重試的
`STALE_CANONICAL_PLAN`，不附 partial result。provider、EvidenceStore、migration、render 與
canonical write 全部仍在這兩個命令之外。

`propose`／`score`目前只接受 canonical trip。`propose`在 caller 明示、timezone-aware 的
evaluation instant下建立既有`ScheduleProblem`並執行 bounded deterministic solver；輸出只含
opaque `problem_ref`／`proposal_ref`、solver status與counts，不含 assignments、activity/day/location
ID或時間。`score`不信任 caller 提供的 candidate或分數，而是以同一 instant重新讀取 canonical
state、重跑 solver、比對 exact proposal ref並執行 trusted replay，之後才輸出 aggregate
lexicographic score breakdown。錯誤 instant、ref或 canonical revision會要求重新 propose；執行中的
source drift則回可重試的`STALE_CANONICAL_PLAN`，沒有partial result。

這兩個結果都固定`provisional=true`、`runtime_evidence_loaded=false`，不開EvidenceStore、不持久化
candidate或pending review，也不建立apply／approval authority。即使 canonical內的舊欄位標為
verified，score仍只能用於下一步技術比較；必須用下述exact runtime evidence composition重新評估，
不能據此聲稱`travel_ready`或寫入`plan.json`。Legacy trip會在 scheduler前以
`CANONICAL_PLAN_REQUIRED`拒絕，不會自動migration。

Developer host已有獨立的process-local evidence seam：
`validate_trip_with_evidence()`、`propose_trip_with_evidence()`與
`score_trip_with_evidence()`只接受exact `EvidenceSnapshot`物件，不接受JSON、digest或CLI flag
自稱已載入evidence。Snapshot自己的evaluation clock會同時綁入composition、Phase 4.6 readiness、
schedule problem與proposal ref；public proposal ref另綁readiness ID、exact lodging intake與
pending review組成的runtime context ref，因此canonical revision、clock、evidence binding或typed
runtime context任一漂移，都必須重新propose。Disk-only與
evidence-bound proposal refs也不能互換。

這條seam仍沿用bounded no-follow canonical reader並在結果離開前重驗來源；不自行開啟可能因
retention而寫入的EvidenceStore，也不呼叫provider或寫trip。Evidence-bound結果會附上安全的
readiness profile與明確`readiness_scope`、`runtime_evidence_loaded=true`、`provisional=false`；
此處的non-provisional只表示
分數已綁exact runtime evidence，不代表已獲canonical mutation authority。Score一律明示
`apply_authority=false`、`canonical_write_performed=false`。只有candidate對canonical state有實際
持久化變更時才回`apply_review_available=true`／`review_proposal`；no-op score回
`apply_review_available=false`／`next_action=none`，不要求不存在的apply授權。CLI刻意不新增
`--evidence-*`入口。

Developer host可再呼叫`prepare_trip_schedule_apply_review()`，傳入同一exact snapshot、runtime
context、opaque proposal ref、exact `TripStore`與可重載的evidence source。此review-only bridge會
重新compose／solve／trusted replay，重驗proposal ref與clock，經`ScheduleStager`重載current
evidence、確認strict improvement並只做canonical preview，最後包成30分鐘有效、不可序列化的
`accept_apply`／`request_changes`／`cancel` review。私有人工審閱面會顯示exact canonical diff，
但不輸出provider runtime state或evidence／product-context digest；結果固定
`apply_authority=false`、`canonical_write_performed=false`、`pending_review_retained=true`。
這個bridge不擷取response、不呼叫commit，也沒有CLI `apply`入口；generic「繼續」不能替代exact
enum response。真正執行仍須在下一個獨立gate重驗review expiry、current evidence、canonical CAS、
必要approval與receipt語義。

已產生的 legacy 行程頁另有第五個唯讀「檢查」分頁；可在行程網址後加上
`#review` 直接開啟。它只接收 `validate` 經過固定繁中分類後的 aggregate 摘要，
不嵌入 raw issue token、地點、時間、ID、evidence 或 provider 資料；顯示「可行」
也不代表 `travel_ready`。頁面是產生當下的離線結果，更新資料後必須重新產生，且不會
確認即時交通、營業、空位或訂位。

Ishigaki 的使用者已授權 live exit gate 時，才可使用下列**固定範圍**命令：

```bash
direnv exec . python3 scripts/ishigaki_provider_exit_gate.py trips/ishigaki-2026-10 --live
```

它最多作兩次 Places identity search 與一次 driving Routes request；每一階段前後
都重查固定的`trip.json`、`itinerary.json`與`place_candidates.json`來源；pagination、
ambiguity 或 source drift 一律停止。所有 identity
與 route evidence 僅在 process memory 做 typed contract check 後丟棄，不會寫入
legacy cache、EvidenceStore、`plan.json`、renderer 或 deployment。這是單一 pilot，
不是一般的 live provider CLI；遠期營業時間仍必須在接近行程日期時重新確認。

若 gate 回報 identity review required，只有使用者要求查看候選時才可額外執行一次
read-only origin review：

```bash
direnv exec . python3 scripts/ishigaki_provider_exit_gate.py trips/ishigaki-2026-10 --live --review-origin
```

它只做一個 Places request、零 promotion／merge／route call；候選的名稱、公開地址與
類型僅作本次人工選擇並附 Google Maps attribution，provider ID、query、座標、token
與原始回應不會輸出或保存。選擇本身不是 grant；後續仍需新的 exact、source-bound
review 才可繼續。

若正常 Ishigaki gate 已明確回報`invalid_request`，且使用者另行明確允許最小診斷，才可
使用：

```bash
direnv exec . python3 scripts/ishigaki_provider_exit_gate.py trips/ishigaki-2026-10 \
  --live --minimal-route-diagnostic --origin-choice B \
  --origin-selection-binding-v2 {matching-review-binding}
```

它仍會重新驗證兩個 identity，並只發出一次同一個 driving request、但回應 field mask
縮減為`routes.distanceMeters,routes.duration`。這是 provider-acceptance 診斷，不是
route evidence：只回報 HTTP 是否接受請求，絕不解碼、merge、保存或顯示路線值；任何
source drift 或選擇綁定變動一律停止。

若最小 mask 仍被拒絕，下一個且同樣需要明確授權的 baseline 診斷會改用
`--undated-route-diagnostic`；它只移除`departureTime`，以區分遠期時刻與端點／專案
存取問題。它仍不是十月行程的 route evidence。

這個 readiness seam 不讀寫 store、plan 或 provider，也沒有 mutation／confirmation
authority；公開行程識別只提供不可逆的`trip_ref`。Canonical 住宿即使已是
`booked`，其 evidence 仍固定為 `unverified`，
因此只能進 `review`，不會被「已決定」誤判成「已驗證」。沒有住宿時也必須由
lodging intake 以exact `[stay_start, stay_end)`明確表達 `not_required`，否則
readiness 會要求補充資訊。

4.5B 另提供純 offline 的 SerpApi hotel response normalizer，嚴格區分 metadata
status、top-level error、empty success 與 partial result；query/search ID/token/位置
與價格原值不進 safe view，所有結果仍只產生 provider-discovered candidate +
unverified。Result 只是 non-provenance DTO；status 與 process-local diagnostic ref
不可作 authorization、cache、evidence、receipt 或住宿決策依據。此 normalizer 沒有
HTTP、cache、`HOTEL_OFFER` fact、房態或訂位語意。
Phase 4.5D的host-owned住宿確認只接受上述exact request，不會把discovery result
直接promotion。舊
`search_flights.py` 與其 cache 保留資料相容性但已 quarantine；`search_hotels.py`
也只可在使用者明確授權 live provider、成本與資料保留政策後，作 exit gate 外的
legacy 候選 discovery；不能代表房態、訂位或可直接寫入計畫。一般 4.5B 流程只離線
正規化 caller-supplied response，不會自行呼叫 provider。

私有規劃流程：
1. **Guided draft** — 自然語言 → 私有、無副作用草稿 → 只問真正 blocker
2. **Scout** — 互動式規劃：候選景點 → 用戶取捨 → 路線優化 → 驗證
3. **Build** — 組裝 JSON → 充實交通 → legacy營業時間提示 → 渲染私有 HTML

公開發布是另外的、明確審閱流程，不是 Build 的自動步驟。

## 專案結構

```
trip-plan/
├── trip_planner/          # deterministic kernel、canonical codec、mutation/store
├── tests/                 # offline adversarial regression
├── docs/                  # roadmap、ADR 與 correctness contracts
├── scripts/               # 所有腳本
│   ├── build_places_cache.py   # legacy full-mask cache（預設 quarantine）
│   ├── build_itinerary.py      # 從簡化輸入 + cache → itinerary.json
│   ├── enrich_itinerary.py     # 充實交通資料（距離/時間/模式）
│   ├── routes_coverage.py      # Routes API 地區覆蓋資料（transit/two_wheeler 支援國家）
│   ├── check_hours.py          # legacy regular-hours advisory（不輸出綠燈）
│   ├── search_flights.py       # legacy：後續 quarantine，非正常規劃功能
│   ├── search_hotels.py        # legacy candidate discovery，非訂房／房態來源
│   ├── render_trip.py          # private local preview：HTML + 行事曆
│   ├── build_index.py          # private local preview 的首頁
│   ├── prepare_public_release.py # 不寫入的公開 manifest 候選
│   ├── build_public_site.py    # 只建置已核准的公開 artifact tree
│   └── deploy.sh               # explicit public release only
├── template/
│   ├── trip.html               # 行程頁面 Jinja2 模板
│   ├── index.html              # 首頁模板
│   ├── public_trip.html        # 精簡公開行程模板
│   ├── public_index.html       # 精簡公開首頁模板
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
├── public/                     # 明確審閱後才建立的公開來源
│   ├── release.json            # source + rendered HTML digest allowlist
│   └── trips/{slug}.json       # 僅公開摘要，絕不自動由 trips/ 產生
├── .envrc.example
└── requirements.txt
```
