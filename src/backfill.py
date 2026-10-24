"""補跑歷史交易日的盤後報告（Yahoo 1 分鐘資料僅保留約 30 天）
依日期由舊到新逐日：下載當天 1 分鐘 K 線 → 盤後規則 → 預警列表、風險分數 → 更新基準檔與分鐘量基準
用法：python src/backfill.py 2026-10-20 2026-10-23 [--keep-profile]
      預設會先清空分鐘量基準（profile）再依序重建；--keep-profile 則沿用現有基準。
需先完成盤後初始化（data/daily 有這段期間的日線）。
"""
import sys

import pandas as pd

from common import DATA, download, get_logger, load_universe, load_yaml, path
from eod import eod_for_day, load_daily

log = get_logger("backfill")


def main(start, end, keep_profile=False):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if (pd.Timestamp.now() - start).days > 28:
        log.warning("起始日超過約 28 天前，Yahoo 可能已無 1 分鐘資料")
    uni = load_universe()
    cfg = load_yaml("rules.yaml")
    daily = load_daily(end, 400)
    days = sorted(d for d in daily["date"].unique() if start <= d <= end)
    if not days:
        raise SystemExit("這段期間沒有交易日的日線資料，請先執行盤後初始化")
    log.info(f"補跑交易日：{', '.join(f'{d:%m/%d}' for d in days)}")

    prof = path("profile", "profile.csv")
    if not keep_profile and prof.exists():
        prof.unlink()
        log.info("已清空分鐘量基準，將依補跑日期重建")

    for d in days:
        d = pd.Timestamp(d)
        raw = download(uni["yahoo"].tolist(), None, "1m", log,
                       start=f"{d:%Y-%m-%d}", end=f"{d + pd.Timedelta(days=1):%Y-%m-%d}")
        if raw.empty:
            log.warning(f"{d:%Y-%m-%d} 抓不到 1 分鐘資料，略過")
            continue
        eod_for_day(d, raw, daily, uni, cfg)
    log.info(f"補跑完成，報告在 {DATA / 'report'}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        raise SystemExit(__doc__)
    main(args[0], args[1], keep_profile="--keep-profile" in sys.argv)
