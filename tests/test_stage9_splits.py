"""階段 9（股份分割還原）合成測試

日線是未還原價。分割當天價格會跳一個級距，不還原就會讓橫跨分割點的 MA20、20 日均量、
均成交額與前收全部失真——2026-10-06 的 2723 由 747.00 變成 70.65，偏離均線連日報紅；
1780 與 1094 在 10/30 分割當天衝上風險排行第 1 與第 3 名，而「即時漲跌幅 −83%」整個
是分割造成的。本測試鎖住還原行為，以及「沒有分割資料時完全不動作」。
"""
import sys

import pandas as pd

sys.path.insert(0, "src")
from eod import adjust_splits          # noqa: E402
from rules import SPLIT_TAG_RULES      # noqa: E402

checks = []
D = [pd.Timestamp("2026-10-01") + pd.Timedelta(days=i) for i in range(5)]


def frame(splits):
    """收盤 100、量 1,000 的五天；splits 為每天的分割比例（0 = 無）"""
    return pd.DataFrame({"code": [1] * 5, "date": D, "open": 100.0, "high": 100.0,
                         "low": 100.0, "close": 100.0, "volume": 1000.0, "splits": splits})


# 第 3 天（index 2）發生 10 股換 1 股：之前的價格要除以 10、量要乘以 10
out, last = adjust_splits(frame([0, 0, 10.0, 0, 0]))
c, v = out["close"].tolist(), out["volume"].tolist()
checks += [
    ("分割前的收盤被除以 10", c[0] == 10.0 and c[1] == 10.0),
    ("分割當天及之後不動", c[2] == 100.0 and c[3] == 100.0 and c[4] == 100.0),
    ("分割前的成交量被乘以 10", v[0] == 10000.0 and v[1] == 10000.0),
    ("分割當天及之後的量不動", v[2] == 1000.0 and v[4] == 1000.0),
    ("回報最後一次分割日", last == {1: D[2]}),
    ("OHLC 四個欄位都還原", out["open"].iloc[0] == 10.0 and out["high"].iloc[0] == 10.0
     and out["low"].iloc[0] == 10.0),
]

# 反向分割（1 股換 10 股，比例 0.1）：之前的價格要乘以 10
out2, _ = adjust_splits(frame([0, 0, 0.1, 0, 0]))
checks.append(("反向分割：分割前收盤 ×10", abs(out2["close"].iloc[0] - 1000.0) < 1e-9))

# 同一檔兩次分割：係數要累乘
out3, _ = adjust_splits(frame([0, 2.0, 0, 5.0, 0]))
checks += [
    ("兩次分割累乘：第 1 天 ÷(2×5)", abs(out3["close"].iloc[0] - 10.0) < 1e-9),
    ("兩次分割累乘：第 3 天只 ÷5", abs(out3["close"].iloc[2] - 20.0) < 1e-9),
    ("最後一天不受影響", out3["close"].iloc[4] == 100.0),
]

# 沒有分割 → 完全不動
same, last0 = adjust_splits(frame([0] * 5))
checks += [
    ("全無分割時價格不變", same["close"].tolist() == [100.0] * 5),
    ("全無分割時成交量不變", same["volume"].tolist() == [1000.0] * 5),
    ("全無分割時不回報分割日", last0 == {}),
]

# 舊格式（沒有 splits 欄位）→ 原樣回傳，不得報錯
old = frame([0] * 5).drop(columns=["splits"])
back, last1 = adjust_splits(old)
checks += [
    ("舊格式無 splits 欄位時原樣回傳", back["close"].tolist() == [100.0] * 5 and last1 == {}),
    ("舊格式不新增欄位", list(back.columns) == list(old.columns)),
]

# 多檔互不影響
two = pd.concat([frame([0, 0, 10.0, 0, 0]), frame([0] * 5).assign(code=2)], ignore_index=True)
o4, l4 = adjust_splits(two)
checks += [
    ("多檔：有分割的那檔被還原", o4[o4["code"] == 1]["close"].iloc[0] == 10.0),
    ("多檔：沒分割的那檔不受影響", (o4[o4["code"] == 2]["close"] == 100.0).all()),
    ("多檔：只回報有分割的", set(l4) == {1}),
]

# 標記範圍
checks += [
    ("標記涵蓋分母來自 1 分 K 封存的三條與換手率",
     set(SPLIT_TAG_RULES) == {"3分鐘量暴增", "連續放量", "成交量暴增", "換手率異常"}),
    ("價格類不標記（日線已還原）",
     not ({"偏離均線", "即時漲跌幅", "開盤價異常"} & set(SPLIT_TAG_RULES))),
    ("冬眠股異動不標記（分母 avg_turnover 已隨日線還原）",
     "冬眠股異動" not in SPLIT_TAG_RULES),
]

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
