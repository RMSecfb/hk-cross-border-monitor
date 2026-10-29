"""階段 7（B 軌覆蓋缺口）合成測試：封存全名單收錄 + 冬眠股異動

主案是把 1 分 K 封存的收錄門檻（原 20 日均成交額 ≥ 50 萬）取消，讓 1,649 檔冬眠股
拿得到同時點分母；配套的冬眠股異動指標的設計重點是「不經過 1 分 K」，所以測試刻意讓部分受測股票完全沒有 K 線，
驗證它們仍然進得了警報——那正是 1 分 K 封存改為全名單收錄後，仍會存在的 1～4 週空窗。
"""
import sys

import pandas as pd

sys.path.insert(0, "src")
import bars_store                     # noqa: E402
from common import load_yaml          # noqa: E402
from rules import run_rules           # noqa: E402

cfg = load_yaml("rules.yaml")
dm = cfg["dormant_move"]
checks = []
DAY = pd.Timestamp("2026-10-29")


def bar(c, m, px, v):
    return dict(code=c, time=DAY.normalize() + pd.Timedelta(minutes=int(m)),
                open=px, high=px, low=px, close=px, volume=v)


# 只有 10 號有 1 分 K；其餘各股完全沒有 K 線，模擬最冷門的冬眠股
bars = pd.DataFrame([bar(10, m, 10.0, 1_000) for m in range(570, 600)])

#  code  20 日均成交額      當日成交額        預期
#   10      300,000       12,000,000      40 倍 → 紅（有 1 分 K）
#   11      300,000        6,000,000      20 倍 → 橙（無 1 分 K，仍須觸發）
#   12      300,000        3,000,000      10 倍 → 黃
#   13      300,000        2,700,000       9 倍 → 未達黃，不觸發
#   14      300,000          900,000       3 倍 → 當日成交額未達下限，不觸發
#   15    5,000,000      500,000,000     100 倍 → 非冬眠股（均額 ≥ 50 萬），不觸發
#   16            0        5,000,000     套 base_floor → 500 倍 → 紅
#   17          NaN       10,000,000     無均額資料 → 不觸發
codes = [10, 11, 12, 13, 14, 15, 16, 17]
avg_to = [300_000, 300_000, 300_000, 300_000, 300_000, 5_000_000, 0, None]
cur_to = [12_000_000, 6_000_000, 3_000_000, 2_700_000, 900_000, 500_000_000, 5_000_000, 10_000_000]

uni = pd.DataFrame({"code": codes, "name": "X", "vcm_pct": [None] * len(codes)})
stats = pd.DataFrame({"code": codes, "prev_close": 10.0, "ma": 10.0, "avg_vol": 1e5,
                      "avg_turnover": avg_to, "halted": False})
close = pd.Series(10.0, index=codes)
vol = pd.Series([t / 10.0 for t in cur_to], index=codes)

a = run_rules(bars, stats, uni, cfg, daily_close=close, daily_volume=vol)
d = a[a["rule"] == "冬眠股異動"].set_index("code")

checks += [
    ("只有 4 檔觸發（10/11/12/16）", sorted(d.index) == [10, 11, 12, 16]),
    ("10：40 倍 → 紅", 10 in d.index and d.loc[10, "level"] == "紅"),
    ("11：完全沒有 1 分 K 也照樣觸發", 11 in d.index),
    ("11：20 倍 → 橙", 11 in d.index and d.loc[11, "level"] == "橙"),
    ("12：10 倍 → 黃", 12 in d.index and d.loc[12, "level"] == "黃"),
    ("13：9 倍未達黃燈，不觸發", 13 not in d.index),
    ("14：當日成交額未達下限，不觸發", 14 not in d.index),
    ("15：非冬眠股不納入，與成交量暴增不重疊", 15 not in d.index),
    ("16：均額為 0 不會算出無限大", 16 in d.index and pd.notna(d.loc[16, "value"])),
    ("16：套用 base_floor 後 = 500 倍 → 紅",
     16 in d.index and abs(d.loc[16, "value"] - 5_000_000 / dm["base_floor"]) < 1
     and d.loc[16, "level"] == "紅"),
    ("16：說明欄標明已套下限", 16 in d.index and "下限" in d.loc[16, "detail"]),
    ("17：無均額資料不觸發", 17 not in d.index),
    ("倍數算對：10 = 12,000,000 / 300,000 = 40",
     10 in d.index and abs(d.loc[10, "value"] - 40) < 0.01),
]

# 盤中（沒有日線）不得計算
a_intra = run_rules(bars, stats, uni, cfg)
checks.append(("盤中無日線資料時不計算", (a_intra["rule"] == "冬眠股異動").sum() == 0))

# 復牌股須帶標記：停牌期間成交額為 0 會壓低 20 日均額，倍數同樣失真
stats_h = stats.copy()
stats_h.loc[stats_h["code"] == 11, "halted"] = True
a_h = run_rules(bars, stats_h, uni, cfg, daily_close=close, daily_volume=vol)
r11 = a_h[(a_h["rule"] == "冬眠股異動") & (a_h["code"] == 11)]
checks.append(("復牌股帶「復牌首日」標記",
               len(r11) == 1 and "復牌首日" in r11["note"].iloc[0]))

# 門檻設定本身的健全性
checks += [
    ("分級遞增 黃 < 橙 < 紅", dm["黃"] < dm["橙"] < dm["紅"]),
    ("冬眠線與原 1 分 K 收錄門檻一致（50 萬）", dm["max_avg_turnover"] == 500_000),
    ("report 已登錄該規則的顯示格式",
     __import__("report").RULE_FMT.get("冬眠股異動", (None,))[0] == "dormant_move"),
]

# ---- 主案：1 分 K 封存不再依成交額篩選 ----
bars_all = pd.DataFrame(
    [bar(10, m, 10.0, 1_000) for m in range(570, 575)]        # 熱門股
    + [bar(99, 572, 1.0, 300)]                                 # 冬眠股，當天只成交 1 分鐘
    + [bar(98, 573, 1.0, 0)])                                  # 零成交，應被 volume > 0 濾掉
st_all = pd.DataFrame({"code": [10, 99, 98], "avg_turnover": [5e8, 30_000, 0]})
fr = bars_store.to_frame(bars_all, st_all)
checks += [
    ("封存門檻已取消（MIN_AVG_TURNOVER = 0）", bars_store.MIN_AVG_TURNOVER == 0),
    ("冬眠股（均額 3 萬）會被封存收錄", 99 in set(fr["code"])),
    ("熱門股照常收錄", 10 in set(fr["code"])),
    ("零成交的分鐘仍被濾掉（控制容量）", 98 not in set(fr["code"])),
    ("收錄後冬眠股即可取得同時點分母",
     not bars_store.build_baseline(fr.assign(day=DAY)).query("code == 99").empty),
]

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
