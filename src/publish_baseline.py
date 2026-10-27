"""由 Release 上現有的 1 分 K 建立同時點基準，存成本地檔供 workflow 上傳

量能三指標（成交量暴增、3 分鐘量暴增、連續放量）的分母就是這份基準；沒有它，那三個指標
不會計算（刻意不退回舊的時段內插分母）。平常由每日盤後批次自動產生，本程式供補跑完歷史
之後手動補發一次，讓新分母當天就能生效。

用法：python src/publish_baseline.py      會把產生的檔名印到 stdout 最後一行
"""
import sys

import bars_store
from common import get_logger

log = get_logger("baseline")


def main():
    days = bars_store.available_days(limit=25)[:20]
    if not days:
        raise SystemExit("Release 上沒有任何 bars-* 的 1 分 K，無法建立基準")
    hist, got = bars_store.load_history(days)
    if not got:
        raise SystemExit("1 分 K 下載失敗，無法建立基準")
    bl = bars_store.build_baseline(hist)
    if bl.empty:
        raise SystemExit("算出來的基準是空的")
    day = max(got)
    f = bars_store.save_baseline(bl, day)
    n_short = int(bl["n_short"].max()) if "n_short" in bl else 0
    n_long = int(bl["n_long"].max()) if "n_long" in bl else 0
    log.info(f"用 {len(got)} 個交易日（{min(got):%m/%d}~{day:%m/%d}）建立基準；"
             f"單檔最多累積 5 日視窗 {n_short} 天、20 日視窗 {n_long} 天")
    log.info("3 分鐘量暴增／連續放量需 5 天、成交量暴增需 10 天，未達者該檔仍不計算")
    print(f.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
