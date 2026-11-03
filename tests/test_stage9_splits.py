"""階段 9（股份分割事件）合成測試

本檔原先鎖的是「回頭還原歷史價量」的行為，2026-11-03 推翻：
**Yahoo 的歷史價格本來就已經還原過分割。** auto_adjust=False 只影響股息還原
（Close vs Adj Close），分割一律已套用在 OHLC 上；stock splits 欄位只是事件註記。
實測 63 筆分割事件中 60 筆（95%）的比例與 Yahoo 自家價格序列不一致，且分割當天的
實際價格比幾乎都是 1.0——再還原一次就是把正確資料改壞。

現在鎖的是：只取分割日期供標記，**任何情況下都不得改動價量**。
"""
import sys

import pandas as pd

sys.path.insert(0, "src")
import eod                             # noqa: E402
from rules import SPLIT_TAG_RULES      # noqa: E402

checks = []
D = [pd.Timestamp("2026-10-01") + pd.Timedelta(days=i) for i in range(5)]


def frame(splits, code=1):
    return pd.DataFrame({"code": [code] * 5, "date": D, "open": 100.0, "high": 100.0,
                         "low": 100.0, "close": 100.0, "volume": 1000.0, "splits": splits})


checks.append(("函式已更名為 recent_splits，舊的 adjust_splits 不再存在",
               hasattr(eod, "recent_splits") and not hasattr(eod, "adjust_splits")))

# 有分割：只回日期，價量一律不動
d = frame([0, 0, 10.0, 0, 0])
before = d.copy()
last = eod.recent_splits(d)
checks += [
    ("回報最後一次分割日", last == {1: D[2]}),
    ("價格完全未被改動", d["close"].tolist() == before["close"].tolist()),
    ("成交量完全未被改動", d["volume"].tolist() == before["volume"].tolist()),
    ("OHLC 皆未被改動", d[["open", "high", "low"]].equals(before[["open", "high", "low"]])),
]

# 反向分割（比例 < 1）同樣不得改動
d2 = frame([0, 0, 0.2, 0, 0]); b2 = d2.copy()
eod.recent_splits(d2)
checks.append(("反向分割也不得改動價量", d2["close"].tolist() == b2["close"].tolist()))

# 同檔多次分割 → 取最後一次
checks.append(("同檔多次分割取最後一次", eod.recent_splits(frame([0, 2.0, 0, 5.0, 0])) == {1: D[3]}))

# 多檔
two = pd.concat([frame([0, 0, 10.0, 0, 0], 1), frame([0] * 5, 2)], ignore_index=True)
checks.append(("多檔：只回報有分割的那檔", eod.recent_splits(two) == {1: D[2]}))

# 無分割／舊格式
checks += [
    ("全無分割時回空字典", eod.recent_splits(frame([0] * 5)) == {}),
    ("舊格式缺 splits 欄位時回空字典、不報錯",
     eod.recent_splits(frame([0] * 5).drop(columns=["splits"])) == {}),
]

# 標記範圍：分割後 1 分 K 封存的量級距改變、shares.csv 股本未更新，故仍需標記這兩類
checks += [
    ("標記涵蓋分母來自 1 分 K 封存的三條與換手率",
     set(SPLIT_TAG_RULES) == {"3分鐘量暴增", "連續放量", "成交量暴增", "換手率異常"}),
    ("價格類不標記", not ({"偏離均線", "即時漲跌幅", "開盤價異常"} & set(SPLIT_TAG_RULES))),
    ("冬眠股異動不標記", "冬眠股異動" not in SPLIT_TAG_RULES),
]

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
