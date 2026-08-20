# 發布準備狀態 / Release readiness

本文件區分「repository 可自動證明」與「必須由正式環境產生」的證據。可建置
或單元測試通過不等於已完成簽署、平台驗收或正式發布。

## 已自動化的證據

- Python unit/integration/contract/security/migration/packaging tests。
- model、locale、API/config/database/export/update contracts 與文件連結驗證。
- tracked/untracked source 與可達 Git history 的 secret/control-character audit。
- `pip-audit` 已知漏洞檢查、CycloneDX 1.6 SBOM、runtime/development license inventory。
- 365 天 × 100 專案合成資料的 ingest、30/365 天 query、export、backup、startup 與 integrity budget。
- 72 小時 accelerated scheduler simulation，含 retryable errors、partial result、sleep/resume 與 wall-clock regression；此結果明確不是 native 72-hour soak。
- Windows packaged resource/loopback/SQLite/export/clean-shutdown smoke；macOS/Linux 由原生 CI runner 建置。
- Stable workflow 對缺少 Windows/macOS/Linux/update signing material 採 fail-closed，tag publish 不重建 candidate bytes。

本機產生的報告位於被忽略的 `build/`：`dependency-audit.json`、
`OpenAI-Free-Credit-Tracker.cdx.json`、`license-inventory.json` 與
`quality-evidence.json`。正式 Release 必須重新由相同 commit 的受保護 CI 產生並附加。

## 尚需外部正式證據

| Gate | 所需證據 | 目前狀態 |
|---|---|---|
| Windows 10/11 lifecycle | clean VM install/upgrade/uninstall、Credential Manager、tray/toast、startup、update rollback | 待執行 |
| macOS | arm64/x86_64 app/DMG、Developer ID、hardened runtime、notarization/staple、Keychain/menu bar/notification | 待受保護 CI 與實機 |
| Linux | Ubuntu 22.04/24.04 AppImage/tar、Secret Service、tray/foreground、desktop integration | 待受保護 CI 與乾淨 VM |
| Artifact malware scan | 正式完整 artifact set、最新 definitions、fail-closed clean report | 待 candidate CI；本機 Defender 管理介面拒絕存取，不算通過 |
| Real OpenAI API | 使用獨立測試組織與 Admin Key 的分頁、401/403/429、資料完整性、組織驗證 | 待安全測試帳號 |
| Signed updater E2E | RC1 → RC2 download/install/health/commit/rollback/manual repair | 待兩個已簽 RC |
| Independent review | security threat model/findings 與雙語文件可執行性 | 待未參與開發者 |
| Native soak | 三平台各 72 小時，含 sleep/resume、網路切換與 UTC reset | 待持續執行 |
| Stable publication | protected settings、production signing secrets、approved tag與 GitHub Release | 待所有前述 P0 gates |

任何未完成項目都不得在 roadmap 驗收紀錄中標示為通過。候選版可發布為
GitHub prerelease，stable tag/Release 必須等所有 P0 gate 具備可追溯證據。
