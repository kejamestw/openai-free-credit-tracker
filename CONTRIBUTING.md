# Contributing

感謝協助改善 OpenAI Free Credit Tracker。

## 開發環境

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e .
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## 提交流程

1. Fork Repository。
2. 從 `main` 建立功能分支，例如 `feat/model-catalog`。
3. 修改程式並補上測試。
4. 執行完整驗證命令。
5. 確認沒有金鑰、Project ID、Organization ID、Email、帳務資訊或真實 Usage JSON。
6. 使用 Conventional Commit，例如 `fix: handle delayed costs response`。
7. 建立 Pull Request 並說明問題、作法與測試結果。

## 更新模型或價格

修改 `data/models.json`，並提供：

- 模型名稱與別名
- 免費額度群組
- Input、Cached Input、Output 單價
- 官方來源 URL
- 查證日期

請同步執行：

```powershell
python scripts/validate_models.py
python -m pytest -q
```

## 禁止提交

- `sk-admin-` 或 `sk-proj-` 金鑰
- 真實 Project／Organization ID
- 未匿名化的 API 回應
- 個人帳務截圖
- `.agent/`、`.agents/`、建置產物或本機設定
