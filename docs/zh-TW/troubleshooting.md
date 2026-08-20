# 疑難排除

## 無法啟動或瀏覽器連不上

- 從應用程式本身開啟的 `http://127.0.0.1:<port>` 操作；不要開本機 HTML、固定舊 port 或遠端 dev server。
- 確認沒有既有 instance。若程式異常中止，instance lock 會在確認 owner 已消失後復原；不要手動刪除仍在使用的 lock。
- 執行 `--smoke-test` 驗證 bundled resources 與 loopback bind；此命令不建立或讀取使用者資料。
- `--config-path`、`--data-path`、`--log-path` 可找出診斷位置。

## 401／403／429／5xx／timeout

- 401：重新驗證該 profile 的 Admin Key；不要改用另一個 profile 的 credential。
- 403：確認 Organization Owner 權限，或瀏覽器是否從正確 loopback origin 操作。
- 429：scheduler 會 backoff；不要把 interval 調到低於 300 秒。
- 5xx／timeout／offline：完整 slice 不受影響；失敗 slice 下次安全重抓。Costs 失敗不會抹掉成功的 Usage。

## Credential、tray 與通知

- Windows Credential Manager/macOS Keychain 鎖定時解鎖後再試。
- Linux 需要可用的 DBus Secret Service；沒有時以 foreground 單次模式使用，不會用文字檔 fallback。
- tray/notification 不可用或權限被拒絕時，核心查詢與歷史仍可用；程式不會反覆要求通知權限。

## DB busy／corrupt／磁碟滿

不要刪除原 DB。先退出程式、保留 log/request ID，執行 integrity check。corrupt 會進入唯讀/停止寫入狀態；依 [維運文件](operations.md) 驗證 backup SHA 後還原。磁碟滿時先釋放目標磁碟空間，確認 atomic temp/backup 仍存在，再重試。

## stale data 或重複通知

查看最後成功同步與 incomplete slices。stale 資料不送出額度安全通知；notification dedup 以 profile/rule/group/project/UTC day 持久化。如果重複，保存匿名化 diagnostics 與版本，不要貼 DB 或原始識別到公開 Issue。
