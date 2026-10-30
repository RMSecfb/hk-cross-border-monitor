"""階段 8（日線合併）合成測試

盤後批次每次重抓最近 5 天的日線，原本無條件覆寫。Yahoo 偶爾只回部分資料時，
正確的歷史會被直接摧毀——實際發生過兩次（2026-10-21、2026-10-28），
後者污染了隔天整天的監控基準。本測試鎖住「合併、不覆寫」的行為。
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, "src")
import common                                # noqa: E402

tmp = Path(tempfile.mkdtemp())
common.DATA = tmp                            # 導向暫存目錄，不碰真實資料
import eod                                   # noqa: E402
eod.DATA = tmp
eod.path = common.path

checks = []
DAY = pd.Timestamp("2026-10-28")
F = tmp / "daily" / "2026" / "daily_20261028.parquet"


def frame(codes, close=10.0):
    return pd.DataFrame({"code": codes, "date": DAY, "open": close, "high": close,
                         "low": close, "close": close, "volume": 1000.0, "dividends": 0.0})


# 第一次：完整的 2,790 檔
eod.save_daily(frame(range(1, 2791)), DAY)
checks.append(("首次寫入 2,790 列", len(pd.read_parquet(F)) == 2790))

# 第二次：Yahoo 只回 25 檔（重現 2026-10-28 的災情）
eod.save_daily(frame(range(1, 26), close=11.0), DAY)
got = pd.read_parquet(F)
checks += [
    ("部分下載後仍保有 2,790 列（舊行為會剩 25 列）", len(got) == 2790),
    ("重疊的 25 檔以新資料為準（收盤價更新為 11.0）",
     (got[got["code"] <= 25]["close"] == 11.0).all()),
    ("未回傳的 2,765 檔保留原值", (got[got["code"] > 25]["close"] == 10.0).all()),
    ("沒有重複的 code", got["code"].is_unique),
]

# 第三次：Yahoo 回傳比既有更多（新上市），應一併納入
eod.save_daily(frame(range(1, 2801)), DAY)
checks.append(("新增的 10 檔會被納入（合併不是只保舊）", len(pd.read_parquet(F)) == 2800))

# upto 之後的日期不得寫入
future = frame([1]).assign(date=pd.Timestamp("2026-10-29"))
eod.save_daily(future, DAY)
checks.append(("upto 之後（未收市）的日期不寫檔",
               not (tmp / "daily" / "2026" / "daily_20261029.parquet").exists()))

# 全新的日期仍可正常建立
eod.save_daily(frame([1, 2]).assign(date=pd.Timestamp("2026-10-27")), pd.Timestamp("2026-10-27"))
checks.append(("全新日期照常建立", (tmp / "daily" / "2026" / "daily_20261027.parquet").exists()))

shutil.rmtree(tmp, ignore_errors=True)
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  OK  " if ok else "  FAIL") + "  " + n)
sys.exit(1 if bad else 0)
