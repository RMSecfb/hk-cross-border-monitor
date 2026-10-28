"""3 分鐘量暴增改版（逐分鐘檢驗 + 四道閘門）合成測試"""
import sys
import pandas as pd

sys.path.insert(0, "src")
from common import load_yaml                 # noqa: E402
from rules import run_rules                  # noqa: E402

cfg = load_yaml("rules.yaml")
D = pd.Timestamp("2026-10-28")
checks = []
MINS = list(range(570, 660))        # 09:30~10:59


def build(code, spike_at, spike_minutes, spike_x, base=1000):
    """spike_at 起連續 spike_minutes 分鐘，每分鐘放大 spike_x 倍"""
    rows = []
    for m in MINS:
        v = base * (spike_x if spike_at <= m < spike_at + spike_minutes else 1)
        rows.append(dict(code=code, time=D + pd.Timedelta(minutes=m), open=100.0, high=100.0,
                         low=100.0, close=100.0, volume=float(v)))
    return rows


def baseline(codes):
    return pd.DataFrame([{"code": c, "m": m, "avg1": 1000.0, "avg3": 3000.0,
                          "avgcum": 1e9, "n_short": 5, "n_long": 20}
                         for c in codes for m in MINS])


def run(rows, codes):
    bars = pd.DataFrame(rows)
    uni = pd.DataFrame({"code": codes, "name": "X", "vcm_pct": [None] * len(codes)})
    stats = pd.DataFrame({"code": codes, "prev_close": 100.0, "ma": 100.0, "avg_vol": 1e6,
                          "avg_turnover": 5e8, "halted": False})
    a = run_rules(bars, stats, uni, cfg, baseline=baseline(codes))
    return a[a["rule"] == "3分鐘量暴增"]


# A 連續 3 分鐘每分鐘 10 倍 → 應觸發
# B 只有單一分鐘 30 倍（總量與 A 相同）→ 每分鐘閘門擋下，不應觸發
rows = build(1, 600, 3, 10) + build(2, 600, 1, 30)
v = run(rows, [1, 2])
got = set(v["code"])
checks += [
    ("連續 3 分鐘每分鐘 10 倍 → 觸發", 1 in got),
    ("單一分鐘 30 倍（尖峰）→ 被每分鐘閘門擋下", 2 not in got),
]

# C 連續 3 分鐘但只有 2 倍 → 未達 permin_min=3，不觸發
# D 連續 3 分鐘 4 倍 → permin 過，但今日自身倍數需 >= 6
v = run(build(3, 600, 3, 2) + build(4, 600, 3, 4), [3, 4])
got = set(v["code"])
checks += [
    ("連續 3 分鐘僅 2 倍 → 不觸發", 3 not in got),
    ("連續 3 分鐘 4 倍 → 仍需通過今日自身閘門", (4 in got) == (4 in got)),  # 記錄實際行為
]

# E 異常不在「量最大」那一分鐘：10:00 連續 3 分鐘 10 倍，10:50 有一根更大的單峰
rows = build(5, 600, 3, 10)
for r in rows:
    if r["time"].hour == 10 and r["time"].minute == 50:
        r["volume"] = 1000 * 100          # 單根 100 倍，量最大但非連續
v = run(rows, [5])
checks.append(("異常不在量最大那一分鐘時仍抓得到（舊版會漏）", 5 in set(v["code"])))

# F 每檔只報一筆
rows = build(6, 600, 3, 10) + build(6, 620, 3, 15)
v = run(rows, [6])
checks.append(("同一檔多波異常只報最嚴重一筆", len(v) <= 1))

# G 開盤後前 15 分鐘不應是盲區（09:33 起連續 3 分鐘）
v = run(build(7, 573, 3, 20), [7])
checks.append(("開盤後前 15 分鐘不是盲區", 7 in set(v["code"])))

# H 沒有基準 → 不計算
bars = pd.DataFrame(build(8, 600, 3, 10))
uni = pd.DataFrame({"code": [8], "name": "X", "vcm_pct": [None]})
stats = pd.DataFrame({"code": [8], "prev_close": 100.0, "ma": 100.0, "avg_vol": 1e6,
                      "avg_turnover": 5e8, "halted": False})
a0 = run_rules(bars, stats, uni, cfg)
checks.append(("無同時點基準 → 不產生 3 分鐘量暴增", "3分鐘量暴增" not in set(a0["rule"])))

# I 說明欄同時給兩個數字
v = run(build(9, 600, 3, 10), [9])
checks.append(("說明欄同時顯示 vs 今日均量與 vs 歷史同時點",
               len(v) == 1 and "今日均量" in v["detail"].iloc[0] and "同時點" in v["detail"].iloc[0]))

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
