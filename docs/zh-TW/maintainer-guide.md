# 維護者指南

## 變更契約

先更新 `docs/contracts` 與 golden/consumer tests。v1.x 只可新增 optional 設定/API 欄位；不得改變既有欄位語意。DB migration forward-only，寫入前必須建立有 SHA metadata 的一致性 backup。Export/catalog schema 變更需新 schema version；catalog content 可在 schema 不變時獨立更新。

匿名化上游 fixture 時移除 Key、organization/project 原 ID、帳務與 request metadata；未知 optional 欄位應保留 parser 相容性測試。價格、資格或額度更新必須附來源、verified/effective date 並通過 `validate_models.py`。

## 驗證

```powershell
python -m pip install -e . -r requirements-dev.txt
python scripts/validate_models.py
python scripts/validate_locales.py
python scripts/validate_contracts.py
python scripts/audit_repository.py
python scripts/audit_dependencies.py --output build/dependency-audit.json
python scripts/generate_sbom.py --output build/OpenAI-Free-Credit-Tracker.cdx.json
python -m pytest -q --basetemp build/pytest-release
node --check web/js/domain.js
node --check web/js/app.js
node --test tests/frontend_domain.test.cjs
```

## Release

1. Freeze 功能，只接受 blocker/security/data-loss/compatibility/docs 修正。
2. 由 workflow_dispatch 建立未發布 candidate；不要先建立 tag。
3. 三個原生 runner 完成 tests、packaged smoke、架構/resource 檢查。執行 dependency/license/secret audit，產生 SBOM、checksums、artifact manifest。
4. Windows/macOS/Linux 依公開 signing policy 簽署；macOS 必須 hardened runtime + notarization，Linux/Windows 的信任根也必須在 release notes 說明。沒有正式 secret 時 stable job fail closed。
5. 在乾淨 Win10/11、受支援 macOS 與 Ubuntu 22.04/24.04 完成安裝、升級、移除、credential/tray/notification/update rollback。完成 72 小時 soak，附機器、時間與 log SHA。
6. RC1 findings 修正後發布 RC2，從 RC1 演練更新與 rollback；保留無 P0/P1 的觀察期。
7. 所有門檻有證據後，確認版本/tag/UI/manifest/assets 一致，再建立 protected signed tag 與正式 Release。

事件發生時可撤回 manifest/channel 或 Release asset，但不要重寫已發布 tag。保存 provenance 與診斷，依兼容性政策發布 patch；疑似 signing key 外洩立即輪替 keyring 並撤銷受影響 channel。
