"""階段 9（股份分割事件）合成測試

本檔原先鎖的是「回頭還原歷史價量」的行為，2026-11-03 推翻：
**Yahoo 的歷史價格絕大多數已經還原過分割。** auto_adjust=False 只影響股息還原
（Close vs Adj Close）；stock splits 欄位只是事件註記。實測 63 筆分割事件中 60 筆
（95%）分割當天的實際價格比都是 1.0——再還原一次就是把正確資料改壞。

同日再修一次：「絕大多數」不等於全部。1351 於 11/02 記錄比例 0.2，價格 1.250 → 0.250，
實際價格比恰好等於記錄比例，Yahoo 這一筆沒有回頭還原，序列留著 5 倍斷點。

所以現在鎖兩件事：
  ① 任何情況下都不得改動價量；
  ② 以「分割當日實際價格比 vs Yahoo 記錄比例」判定該筆是否已還原，只有確實沒還原的
     才進 split_unadjusted（不是把所有分割股一律標上）。
"""
import sys

import pandas as pd

sys.path.insert(0, "src")
import eod                                            # noqa: E402
from rules import SPLIT_TAG_RULES, SPLIT_UNADJ_RULES  # noqa: E402

checks = []
D = [pd.Timestamp("2026-10-01") + pd.Timedelta(days=i) for i in range(5)]


def frame(splits, code=1, closes=None):
    c = closes if closes is not None else [100.0] * 5
    return pd.DataFrame({"code": [code] * 5, "date": D, "open": c, "high": c,
                         "low": c, "close": c, "volume": 1000.0, "splits": splits})


checks.append(("函式已更名為 recent_splits，舊的 adjust_splits 不再存在",
               hasattr(eod, "recent_splits") and not hasattr(eod, "adjust_splits")))

# ---- ① 價量一律不得改動 ----
d = frame([0, 0, 10.0, 0, 0])
before = d.copy()
last, unadj = eod.recent_splits(d)
checks += [
    ("回傳 (last, unadj) 兩個字典", isinstance(last, dict) and isinstance(unadj, dict)),
    ("回報最後一次分割日", last == {1: D[2]}),
    ("價格完全未被改動", d["close"].tolist() == before["close"].tolist()),
    ("成交量完全未被改動", d["volume"].tolist() == before["volume"].tolist()),
    ("OHLC 皆未被改動", d[["open", "high", "low"]].equals(before[["open", "high", "low"]])),
]

d2 = frame([0, 0, 0.2, 0, 0]); b2 = d2.copy()
eod.recent_splits(d2)
checks.append(("反向分割也不得改動價量", d2["close"].tolist() == b2["close"].tolist()))

checks.append(("同檔多次分割取最後一次",
               eod.recent_splits(frame([0, 2.0, 0, 5.0, 0]))[0] == {1: D[3]}))

two = pd.concat([frame([0, 0, 10.0, 0, 0], 1), frame([0] * 5, 2)], ignore_index=True)
checks.append(("多檔：只回報有分割的那檔", eod.recent_splits(two)[0] == {1: D[2]}))

checks += [
    ("全無分割時回空字典", eod.recent_splits(frame([0] * 5))[0] == {}),
    ("舊格式缺 splits 欄位時回空字典、不報錯",
     eod.recent_splits(frame([0] * 5).drop(columns=["splits"]))[0] == ({}, {})[0]),
]

# ---- ② 是否已還原的判定：拿實際價格比對帳記錄比例 ----
# 已還原：記 10 倍，但價格沒跳（實際比 1.0）→ Yahoo 已處理，不標
adj = frame([0, 0, 10.0, 0, 0], closes=[100.0, 100.0, 100.0, 100.0, 100.0])
checks.append(("已還原（價格沒跳）不進 split_unadjusted", eod.recent_splits(adj)[1] == {}))

# 未還原（1351 實例）：記 0.2，價格 1.250 → 0.250，實際比 0.2
c1351 = frame([0, 0, 0.2, 0, 0], closes=[1.25, 1.25, 0.25, 0.25, 0.25])
checks.append(("未還原（實際比 = 記錄比例 0.2）判定為未還原",
               eod.recent_splits(c1351)[1] == {1: D[2]}))

# 未還原的反向案例：記 5.0，價格 ×5
rev = frame([0, 0, 5.0, 0, 0], closes=[1.0, 1.0, 5.0, 5.0, 5.0])
checks.append(("未還原的反向分割（×5）同樣判定為未還原",
               eod.recent_splits(rev)[1] == {1: D[2]}))

# 910 實例：記 0.005，實際比 0.758 → 差兩個數量級，屬已還原
c910 = frame([0, 0, 0.005, 0, 0], closes=[2.77, 2.77, 2.1, 2.1, 2.1])
checks.append(("記錄比例與實際比差兩個數量級 → 判為已還原", eod.recent_splits(c910)[1] == {}))

# 比例本身接近 1 → 無從判別，一律不標（避免把一般漲跌誤判成未還原）
near1 = frame([0, 0, 1.1, 0, 0], closes=[100.0, 100.0, 110.0, 110.0, 110.0])
checks.append(("記錄比例接近 1 時不標記（無從判別）", eod.recent_splits(near1)[1] == {}))

# 分割當日是序列第一筆（沒有前一日可比）→ 不標、不報錯
first = pd.DataFrame({"code": [1] * 3, "date": D[:3], "open": 1.0, "high": 1.0, "low": 1.0,
                      "close": 1.0, "volume": 1000.0, "splits": [0.2, 0, 0]})
checks.append(("分割落在序列第一筆時不標記、不報錯", eod.recent_splits(first)[1] == {}))

# ---- ③ 兩組標記的範圍 ----
checks += [
    ("第一組涵蓋分母來自 1 分 K 封存的三條與換手率",
     set(SPLIT_TAG_RULES) == {"3分鐘量暴增", "連續放量", "成交量暴增", "換手率異常"}),
    ("第一組不含價格類（價量已還原，價格類是對的）",
     not ({"偏離均線", "即時漲跌幅", "開盤價異常", "冬眠股異動"} & set(SPLIT_TAG_RULES))),
    ("第二組涵蓋跨多日分母的兩條",
     set(SPLIT_UNADJ_RULES) == {"偏離均線", "冬眠股異動"}),
    ("第二組不含只比 prev_close 的兩條",
     not ({"即時漲跌幅", "開盤價異常"} & set(SPLIT_UNADJ_RULES))),
    ("兩組互斥", not (set(SPLIT_TAG_RULES) & set(SPLIT_UNADJ_RULES))),
]

bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
