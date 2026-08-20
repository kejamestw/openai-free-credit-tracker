# 快速開始

OpenAI 免費額度追蹤器是本機優先的桌面工具。它把 OpenAI Admin Usage、Costs 與本專案的模型目錄分開呈現，不把牌價估算說成帳單。

## 安裝與第一次查詢

1. 從 GitHub Release 下載符合平台與架構的產物，以及 `SHA256SUMS.txt`、SBOM 與簽署 manifest。
2. 先核對 SHA-256；macOS 另以 `codesign`／`spctl` 驗證，Windows 若提供 Authenticode 則以「數位簽章」頁籤驗證。不要繞過 Gatekeeper 或系統安全警告。
3. 啟動後只使用程式開啟的 `http://127.0.0.1:<隨機連接埠>`。不要直接打開 `web/index.html`。
4. 單次查詢可在儀表板輸入 Organization Admin API Key。Key 只供該次要求使用，完成後欄位會清空。
5. 需要背景同步時，建立 profile 並明確同意將 Key 存入 Windows Credential Manager、macOS Keychain 或 Linux Secret Service。沒有安全 backend 時只允許前景單次查詢。

從原始碼執行：

```powershell
python -m pip install -e .
python -m quota_monitor
python -m quota_monitor --version
python -m quota_monitor --smoke-test
```

`--no-browser` 可停用自動開啟；`--config-path`、`--data-path`、`--log-path` 只顯示路徑而不建立資料夾。

## 資料語意

- 額度日固定為 `00:00 UTC` 到隔日 `00:00 UTC`，本地顯示不會改變歸屬。
- `Usage` 是 token 使用；`Costs` 是上游成本；`catalog_list_price_estimate` 只是依目錄計算的估價。
- stale 或不完整資料會明確標記，不會觸發「安全」類額度通知。
- 專案與 profile ID 預設使用不可逆遮罩；匯出明碼必須明確選擇。

下一步：[設定參考](config-reference.md)、[資料參考](data-reference.md)、[維運與復原](operations.md)、[疑難排除](troubleshooting.md)。
