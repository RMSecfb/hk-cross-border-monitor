# 港股跨境交易監控（HK Monitor）

GitHub Actions 每 30 分鐘抓取全部港股（約 2,700 檔）的 1 分鐘 K 線（Yahoo，延遲約 15 分鐘），
依《港股交易風險監控即時系統_Excel模組》中「異常交易監控參數」「VCM與市場機制」的閾值產生黃/橙/紅警報，
結果以「日期時分秒」檔名存回本 repo 的 `data/`，並以 Streamlit 儀表板呈現。

```
GitHub Actions（雲端，可連 Yahoo / HKEX）
  ├─ 盤中監控  每 30 分鐘 → data/snapshot、data/alerts、data/status.json
  ├─ 盤後批次  16:42      → data/daily、data/report、data/stats、data/profile、config/universe.csv
  ├─ 每週股本  週六        → config/shares.csv
  └─ 補跑報告  手動        → 指定期間重算盤後報告
        │ commit 回 repo
        ▼
  本 repo ──► Streamlit 儀表板（streamlit_app.py）
          │     ├─ 看盤中／盤後警報、VCM、高風險排行、趨勢追蹤
          │     ├─ 下載 Excel／PDF 監控報告
          │     └─ 線上改參數、手動啟動盤中抓取（回寫 GitHub，需 Secrets）
          └─► 公司電腦 git pull 後由其他系統讀取 data/
```

## 一、第一次設定

1. 在公司 GitHub 組織建立**私有** repo，把本資料夾內容全部上傳（含 `.github/`）。
2. repo → **Settings → Actions → General → Workflow permissions** 選 **Read and write permissions**。
3. repo → **Actions** → 選「盤後批次」→ **Run workflow**，勾選「初始化」。
   約 10–20 分鐘，會下載監控名單（HKEX 證券名單、VCM 名單）與約半年日線，建立 `data/stats`。
4. （選用）Actions →「每週更新股本」→ Run workflow，建立 `config/shares.csv`，才會計算換手率。
5. 之後盤中、盤後會自動依排程執行。想在非交易時段測試：Actions →「盤中監控」→ Run workflow，勾選 force。

## 二、排程（香港/台北時間；cron 以 UTC 撰寫）

| 工作 | 時間 | 說明 |
| --- | --- | --- |
| 盤中監控 | 10:02、10:32、11:02、11:32、12:17、13:32、14:02、14:32、15:02、15:32、16:02 | 午休不跑；避開整點以減少 GitHub 排程延遲 |
| 盤後批次 | 16:42 | 以收盤資料全量重算；盤中失敗時以盤後報告為準 |
| 每週股本 | 週六 10:00 | 選用 |
| 補跑歷史盤後報告 | 手動 | 補跑指定期間（Yahoo 1 分鐘資料約保留 30 天），同時重建分鐘量基準 |

- 假期、颱風不需設定：程式以盈富基金（2800）當日有無行情判斷，無行情即結束。
- GitHub 排程可能延遲數分鐘到數十分鐘，偶爾跳過一次；下一次執行會重抓全天資料，不會漏算。
- 用量估算：每次盤中約 3–6 分鐘 × 11 次 × 22 日 + 盤後約 10 分鐘 × 22 日 ≈ 1,000–1,700 分鐘/月，
  在私有 repo 免費額度 2,000 分鐘內。實際時間請到 Actions 頁面查看，再調整 `.github/workflows/intraday.yml` 的 cron。

## 三、資料檔（`data/`）

| 路徑 | 更新 | 內容 |
| --- | --- | --- |
| `status.json` | 盤中每次 | 最近一次執行時間、資料截至時間、取得檔數、抓不到的代碼、新增警報數 |
| `snapshot/YYYY-MM-DD/snapshot_YYYYMMDD_HHMMSS.parquet` | 盤中每次 | 每檔一列：現價、前收、漲跌幅、5 分鐘漲跌、VCM 門檻與使用率、累計量、量比、偏離 MA、成交額 |
| `alerts/alerts_new_YYYYMMDD_HHMMSS.csv` | 盤中每次（有新警報時） | 本次新增警報 |
| `alerts/alerts_YYYYMMDD.csv` | 盤中每次 | 當日累計警報（`found_at` = 發現批次） |
| `report/eod_alerts_YYYYMMDD.csv`、`eod_summary_YYYYMMDD.csv` | 盤後 | 以收盤資料重算的全日警報（逐筆）與統計 |
| `report/eod_report_YYYYMMDD.csv` | 盤後 | 預警列表（Excel 格式）：預警ID、時間、等級、代碼、名稱、監控類型、指標值、閾值、觸發次數、風險分數、連續上榜、近5日上榜、操作建議 |
| `report/score_YYYYMMDD.csv` | 盤後 | 每檔每日風險分數、連續上榜天數（趨勢追蹤用） |
| `daily/YYYY/daily_YYYYMMDD.parquet` | 盤後 | 日線（每天一檔，只新增不覆寫） |
| `stats/stats_YYYYMMDD.csv` | 盤後 | 隔日盤中用的前收、MA、20 日均量、股本 |
| `profile/profile.csv` | 盤後 | 各 30 分鐘時段每分鐘均量（約近 20 日指數平均，供 3 分鐘量暴增、連續放量） |

警報欄位：`time, code, rule, level, value, detail, name, found_at`

repo 容量：快照保留 30 天（`config/settings.yaml` 的 `keep_snapshot_days`），每日新增約 2–3 MB，一年約數百 MB。
完整 1 分鐘資料不存進 repo（太大），盤中每次都重新抓取。

## 四、監控規則（閾值在 `config/rules.yaml`）

| 規則 | 依據 |
| --- | --- |
| 即時漲跌幅、偏離均線、開盤價異常 | 現價/開盤 vs 前收、MA20 |
| 1 分鐘、5 分鐘急漲急跌 | 1 分鐘 K 線逐分鐘回溯；5 分鐘以「5 分鐘前最後成交價」為參考 |
| 成交量暴增、3 分鐘量暴增、連續放量 | 20 日均量；同時段每分鐘均量 |
| VCM 觸發（模擬）、VCM 內部預警 | HKEX VCM 名單各股門檻（±5/10/15/20%）；預警 = 門檻 80%；排除各時段首 15 分鐘與尾市 20 分鐘；午市不沿用早市參考價 |
| 收市競價異常 | 收市價 vs 16:00 前最後價（盤後） |
| 換手率異常 | 需 `config/shares.csv` |

冷卻時間：同股票、同規則、同等級在冷卻期內只報一次；全日型規則同等級一天只報一次。
當日成交額低於 `min_turnover_hkd`（預設 100 萬港元）的冷門股不做短時規則；
3 分鐘量暴增、連續放量、偏離均線只監控「活躍股」（20 日平均成交額 ≥ `active_min_avg_turnover_hkd`，預設 1,000 萬港元），與 Excel 的適用範圍一致。
成交量類規則的基準來自 1 分鐘資料累積的各時段均量（`profile.csv`），因 Yahoo 1 分鐘量加總與日線量常不一致；
3 分鐘量暴增、連續放量需 profile 累積滿 5 個交易日（`min_profile_days`）才開始判斷；
成交量暴增在 profile 累積前暫以 20 日日線均量代替，量比會偏高。
修改閾值有兩種方式，擇一即可：在 Streamlit 的「參數」頁直接改（會寫回 GitHub 並留下修改人與原因，見第五節），
或直接編輯 `config/rules.yaml`。兩者的 commit 紀錄都是完整變更軌跡。

## 五、Streamlit 儀表板

1. 到 [share.streamlit.io](https://share.streamlit.io) → New app → 選本 repo、分支 `main`、主程式 `streamlit_app.py`。
2. App 設定中把可見範圍設為**僅受邀者**，並加入需要看的同仁（行情資料不可公開）。
3. repo 每次有新 commit，Streamlit 會自動更新；頁面上的「重新整理資料」按鈕可清除快取。
4. （選用）要在頁面上**修改參數並寫回 GitHub**、**手動啟動盤中抓取**，需設定 Secrets：
   - 到 GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token：
     Resource owner 選 RMSecfb，Repository access 只選本 repo，Permissions 設 **Contents: Read and write**、**Actions: Read and write**。
   - 到 Streamlit app → Settings → Secrets 貼上：
     ```toml
     GITHUB_TOKEN = "github_pat_..."
     GITHUB_REPO = "RMSecfb/hk-cross-border-monitor"
     ADMIN_PASSWORD = "自訂管理密碼"
     GROQ_API_KEY = "gsk_..."        # 選用，啟用監控摘要的「AI 分析」按鈕
     ```
   - 參數修改會寫回 `config/rules.yaml`、`config/actions.yaml`（保留註解），並在 `config/param_log.csv` 留下修改人、原值、新值、原因；
     commit 紀錄即為完整軌跡。下一次排程執行即套用新參數。

側欄顯示現在時間、最近一次盤中抓取、前次/下次排程與今日排程表。排程時間直接讀 `.github/workflows/*.yml`，
在 GitHub 上修改 cron 後頁面自動同步。

頁面：警報（含盤中報告下載）、VCM 監控（依 VCM 使用率由大到小排序）、今日高風險排行、個股查詢、
盤後報告（預警列表、可回溯日期、報告下載）、趨勢追蹤（連續異常標的、風險變化排行、近 10 日上榜次數）、參數。

**監控摘要**：警報頁與盤後報告頁的最上方（表格之前）各有一塊摘要，盤中與盤後各自獨立。
預設以規則組出當日統計敘述（不需任何 API）；若已設定 `GROQ_API_KEY`，可按「AI 分析」改用 Groq
生成帶判斷的分析摘要，按「規則摘要」可切回。AI 僅在按下按鈕時呼叫，不會在每次重新整理時自動計費。
模型不寫死：每次先向 Groq 查詢該金鑰目前可用的模型，依偏好順序挑選（`llama-3.3-70b-versatile` →
`openai/gpt-oss-120b` → `openai/gpt-oss-20b` → `llama-3.1-8b-instant`），避免 Groq 下架模型後功能失效；
失敗時會顯示 Groq 回傳的原因並自動退回規則摘要。

**報告下載**：警報頁與盤後報告頁各有「下載監控報告」。按「產生報告檔」後可下載 Excel 與 PDF，
兩者版面一致：**摘要文字 → 統計 → 規則分布 → 明細表**，摘要放在所有表格之前；
若先按過「AI 分析」，報告帶入的就是 AI 版摘要。產生器在 `report_export.py`，盤中盤後共用。

| | 盤中報告（警報頁） | 盤後報告（盤後報告頁） |
| --- | --- | --- |
| 資料 | 當日累計警報，依等級排序 | 預警列表，風險分數由高到低 |
| Excel | 全部等級、全部列、全部欄位 | 全部等級、全部列、全部欄位 |
| PDF | **只列紅燈與橙燈**的前 200 列 | 前 200 列的重點欄位 |

PDF 之所以縮減，是因為 A4 寬度有限：盤中黃燈多（常占半數以上），盤後 13 個欄位全放會把每列擠成多行。
完整內容一律在 Excel。盤中 Excel 資料量大（上萬列約需 10–20 秒），所以改為按鈕觸發，不在每次重新整理時重算。

風險分數 = 各監控項目當日最高等級加總（紅 40、橙 25、黃 10，上限 100）。
兩個門檻分開設定：`list_min_score`（預設 30）決定是否「上榜」進入盤後預警列表；
`streak_min_score`（預設 60，即至少紅+橙或兩個紅）決定是否進入趨勢追蹤的「連續異常標的」，
需連續 `streak_days`（預設 3）個交易日都達到。兩者都可在參數頁修改。

盤後預警列表排序：風險分數由高到低 → 同一檔股票排在一起 → 等級（紅/橙/黃）→ 監控類型。
操作建議依規則類別 × 等級設定於 `config/actions.yaml`。

## 六、公司電腦取用資料

公司電腦只需能連 GitHub：

```
git clone https://github.com/<組織>/<repo>.git
git pull          # 用 Windows 工作排程器每 30 分鐘執行一次
```

其他系統直接讀取本機 `data/` 底下的檔案即可（parquet 可用 Python pandas、Power BI 等讀取）。

## 七、限制

- Yahoo 港股報價延遲約 15 分鐘，非官方 API，雲端主機可能被限流或缺資料；`status.json` 的 `missing` 列出抓不到的代碼。
- VCM 為「以 1 分鐘收盤價模擬」，會漏掉分鐘內的瞬間尖峰，不等同交易所實際觸發紀錄。
- 掛撤單、自成交、帳戶盈虧等客戶層面規則需內部委託/成交資料，不在本 repo 範圍。
- 沽空異常尚未納入（HKEX 沽空報表格式待確認後加入）。
- 行情僅供內部風控使用，repo 與 Streamlit 都必須設為私有。

## 八、檔案結構

```
config/            settings.yaml、rules.yaml、actions.yaml、universe.csv（自動產生）、
                   shares.csv（選用）、param_log.csv（參數調整紀錄，首次儲存時建立）
src/               common.py、universe.py、rules.py、intraday.py、eod.py、report.py、
                   backfill.py、update_shares.py
.github/workflows/ intraday.yml、eod.yml、shares.yml、backfill.yml；.github/commit.sh
streamlit_app.py   儀表板
app_support.py     GitHub API（參數寫回、啟動 workflow）與排程時間解析
report_export.py   Excel／PDF 報告產生器（盤中、盤後共用）
requirements.txt   Streamlit 用；requirements-pipeline.txt  Actions 用
```
