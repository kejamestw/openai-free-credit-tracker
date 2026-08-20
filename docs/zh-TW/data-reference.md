# 資料參考

## SQLite

目前資料庫 schema 為 v2。主要概念包括 profiles、projects、usage buckets、collection runs/slices、alert rules、dedup 與 notification history。所有識別、unique key 與查詢均包含 `profile_id`；相同 project/bucket 在不同 profile 不會衝突。

- 時間保存為 UTC epoch；quota day 永遠以 UTC 分日。
- Usage 同步按 UTC day/slice 分段；只有完整 slice 才在單一 transaction 內 reconcile。上游修正後消失的 row 會從完整 slice 移除。
- 部分頁失敗保留 checkpoint 診斷；恢復時安全重抓該 slice，避免把未提交頁誤標完整。
- SQLite 啟用 foreign keys、WAL 與 bounded busy timeout。integrity 失敗會停止寫入並保留原檔。
- v1→v2 migration 先建立一致性 snapshot 與 SHA-256 metadata；失敗 rollback，可重跑並可驗證還原。

## Project 隱私

公開資料使用安裝專屬 HMAC 產生的 `project_key`。原始 Project ID 只保存在私有欄位供明確的明碼匯出，不進入 repr、progress、notification 或一般 log。Project 顯示名稱與 ID 分離。

## Export schema v1

CSV 使用 UTF-8、固定欄位與明確 UTC；JSON 包含 `schema_version`、profile、範圍與 records。預設遮罩 organization/project identifiers；可選排除 ID，只有明確選擇才輸出原始 ID。所有文字欄位防試算表公式注入，檔案以 atomic replace 產生。穩定欄位與範例見 [export contract](../contracts/export-v1.md)。

模型目錄 schema 與來源見 [catalog contract](../contracts/catalog-v1.md) 及 [模型與價格](../model-pricing.md)。
