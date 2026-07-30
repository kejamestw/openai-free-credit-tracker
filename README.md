# OpenAI Free Credit Tracker

> 本機優先的 OpenAI 每日免費 Token 額度、適用模型、Service Tier 與 API 成本監控工具。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![License](https://img.shields.io/badge/License-Apache--2.0-green)
![Status](https://img.shields.io/badge/status-release%20candidate-orange)

[English](README.en.md) | 繁體中文

## 功能

- 分別追蹤高階模型群組與 Mini／Nano 群組的每日免費 Token。
- 主圖只計入 Usage API 明確標示為 `incentivized-tier` 或已知 data-sharing incentive 的流量。
- 每日以 `00:00 UTC` 為日界線。
- 分開顯示免費群組用量、其他用量、目錄價格估算與 Costs API 實際費用。
- Costs API 失敗時仍顯示成功取得的 Usage 資料。
- Admin API Key 僅存在程式與頁面記憶體，不寫入磁碟或瀏覽器儲存空間。
- 本機服務只監聽 `127.0.0.1` 的隨機連接埠。

## 重要聲明

本專案是非官方工具，與 OpenAI 無隸屬或背書關係。免費額度、適用模型、價格與 API 行為可能變動，請以 OpenAI 官方文件及帳務後台為準。

請勿將 Admin API Key 貼到第三方網站、公開 Issue、截圖或 Git commit 中。建議使用專供驗收且可隨時撤銷的 Organization Owner Admin Key。

## Windows portable EXE

正式 Release 會同時提供：

- `OpenAI-Free-Credit-Tracker.exe`
- `SHA256SUMS.txt`

下載後先在 PowerShell 驗證雜湊：

```powershell
Get-FileHash .\OpenAI-Free-Credit-Tracker.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

確認值相同後執行 EXE。預設瀏覽器會開啟 `http://127.0.0.1:<random-port>`。關閉程式視窗或按 `Ctrl+C` 即會停止本機服務；重新啟動後必須重新輸入 Admin API Key。

v0.1.0 的正式 EXE 仍須通過 roadmap 中的乾淨 Windows 10／11 人工驗收後才能發布。

## 從原始碼執行

需要 Python 3.10 或以上版本：

```powershell
python -m pip install -e .
python -m quota_monitor
```

也可以在 Windows 雙擊 `scripts\run_windows.bat`。

查看版本或執行本機 smoke test：

```powershell
python -m quota_monitor --version
python -m quota_monitor --smoke-test
```

## 建立 Windows EXE

```bat
scripts\build_windows.bat
```

腳本會安裝開發相依套件、建立 one-file EXE，並執行封裝後的資源與 loopback smoke test。成功產物位於：

```text
dist\OpenAI-Free-Credit-Tracker.exe
```

任何安裝、建置或 smoke-test 步驟失敗時，腳本會回傳非零 exit code。

## 安全模型

- Admin API Key 只透過 `X-Admin-Key` request header 傳給 loopback server，再由 server 放入 OpenAI Authorization header。
- Key 不會放入 URL、Log、localStorage、sessionStorage、IndexedDB 或設定檔。
- 所有 HTTP 回應使用 `Cache-Control: no-store`。
- 靜態檔案只從封裝的 `web/` 目錄提供，traversal 請求會被拒絕。
- 本工具不會要求或保存 Project ID 與 Organization ID。

## 費用與用量說明

「目錄價格估算」依 `data/models.json` 的價格計算，與 Costs API 回報的「實際費用」是不同資料。估算公式為：

```text
成本 =
  非快取輸入 Token × Input 單價 / 1,000,000
+ 快取輸入 Token × Cached Input 單價 / 1,000,000
+ 輸出 Token × Output 單價 / 1,000,000
```

費用級距使用「1,000 Input Token + 1,000 Output Token」的標準案例：

- 低：低於 US$0.003
- 中：US$0.003 至未滿 US$0.012
- 高：US$0.012 以上

## 已知限制

- 需要 Organization Owner Admin API Key；一般 project key 不適用。
- Usage 與 Costs 資料可能延遲，推估免費用量不是官方餘額保證。
- v0.1.0 不保存設定、歷史、匯出資料或 Key。
- v0.1.0 聚焦 Windows；macOS、Linux、自動更新、系統匣、警示、多專案與多語言均不在本版範圍。
- 目前主要統計 Completions Usage；工具、微調與 Evals 不納入主要免費群組。

## 開發與驗證

```powershell
python -m pip install -r requirements-dev.txt
python scripts\validate_models.py
python -m pytest -q
node --check web\js\app.js
```

## 貢獻與安全通報

請先閱讀 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 與 [docs/security.md](docs/security.md)。請勿在公開 Issue 提供 Key、完整敏感回應或帳務資料。

## 授權

Apache License 2.0。詳見 [LICENSE](LICENSE)。
