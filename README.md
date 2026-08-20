# OpenAI Free Credit Tracker

> 本機優先的 OpenAI Admin Usage、免費額度、成本、歷史與通知工具。

![Python](https://img.shields.io/badge/Python-3.10--3.14-3776AB)
![License](https://img.shields.io/badge/License-Apache--2.0-green)
![Status](https://img.shields.io/badge/status-v1%20candidate-orange)

[English](README.en.md) | 繁體中文 · [完整文件](docs/zh-TW/quick-start.md) · [穩定契約](docs/contracts/README.md) · [Roadmap](docs/roadmap/README.md)

## 這個專案做什麼

- 依 `00:00 UTC` 日界線統計 OpenAI complimentary usage，分開顯示 Usage、Costs API 實際成本與模型目錄牌價估算。
- 支援多 profile、多專案、歷史同步、CSV／JSON 匯出、retention、資料完整性與可復原 migration。
- 背景 scheduler 具有最小間隔、non-overlap、sleep/resume、429/5xx backoff、auth fail-stop 與 stale-data 行為。
- 額度通知只在新跨越門檻時發送，並以 profile/rule/group/project/UTC day 持久化去重。
- 正式支援繁體中文與英文；主要介面可用鍵盤操作、深色模式、reduced motion、200% 縮放與 screen-reader labels。
- Windows、macOS、Linux 共用核心與版本化 App API；平台 path、credential、startup、tray、notification、single-instance 與 updater 都由 adapter 隔離。

本專案是非官方工具，與 OpenAI 無隸屬或背書關係。免費資格、模型、價格與 API 行為可能變動；帳務判斷請以 OpenAI 官方資料為準。

## 安全預設

單次查詢的 Admin API Key 只存在該次要求的記憶體，完成後欄位清空；不進 URL、config、SQLite、browser storage、log、exception、notification、backup 或 export。背景同步只有在使用者明確同意且作業系統安全 credential backend 可用時才啟用；無 backend 時 fail closed。

本機 server 只監聽 `127.0.0.1` 隨機埠，並驗證精確 Host/port、Origin、fetch-site 與靜態路徑。公開錯誤使用安全 envelope 和 request ID。不同 profile 的 credential、資料、sync、alert 與 export 均隔離。

不要把 Admin API Key、原始 API body、Organization/Project ID 或帳務資料貼到 Issue、截圖或 Git。疑似洩漏時立即撤銷 Key。詳見 [安全與隱私](docs/zh-TW/security.md) 和 [threat model](docs/threat-model.md)。

## 從原始碼執行

需要 Python 3.10–3.14：

```powershell
python -m pip install -e .
python -m quota_monitor
```

Windows 也可執行 `scripts\run_windows.bat`。常用診斷：

```powershell
python -m quota_monitor --version
python -m quota_monitor --smoke-test
python -m quota_monitor --no-browser
python -m quota_monitor --config-path --data-path --log-path
```

只使用程式開啟的 `http://127.0.0.1:<random-port>`。不要直接開 `web/index.html` 或改由遠端 dev server 載入。

## 安裝與發布產物

候選版 pipeline 在每個原生 runner 建立：

- Windows portable EXE 與 per-user installer。
- macOS 原生 `.app`／DMG。
- Linux tarball 與 AppImage。
- `SHA256SUMS.txt`、platform artifact manifest、CycloneDX SBOM 與簽署 update manifest。

正式 stable Release 必須先完成對應平台簽署、macOS hardened runtime/notarization、乾淨 VM 安裝/升級/rollback、三平台 72 小時 soak 與獨立安全/文件驗收。沒有真實證據不會把門檻標成完成。下載正式產物前請依 [Quick Start](docs/zh-TW/quick-start.md) 驗證來源與雜湊。

Windows 本機 build：

```bat
scripts\build_windows.bat
scripts\build_installer_windows.bat
```

## 開發與驗證

```powershell
python -m pip install -e . -r requirements-dev.txt
python scripts/validate_models.py
python scripts/validate_locales.py
python scripts/validate_contracts.py
python scripts/audit_repository.py
python scripts/audit_dependencies.py --output build/dependency-audit.json
python scripts/generate_sbom.py --output build/OpenAI-Free-Credit-Tracker.cdx.json
python -m pytest -q --basetemp build/pytest-local
node --check web/js/domain.js
node --check web/js/app.js
node --test tests/frontend_domain.test.cjs
```

CI 在 Windows、macOS、Linux 與 Python 3.10/3.14 執行核心/契約/安全測試。Release workflow 使用原生 runner，Actions 固定完整 commit，candidate 與正式 tag 發布分離。

## 文件

- [快速開始](docs/zh-TW/quick-start.md)
- [設定參考](docs/zh-TW/config-reference.md)
- [資料與匯出](docs/zh-TW/data-reference.md)
- [備份、復原、更新與移除](docs/zh-TW/operations.md)
- [疑難排除](docs/zh-TW/troubleshooting.md)
- [維護者與 Release 指南](docs/zh-TW/maintainer-guide.md)
- [貢獻](CONTRIBUTING.md) · [漏洞通報](SECURITY.md)

## 授權

Apache License 2.0，詳見 [LICENSE](LICENSE)。
