# 維運、備份與復原

## 日常檢查

- UI 的最後成功同步、失敗 slice、完整性與資料新鮮度必須一起判讀。
- `--data-path` 可找出資料目錄；不要在程式執行中直接複製 WAL 資料庫。
- 使用內建 backup/integrity/export 操作或對應 App API；備份透過 SQLite Backup API 建立一致快照。

## Backup

1. 暫停背景監控或使用內建 backup。
2. 建立 config backup 與 SQLite snapshot。
3. 保存產物旁的 metadata（schema from/to、UTC 時間、SHA-256）。
4. 將備份放在有適當權限的位置；匯出可能包含營運資料，不等於公開檔案。

## Restore

1. 完全退出 tray/server/scheduler，確認沒有第二個 instance。
2. 先備份目前檔案。
3. 使用內建 restore 指定 snapshot；程式會先驗證 SHA-256 與 SQLite integrity，再 atomic replace。
4. 啟動後再次執行 integrity check，比對 profiles、projects、usage totals、alerts 與 credential references。credential 本體由作業系統管理，不包含在 DB snapshot。

不要用舊版直接開啟較新的 forward-only DB；應安裝支援該 schema 的版本或還原與該舊版相容的完整備份。

執行中的 Web UI 刻意不提供 restore 入口。桌面 scheduler 仍可能持有資料庫，僅靠 UI 二次確認無法證明 atomic replace 已處於離線安全狀態。請先完全退出，再執行 `openai-free-credit-tracker restore --source <backup> --confirm-restore`；operations command 會取得共用 runtime lock，偵測到其他 instance 時即 fail closed。UI 只會建立並回報受管理的 backup name。

## 更新與 rollback

更新必須依序通過 HTTPS host allowlist、manifest Ed25519 簽章、channel/SemVer/expiry/platform、artifact size/SHA-256、磁碟空間與使用者同意。安裝前保留舊 artifact；新版本健康檢查失敗即 rollback。journal 可辨識中斷階段，不會同時破壞新舊版本。config、DB 與 credential references 不由 manifest 修改。

若 updater 顯示人工復原：完全退出程式，保存 journal/log/request ID，以既有 installer 重新安裝前一個已驗證版本，必要時依上節還原相容備份。

## 移除與清除

一般 uninstall 只移除程式、捷徑與 startup entry，保留 config、DB、exports 與 credential。刪除 credential、刪除歷史、刪除 profile、清除所有資料是不同的破壞性操作；先提供 backup/export，顯示範圍並要求二次確認。
