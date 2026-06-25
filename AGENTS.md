# AGENTS.md

## 先讀

1. `README.md` 看專案用途與資料結構。
2. Codex 應優先使用已安裝的 `trip-planner` skill；詳細 workflow 在 `~/.codex/skills/trip-planner/SKILL.md`。
3. 需要 script schema 或 edge cases 時再讀 `skill/trip-planner.md`。
4. `trips/` 是 local-only trip data，通常被 `.gitignore` 忽略，但 deploy 會讀它產生 GitHub Pages HTML。

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

## 工作邊界

- 不要刪除或覆蓋 local trip data，除非使用者明確要求。
- 不要執行 `scripts/deploy.sh`，除非任務是部署；部署會根據 local trips 產出並推到 GitHub Pages。
- 不要臆測交通時間、營業時間或價格；沒有來源時標成待確認。
- 目前 worktree 可能有使用者自己的 `.envrc.example` 變更；非任務需要不要碰。

## Review 方針

- 行程更新後至少跑 `scripts/check.sh`。
- 涉及路線與營業時間時，額外用相關 cache/validation scripts 驗證。
- sub-agent 適合分工：航班/飯店比較、景點來源驗證、route sanity check、網站輸出 QA。
