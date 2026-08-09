# AGENTS.md

## 先讀

1. `README.md` 看專案用途與資料結構。
2. Codex 應優先使用已安裝的 `trip-planner` skill；詳細 workflow 在 `~/.codex/skills/trip-planner/SKILL.md`。
3. 需要 script schema 或 edge cases 時再讀 `skill/trip-planner.md`。
4. `trips/` 是 local-only trip data，通常被 `.gitignore` 忽略。private renderer 可讀它做本機預覽；公開 deploy 永不讀它，只讀明確核准的 `public/release.json` 與 `public/trips/`。

## 目前狀態

- 這是用結構化 JSON + scripts 產生旅行網站與 calendar 檔的工具。
- 行程事實分成 fixed / tentative / need-to-verify；不要把未確認資料寫成已預訂。
- 距離、路線、營業時間、航班/飯店價格都屬於會變動資訊，需要用工具或來源驗證。
- `.envrc` / API keys 屬於本機 secrets；不要 commit。

## 驗證

預設完整檢查：

```bash
bash scripts/check.sh
```

`scripts/check.sh` 會編譯 scripts 並驗證目前 `trips/*` 的資料完整性；不 render、不 deploy、不呼叫 Google/SerpApi。

## 每日 Provider 開發工作階段

- 已授權的 live provider 開發開始前，先執行 `python3 scripts/trip_planner_dev_session.py status`。若為 `active`，必須重用 credential availability，不得再要求使用者解鎖 vault。
- 若為 `inactive`，由使用者在本機執行一次 `python3 scripts/trip_planner_dev_session.py start`。它只快取 allowlisted provider key 到 `/run/user/$UID` 私有 tmpfs；24 小時後所有讀取會立即拒絕，generation-bound user timer 另做 best-effort 實體清除；不保存 `BW_SESSION`。
- Daily session 只代表 credential availability。它不建立、延長或擴大 provider request、send、execution、canonical mutation 或 deployment authority，也不取代任何 exact typed response／短效 gate。
- 是否需要重問一般 conversational live scope，只能依目前對話與 typed contract 判斷，不能從 session 狀態推論；目前對話已明確涵蓋且 scope 未變時不要機械性重問，但契約要求的 exact response、expiry 與 authority 必須照常執行。
- 不讀取、輸出、記錄或序列化 runtime session 檔；`_emit` 僅能由 `.envrc` 的 command substitution 使用。需要提前結束時執行 `python3 scripts/trip_planner_dev_session.py stop`；stop／expiry 只能阻止後續載入，不能撤回已由既有 process 繼承的環境變數。

## 工作邊界

- 不要刪除或覆蓋 local trip data，除非使用者明確要求。
- 不要執行 `scripts/deploy.sh`，除非使用者明確要求發布已審閱的公開摘要；它會驗證 `public/release.json` 後 force-push 僅含 allowlisted public artifacts 的 GitHub Pages。
- 不要臆測交通時間、營業時間或價格；沒有來源時標成待確認。
- 目前 worktree 可能有使用者自己的 `.envrc.example` 變更；非任務需要不要碰。

## Review 方針

- 行程更新後至少跑 `scripts/check.sh`。
- 涉及路線與營業時間時，額外用相關 cache/validation scripts 驗證。
- sub-agent 適合分工：航班/飯店比較、景點來源驗證、route sanity check、網站輸出 QA。
