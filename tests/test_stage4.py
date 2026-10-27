"""階段 4（同時點分母與自動退回）合成測試"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "src")
from bars_store import build_baseline        # noqa: E402
from common import load_yaml                 # noqa: E402
from rules import run_rules                  # noqa: E402

cfg = load_yaml("rules.yaml")
checks = []
DAY = pd.Timestamp("2026-10-27")
HIST = [pd.Timestamp(f"2026-10-{d:02d}") for d in (19, 20, 21, 22, 23)]


def hist_rows(code, per_min, minutes, days=HIST):
    return [dict(code=code, m=m, volume=per_min, day=d) for d in days for m in minutes]


# 歷史：10:00–10:09 每分鐘 1,000 股；只有 10:05 這一分鐘平時就放量 10,000 股
MIN = list(range(600, 610))
rows = []
for c in (61, 62):
    for d in HIST:
        for m in MIN:
            rows.append(dict(code=c, m=m, volume=10_000 if m == 605 else 1_000, day=d))
hist = pd.DataFrame(rows)
bl = build_baseline(hist)

b61 = bl[(bl["code"] == 61)].set_index("m")
checks += [
    ("avg1：平常分鐘 = 1,000", abs(b61.loc[600, "avg1"] - 1000) < 1),
    ("avg1：10:05 的時點效應被保留 = 10,000", abs(b61.loc[605, "avg1"] - 10000) < 1),
    ("avg3：結束於 10:05 的 3 分鐘 = 1,000+1,000+10,000", abs(b61.loc[605, "avg3"] - 12000) < 1),
    ("avgcum：10:09 累計 = 9×1,000 + 10,000", abs(b61.loc[609, "avgcum"] - 19000) < 1),
]


def bar(c, m, px, v):
    t = DAY.normalize() + pd.Timedelta(minutes=int(m))
    return dict(code=c, time=t, open=px, high=px, low=px, close=px, volume=v)


# 今日：61 在 10:05 也是 10,000（符合該時點常態，不應判為暴增）
#       62 在 10:02 衝到 10,000（該時點常態僅 1,000 → 3 分鐘量 12,000 / 3,000 = 4 倍）
rows = []
for m in MIN:
    rows.append(bar(61, m, 100, 10_000 if m == 605 else 1_000))
    rows.append(bar(62, m, 100, 10_000 if m == 602 else 1_000))
bars = pd.DataFrame(rows)
codes = [61, 62]
uni = pd.DataFrame({"code": codes, "name": "X", "vcm_pct": [None] * 2})
stats = pd.DataFrame({"code": codes, "prev_close": 100.0, "ma": 100.0, "avg_vol": 1e5,
                      "avg_turnover": 5e8, "halted": False})

a = run_rules(bars, stats, uni, cfg, baseline=bl)
v3 = a[a["rule"] == "3分鐘量暴增"].set_index("code")
checks += [
    ("同時點分母：10:05 的常態放量不誤報", 61 not in v3.index),
    ("同時點分母：10:02 的異常放量有報", 62 in v3.index),
    ("3 分鐘量暴增倍數 = 12,000 / 3,000 = 4 倍",
     62 in v3.index and abs(v3.loc[62, "value"] - 4.0) < 0.05),
    ("3 分鐘量暴增每檔只一筆（t*）", (a["rule"] == "3分鐘量暴增").sum() <= len(codes)),
    ("說明欄指出用的是同時點分母",
     62 in v3.index and "過去 5 日同時點" in v3.loc[62, "detail"]),
    ("說明欄不再出現「歷史不足」的退回字樣", not a["detail"].str.contains("歷史不足").any()),
]

# 成交量暴增：今日 10:09 累計 = 9×1,000+10,000 = 19,000（61）與 19,000（62）→ 1 倍，不應觸發
vr = a[a["rule"] == "成交量暴增"]
checks.append(("成交量暴增：與歷史同時點累計相同 → 不觸發", vr.empty))

# 成交量暴增需累積 min_hist_days_cum 天；只有 5 天時應退回時段內插法
bars5 = bars.copy()
bars5["volume"] = bars5["volume"] * 5
a5 = run_rules(bars5, stats, uni, cfg, baseline=bl)
checks.append((f"歷史只有 5 天 → 成交量暴增不計算（需 {cfg['min_hist_days_cum']} 天）",
               "成交量暴增" not in set(a5["rule"])))

# 12 天歷史 → 啟用同時點累計分母
H12 = list(pd.date_range("2026-10-06", periods=12, freq="B"))
hist12 = pd.DataFrame([dict(code=c, m=m, volume=10_000 if m == 605 else 1_000, day=d)
                       for c in codes for d in H12 for m in MIN])
bl12 = build_baseline(hist12)
a12 = run_rules(bars5, stats, uni, cfg, baseline=bl12)
vr12 = a12[a12["rule"] == "成交量暴增"].set_index("code")
checks += [
    ("12 天歷史：全日量 ×5 → 觸發", len(vr12) == 2),
    ("成交量暴增倍數 ≈ 5", len(vr12) == 2 and abs(vr12["value"].max() - 5.0) < 0.05),
    ("說明欄指出用的是同時點累計分母",
     len(vr12) == 2 and all("過去 20 日同時點" in x for x in vr12["detail"])),
]

# 無基準檔 → 三個量能指標都不產生警報
a0 = run_rules(bars5, stats, uni, cfg, baseline=None)
checks.append(("無基準檔 → 量能指標一筆都不報",
               not ({"成交量暴增", "3分鐘量暴增", "連續放量"} & set(a0["rule"]))))

# 基準檔為空 DataFrame 也要能退回，不可拋例外
try:
    run_rules(bars5, stats, uni, cfg, baseline=pd.DataFrame(columns=["code", "m", "avg1", "avg3", "avgcum"]))
    checks.append(("空基準檔不拋例外", True))
except Exception as e:
    checks.append((f"空基準檔不拋例外（{e}）", False))

# 整日停牌的日子不應拉低分母
hist2 = pd.concat([hist, pd.DataFrame([dict(code=63, m=m, volume=1_000, day=d)
                                       for d in HIST[:3] for m in MIN])], ignore_index=True)
bl2 = build_baseline(hist2)
checks.append(("只交易 3 天的股票：分母用 3 天平均而非 5 天",
               abs(bl2[(bl2["code"] == 63) & (bl2["m"] == 600)]["avg1"].iloc[0] - 1000) < 1))

# 稀疏分鐘：某檔 5 天都在 10:00 成交 1,000 股，只有 1 天在 10:07 又成交 500 股。
# 10:07 的平均累計量應為 1,000 + 500/5 = 1,100，而不是「只有 1 天有列」的 1,500/5 = 300。
sp = [dict(code=71, m=600, volume=1_000, day=d) for d in HIST]
sp += [dict(code=71, m=607, volume=500, day=HIST[0])]
bl3 = build_baseline(pd.DataFrame(sp))
c71 = bl3[bl3["code"] == 71].set_index("m")
checks += [
    ("稀疏分鐘的平均累計量不被低估（10:07 = 1,100）", abs(c71.loc[607, "avgcum"] - 1100) < 1),
    ("沒成交的分鐘沿用前一分鐘累計（10:03 = 1,000）", abs(c71.loc[603, "avgcum"] - 1000) < 1),
]

# 連續放量：今日每分鐘都是歷史同分鐘的 3 倍
rows2 = [bar(61, m, 100, (10_000 if m == 605 else 1_000) * 3) for m in range(570, 600)]
rows2 += [bar(61, m, 100, (10_000 if m == 605 else 1_000) * 3) for m in MIN]
hist2b = pd.DataFrame([dict(code=61, m=m, volume=10_000 if m == 605 else 1_000, day=d)
                       for d in HIST for m in range(570, 610)])
bl4 = build_baseline(hist2b)
a4 = run_rules(pd.DataFrame(rows2), stats[stats["code"] == 61], uni[uni["code"] == 61], cfg, baseline=bl4)
sk = a4[a4["rule"] == "連續放量"]
checks += [
    ("連續放量：每分鐘都 3 倍 → 觸發橙", len(sk) == 1 and sk["level"].iloc[0] == "橙"),
    ("連續放量倍數 ≈ 3", len(sk) == 1 and abs(sk["value"].iloc[0] - 3.0) < 0.05),
]

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
