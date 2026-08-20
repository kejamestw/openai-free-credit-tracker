# 安全與隱私

本機 HTTP server 只監聽 IPv4 loopback 隨機埠，驗證精確 Host/port、Origin 與 `Sec-Fetch-Site`，拒絕 DNS rebinding、跨站要求、traversal、任意 URL/檔案 deep link。回應使用 no-store、CSP、nosniff、frame/referrer 限制與穩定錯誤 envelope。

單次 Key 僅存在 browser/application request memory。背景 Key 只能進入 Windows Credential Manager、macOS Keychain 或 Linux Secret Service，設定與 DB 只保存 opaque reference；無安全 backend 時 fail closed。Key 不得出現在 URL、argv、log、exception、notification、temp、backup、export、fixtures 或 repository。

更新使用 canonical JSON + Ed25519；artifact 再驗證 size 與 SHA-256。CI 最小權限、Action 固定 commit、dependency audit、secret/history scan、SBOM 與 artifact manifest 都是發布門檻。各平台正式發布仍需對應平台簽署；SHA-256 不能取代可信簽署來源。

完整邊界、殘餘風險與緩解見 [threat model](../threat-model.md)。漏洞請使用 GitHub Security 的 private vulnerability reporting；不要在公開 Issue 放 exploit、Key、原始 API body、組織識別或帳務資料。疑似洩漏時立即撤銷 Key。
