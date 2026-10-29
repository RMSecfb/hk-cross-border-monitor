"""設定一致性：規則名在 rules.py / actions.yaml / rules.yaml / report.py / 參數頁之間不可漏接"""
import re
import sys
import yaml

sys.path.insert(0, "src")
from report import RULE_FMT                  # noqa: E402

cfg = yaml.safe_load(open("config/rules.yaml", encoding="utf-8"))
acts = yaml.safe_load(open("config/actions.yaml", encoding="utf-8"))
src = open("src/rules.py", encoding="utf-8").read()
app = open("streamlit_app.py", encoding="utf-8").read()

emitted = set(re.findall(r'emit\([^,]+,[^,]+,\s*"([^"]+)"', src))
act_rules = {r for g in acts.values() for r in g["rules"]}
cd = set(cfg["cooldown_min"])
# 參數頁可編輯到的規則：RULE_NAME 的值 + EXTRA_COOLDOWN
page = set(re.findall(r'"[\w.]+": "([^"]+)"', app.split("RULE_NAME = {")[1].split("}")[0]))
page |= set(re.findall(r'"([^"]+)"', app.split("EXTRA_COOLDOWN = [")[1].split("]")[0]))

checks = [
    ("規則共 12 條（VCM 兩條已合併為一條；2026-10-29 加入冬眠股異動）", len(emitted) == 12),
    ("actions.yaml 涵蓋所有規則", not emitted - act_rules),
    ("actions.yaml 無已移除的規則", not act_rules - emitted),
    ("cooldown_min 涵蓋所有規則", not emitted - cd),
    ("cooldown_min 無已移除的規則", not cd - emitted),
    ("report.RULE_FMT 涵蓋所有規則", not emitted - set(RULE_FMT)),
    ("report.RULE_FMT 無已移除的規則", not set(RULE_FMT) - emitted),
    ("參數頁可編輯到所有規則的冷卻時間", not emitted - page),
    ("rules.yaml 不再有已淘汰的鍵",
     not ({"vcm_early_ratio", "cas_dev"} & set(cfg))),
    ("3 分鐘量暴增的四道閘門齊備",
     all(k in cfg["vol_3m_gate"] for k in ("hist_min", "today_min", "permin_min", "traded_min"))),
    ("閘門的 today_min 與分級黃燈同值（語意一致）",
     cfg["vol_3m_gate"]["today_min"] == cfg["vol_3m"]["A"]["黃"]),
    ("雙軌參數結構完整",
     all("A" in cfg[k] and "B" in cfg[k] and "min_turnover" in cfg[k]["A"]
         for k in ("vol_ratio", "vol_3m"))),
    ("新參數齊備",
     all(k in cfg for k in ("pool_split_turnover", "min_hist_days", "min_hist_days_cum",
                            "cas_dev_main", "cas_dev_thin", "cas_pool_split_turnover",
                            "cas_max_stale_min", "cas_max_dev", "vcm_ratio",
                            "max_prev_gap_min", "max_ref_gap_5m_min", "open_window_min", "vol_3m_gate"))),
    ("門檻排序 黃 ≤ 橙 ≤ 紅",
     all(v["黃"] <= v["橙"] <= v["紅"]
         for k in ("day_change", "chg_1m", "chg_5m", "ma_dev", "open_gap", "turnover",
                   "vol_streak", "cas_dev_main", "cas_dev_thin", "vcm_ratio")
         for v in [cfg[k]])
     and all(cfg[k][t]["黃"] <= cfg[k][t]["橙"] <= cfg[k][t]["紅"]
             for k in ("vol_ratio", "vol_3m") for t in ("A", "B"))),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
