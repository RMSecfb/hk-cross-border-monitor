"""階段 2（VCM 合併、CAS 三閥門、換手率改盤後、每批次取 max）合成測試"""
import sys
import pandas as pd

sys.path.insert(0, "src")
from common import load_yaml                       # noqa: E402
from rules import run_rules                        # noqa: E402

DAY = pd.Timestamp("2026-10-27")
cfg = load_yaml("rules.yaml")
checks = []


def bar(code, hhmm, close, vol, open_=None):
    t = DAY + pd.Timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[3:]))
    o = close if open_ is None else open_
    return dict(code=code, time=t, open=o, high=max(o, close), low=min(o, close),
                close=close, volume=vol)


def mkstats(codes, **kw):
    d = dict(prev_close=100.0, ma=100.0, avg_vol=1_000_000, avg_turnover=1e9,
             halted=False, shares=1e9)
    d.update(kw)
    return pd.DataFrame({"code": codes, **{k: [v] * len(codes) if not isinstance(v, list) else v
                                           for k, v in d.items()}})


def run(bars, stats, uni, **kw):
    return run_rules(bars, stats, uni, cfg, **kw)


def idx(a):
    return {(int(r.code), r.rule): r for r in a.itertuples()}


# ---------- 1. VCM：合併為一條、三級 ----------
rows = []
for c, pct in [(11, 4.2), (12, 4.7), (13, 5.5)]:          # 門檻 ±5% → 84% / 94% / 110%
    rows += [bar(c, "10:00", 100, 500_000), bar(c, "10:05", 100 + pct, 500_000)]
bars = pd.DataFrame(rows)
codes = [11, 12, 13]
uni = pd.DataFrame({"code": codes, "name": "X", "vcm_pct": 5.0})
a = run(bars, mkstats(codes), uni)
g = idx(a)
checks += [
    ("VCM 只剩一條規則名 VCM預警", set(a.loc[a["rule"].str.startswith("VCM"), "rule"]) == {"VCM預警"}),
    ("VCM 84% → 黃", g[(11, "VCM預警")].level == "黃"),
    ("VCM 94% → 橙", g[(12, "VCM預警")].level == "橙"),
    ("VCM 110% → 紅", g[(13, "VCM預警")].level == "紅"),
]

# ---------- 2. 換手率：盤中不算、盤後才算 ----------
rows = [bar(21, "10:00", 100, 300_000_000), bar(21, "10:01", 100, 300_000_000)]
bars = pd.DataFrame(rows)
uni1 = pd.DataFrame({"code": [21], "name": "X", "vcm_pct": [None]})
a_intra = run(bars, mkstats([21]), uni1)
a_eod = run(bars, mkstats([21]), uni1,
            daily_close=pd.Series({21: 100.0}), daily_volume=pd.Series({21: 500_000_000.0}))
checks.append(("沒有同時點基準時不產生成交量暴增",
               "成交量暴增" not in set(a_intra["rule"]) and "成交量暴增" not in set(a_eod["rule"])))
checks += [
    ("換手率盤中不計算", "換手率異常" not in set(a_intra["rule"])),
    ("換手率盤後計算", "換手率異常" in set(a_eod["rule"])),
    ("換手率分子用日線量（5e8/1e9=50% → 紅）",
     idx(a_eod)[(21, "換手率異常")].level == "紅" and abs(idx(a_eod)[(21, "換手率異常")].value - 50) < 0.01),
]

# ---------- 3. CAS 三閥門 ----------
def cas_case(code, ref_px, close_px, cas_vol, day_vol, avg_turnover, last_hhmm="15:58"):
    rows = [bar(code, "09:30", ref_px, day_vol - cas_vol), bar(code, last_hhmm, ref_px, 1)]
    if cas_vol:
        rows.append(bar(code, "16:08", close_px, cas_vol))
    return rows, avg_turnover


specs = [
    # (代碼, ref, 收盤, CAS量, 全日量, 20日均成交額, 最後成交時間, 期望等級, 說明)
    (31, 100.0, 102.0, 10_000, 1_000_000, 5e8,  "15:58", "橙", "一般股 +2.0% → 橙"),
    (32, 100.0, 103.0, 10_000, 1_000_000, 5e8,  "15:58", "紅", "一般股 +3.0% → 紅"),
    (33, 100.0, 103.0, 10_000, 1_000_000, 5e8,  "15:20", "橙", "一般股 +3.0% 但最後成交距 16:00 40 分 → 降為橙"),
    (34, 100.0, 106.0, 10_000, 1_000_000, 5e8,  "15:58", None, "偏離 6% > 5% 結構上限 → 資料異常不報"),
    (35, 100.0, 103.5, 10_000, 1_000_000, 1e6,  "15:58", "黃", "冷門股 +3.5%（未達 4%）→ 黃"),
    (36, 100.0, 104.5, 50, 1_000_000, 1e6,  "15:58", "黃", "冷門股 +4.5% 但 CAS 僅 5,225 元 → 只給黃"),
    (37, 100.0, 104.5, 300, 1_000_000, 1e6,  "15:58", "橙", "冷門股 +4.5% 且 CAS 31,350 元（1–5 萬）→ 橙"),
    (38, 100.0, 104.5, 1_000, 1_000_000, 1e6,  "15:58", "紅", "冷門股 +4.5% 且 CAS 104,500 元（≥5 萬）→ 紅"),
    (39, 100.0, 104.5, 400, 1_000, 1e6,  "15:58", "紅", "冷門股 +4.5%，CAS 金額僅 4.2 萬但占全日量 40% → 紅"),
]
rows, avg_map = [], {}
for code, ref, cl, cv, dv, at, lt, _, _ in specs:
    r, a_t = cas_case(code, ref, cl, cv, dv, at, lt)
    rows += r
    avg_map[code] = a_t
bars = pd.DataFrame(rows)
codes = [s[0] for s in specs]
uni = pd.DataFrame({"code": codes, "name": "X", "vcm_pct": [None] * len(codes)})
st = mkstats(codes, avg_turnover=[avg_map[c] for c in codes])
a = run(bars, st, uni, daily_close=pd.Series({s[0]: s[2] for s in specs}))
g = idx(a)
for code, _, _, _, _, _, _, want, desc in specs:
    got = g[(code, "收市競價異常")].level if (code, "收市競價異常") in g else None
    checks.append((f"CAS {desc} → {want or '不報'}", got == want))

# ---------- 4. 每批次每檔取 max 一筆 ----------
base41 = pd.DataFrame([{"code": 41, "m": m, "avg1": 1000.0, "avg3": 3000.0, "avgcum": 10000.0,
                        "n_short": 5, "n_long": 20} for m in range(600, 620)])
rows = [bar(41, f"10:{m:02d}", 100, 100_000) for m in range(0, 20)]
bars = pd.DataFrame(rows)
uni = pd.DataFrame({"code": [41], "name": "X", "vcm_pct": [None]})
a = run(bars, mkstats([41]), uni, baseline=base41)
for r in ["3分鐘量暴增", "連續放量"]:
    checks.append((f"{r} 每檔只留一筆", (a["rule"] == r).sum() <= 1))

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
