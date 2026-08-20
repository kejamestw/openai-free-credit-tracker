# 設定參考（schema v1）

設定只保存非敏感資料，採 UTF-8 JSON、atomic replace 與最近可用備份。未知 optional 欄位會 round-trip 保留；錯誤、未來 schema 或疑似秘密內容不會直接載入，原檔則保留供診斷。Admin API Key 永遠不屬於 config。

| 欄位 | 預設 | 限制／敏感性 | 套用 |
|---|---:|---|---|
| `schema_version` | `1` | 必須為 1；非敏感 | 啟動時 |
| `ui.language` | `zh-TW` | BCP-47；正式支援 `zh-TW`、`en` | 即時 |
| `ui.open_browser_on_start` | `true` | boolean | 下次啟動 |
| `network.request_timeout_seconds` | `45` | 5–300 | 即時，新要求 |
| `updates.channel` | 預發行版為 `beta`；正式版為 `stable` | `stable` 或 `beta` | 下次更新檢查；UI 會標示是否需重啟 |
| `updates.check_on_start` | `true` | boolean | 下次啟動 |
| `history.retention_days` | `null` | `null` 或 1–3650；清理前預覽 cutoff/筆數 | 即時 |
| `monitoring.enabled` | `false` | 需安全 credential backend | 即時 |
| `monitoring.interval_seconds` | `900` | 至少 300 | 即時 |
| `monitoring.freshness_threshold_seconds` | `1800` | 不得小於 interval | 即時 |
| `profiles.active_profile_id` | `null` | opaque 本機 ID；非 Key | 即時 |
| `startup.enabled` | `false` | 預設關閉；由平台 startup adapter 管理 | 即時或安全 rollback |

實際位置可執行 `openai-free-credit-tracker --config-path` 查詢。Windows config 位於 Roaming AppData，data/cache/log 位於 Local AppData；macOS 使用 Application Support/Caches/Logs；Linux 遵循 XDG config/data/cache/state。可用 `OPENAI_CREDIT_TRACKER_{CONFIG,DATA,CACHE,LOG}_DIR` 指定絕對路徑，拒絕相對路徑。

設定損壞時，程式優先讀取最近的有效 backup，否則使用安全預設值；不會覆寫原始損壞檔。UI 會顯示來源、警告與路徑。

設定頁可先預覽「恢復預設」，列出每個將變更的可編輯欄位，並在沿用既有 atomic config replace 前要求第二次確認。此操作會保留目前設定檔選擇、forward-compatible 未知欄位、作業系統安全認證與完整歷史資料庫。
